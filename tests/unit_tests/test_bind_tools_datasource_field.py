"""Regression tests for ``WorkflowAgent._assign_datasources_to_tools`` gate.

Background: the original gate at ``workflow.py:94`` was
``if hasattr(tool, "type")`` — DEAD CODE because no ``BaseTool`` subclass
declares a ``type`` attribute. Result: every ``SqlDataSourceTool``
subclass had ``datasource = None`` after init, and any invocation of
(e.g.) ``QueryProfileTool`` raised ``AttributeError`` on ``None``.
``SqlQueryTool`` was the only tool that worked, because
``SqlAgent.__init__`` had a separate special case.

Fix: replace the dead gate with a typed Pydantic field check —
``"datasource" in getattr(tool, "model_fields", {})``. Walks every
BaseTool subclass that DECLARES a ``datasource`` field (the canonical
signal that the tool wants a datasource bound), without requiring a
brittle ``isinstance`` tied to a specific class hierarchy.

These tests use REAL instances throughout: real ``SqliteDataSource``
(``:memory:``), real ``SqlQueryTool`` / ``QueryProfileTool`` etc.,
real ``WorkflowAgent`` subclass. No mocks — mocks would seed false
confidence and bleed into production patterns.
"""
from __future__ import annotations

import ast
import inspect

import pytest
from pydantic import BaseModel

from ryoma_ai.agent.base import BaseAgent
from ryoma_ai.agent.workflow import WorkflowAgent
from ryoma_ai.datasource.sqlite import SqliteDataSource
from ryoma_ai.tool.sql_tool import (
    CreateTableTool,
    QueryPlanTool,
    QueryProfileTool,
    SqlQueryTool,
)


class _MinimalWorkflowAgent(WorkflowAgent):
    """Real ``WorkflowAgent`` subclass that bypasses ``_bind_tools``
    and ``_build_workflow`` init.

    The methods under test (``_assign_datasources_to_tools``,
    ``add_datasource``) operate on ``self.tools`` and
    ``self.resource_registry`` only — both initialized via
    ``BaseAgent.__init__``. We don't need a live LLM, langgraph
    workflow, or memory for unit tests of the binding mechanism.

    This is a real class instance (not a mock), so attribute lookups
    follow real Python MRO and Pydantic field reflection works
    naturally.
    """

    def __init__(self, tools):
        BaseAgent.__init__(self)
        self.tools = tools


@pytest.fixture
def real_datasource():
    """Real in-memory SQLite datasource. Concrete ``SqlDataSource``
    subclass that needs no credentials, no network, and constructs in
    microseconds. The assignments we test only check identity
    (``tool.datasource is ds``), so any real DataSource subclass works."""
    return SqliteDataSource(":memory:")


@pytest.fixture
def agent_with_sql_tools(real_datasource):
    """Real agent populated with all four ``SqlDataSourceTool``
    subclasses + a registered real datasource. Uses ``add_datasource``
    so the test's pre-condition exercises the SAME post-init path
    baby-NICER uses (``reflective.py:1942``)."""
    agent = _MinimalWorkflowAgent([
        SqlQueryTool(),
        CreateTableTool(),
        QueryProfileTool(),
        QueryPlanTool(),
    ])
    agent.add_datasource(real_datasource)
    return agent


def test_assign_assigns_datasource_to_sql_query_tool(
    agent_with_sql_tools, real_datasource
):
    sql_tool = next(t for t in agent_with_sql_tools.tools if isinstance(t, SqlQueryTool))
    assert sql_tool.datasource is real_datasource, (
        "SqlQueryTool declares ``datasource`` as a Pydantic field; the new "
        "gate must assign the registered datasource to it."
    )


def test_assign_assigns_datasource_to_query_profile_tool(
    agent_with_sql_tools, real_datasource
):
    """QueryProfileTool regression: pre-fix, this tool's datasource
    stayed at ``None`` because the gate ``hasattr(tool, "type")`` failed,
    and ``SqlAgent.__init__`` only special-cased ``SqlQueryTool``.
    """
    qp_tool = next(t for t in agent_with_sql_tools.tools if isinstance(t, QueryProfileTool))
    assert qp_tool.datasource is real_datasource, (
        "QueryProfileTool declares ``datasource`` as a Pydantic field; the new "
        "gate must assign the registered datasource. Pre-fix this assignment "
        "never happened — leading to AttributeError on first invocation."
    )


def test_assign_assigns_datasource_to_create_table_tool(
    agent_with_sql_tools, real_datasource
):
    """CreateTableTool also declares ``datasource`` and was equally
    broken pre-fix. It is normally stripped in baby-NICER but the
    upstream ryoma gate must still cover it for any non-baby-NICER
    consumer."""
    ct_tool = next(t for t in agent_with_sql_tools.tools if isinstance(t, CreateTableTool))
    assert ct_tool.datasource is real_datasource


def test_assign_assigns_datasource_to_query_plan_tool(
    agent_with_sql_tools, real_datasource
):
    """QueryPlanTool is dead code in current ``SqlAgent.__init__`` (never
    instantiated) but exists in upstream and was equally broken.
    Coverage protects any future consumer that uses it."""
    plan_tool = next(t for t in agent_with_sql_tools.tools if isinstance(t, QueryPlanTool))
    assert plan_tool.datasource is real_datasource


def test_assign_skips_tools_without_datasource_field(real_datasource):
    """Tools that DON'T declare ``datasource`` as a Pydantic field
    should not be touched. Synthesizes one such tool and asserts the
    gate skips it cleanly."""

    class _ToolWithoutDatasourceField(BaseModel):
        name: str = "no_datasource_tool"

    tool_without = _ToolWithoutDatasourceField()
    sql_tool = SqlQueryTool()

    agent = _MinimalWorkflowAgent([sql_tool, tool_without])
    agent.add_datasource(real_datasource)

    # SqlQueryTool received it (gate fires)
    assert sql_tool.datasource is real_datasource
    # Tool without the field doesn't even have a datasource attribute
    assert not hasattr(tool_without, "datasource")


def test_bind_tools_gate_does_not_use_dead_hasattr_type():
    """AST sentinel: the gate condition in ``_bind_tools`` (or its
    extracted helper) must not be ``hasattr(tool, "type")`` (the
    historical dead-code form). We verify this structurally by reading
    the source of both functions and asserting the broken pattern is
    absent. Uses ``ast.parse`` + ``ast.walk``, no regex.

    The helper name may vary; we check both the public ``_bind_tools``
    and any function in the module whose AST contains the broken gate.
    """
    # getsource on a class returns "class X:\n    def...\n..." which
    # parses cleanly with ast.parse (top-level class definition is the
    # canonical input shape).
    source = inspect.getsource(WorkflowAgent)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "hasattr"):
            continue
        if len(node.args) != 2:
            continue
        first, second = node.args
        if not (
            isinstance(first, ast.Name)
            and first.id == "tool"
            and isinstance(second, ast.Constant)
            and second.value == "type"
        ):
            continue
        raise AssertionError(
            "WorkflowAgent still contains the dead-code gate "
            "``hasattr(tool, 'type')`` somewhere — this gate never fires "
            "because no BaseTool subclass declares a ``type`` attribute. "
            "Replace with a Pydantic field check: "
            "``'datasource' in getattr(tool, 'model_fields', {})``."
        )
