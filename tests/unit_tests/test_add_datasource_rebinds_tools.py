"""Regression tests for ``WorkflowAgent.add_datasource`` triggering re-bind.

Background: pre-fix, ``add_datasource`` (defined on ``BaseAgent``)
registered the datasource in the ``ResourceRegistry`` but never re-walked
tools to assign their ``datasource`` attribute. So if an agent registers
its datasource AFTER ``__init__`` (the canonical baby-NICER flow at
``reflective.py:1942`` — ``.add_datasource(ds)``), tools that should
have received the datasource were left at ``None`` indefinitely.

Fix: ``WorkflowAgent`` overrides ``add_datasource`` to call a lightweight
``_assign_datasources_to_tools`` helper after registration. The helper
does NOT re-bind the model (no model rebuild); it only walks
``self.tools`` and sets ``tool.datasource`` for tools that declare a
``datasource`` field.

These tests use REAL instances throughout: real ``SqliteDataSource``
(``:memory:``), real ``SqlQueryTool`` / ``QueryProfileTool``, real
``WorkflowAgent`` subclass that bypasses only the parts of init that
require a live LLM (we don't exercise the model in these tests).
"""
from __future__ import annotations

import pytest

from ryoma_ai.agent.base import BaseAgent
from ryoma_ai.agent.workflow import WorkflowAgent
from ryoma_ai.datasource.sqlite import SqliteDataSource
from ryoma_ai.tool.sql_tool import QueryProfileTool, SqlQueryTool


class _MinimalWorkflowAgent(WorkflowAgent):
    """Real ``WorkflowAgent`` subclass that bypasses ``_bind_tools``
    and ``_build_workflow`` init.

    Real Python class — not a mock. Inherits the actual
    ``add_datasource`` and ``_assign_datasources_to_tools`` methods we're
    testing. Only skips the LLM-binding and workflow-compilation parts
    of init that aren't relevant to the tool-binding mechanism.
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


def test_add_datasource_assigns_to_tools_post_init(real_datasource):
    """Construct a real agent that's already past ``__init__`` (so
    ``_bind_tools`` already ran against an empty registry), then call
    ``add_datasource(ds)``, and assert every tool with a ``datasource``
    field now has the datasource assigned. Pre-fix: tools' datasource
    stayed at ``None``.
    """
    agent = _MinimalWorkflowAgent([SqlQueryTool(), QueryProfileTool()])

    # Real call to the production API (not mocked).
    agent.add_datasource(real_datasource)

    sql_tool = next(t for t in agent.tools if isinstance(t, SqlQueryTool))
    qp_tool = next(t for t in agent.tools if isinstance(t, QueryProfileTool))

    assert sql_tool.datasource is real_datasource
    assert qp_tool.datasource is real_datasource, (
        "QueryProfileTool's datasource must be assigned by the post-init "
        "add_datasource path. Pre-fix this assignment never happened, "
        "leaving tool.datasource at None and causing AttributeError on "
        "first invocation."
    )


def test_add_datasource_returns_self_for_chaining(real_datasource):
    """``add_datasource`` historically returned ``self`` so callers can
    chain (``agent.add_datasource(ds).add_datasource(...)``). Preserve
    this contract — Bug #2's surgical-fix discipline applies: don't
    silently change return signatures."""
    agent = _MinimalWorkflowAgent([])

    result = agent.add_datasource(real_datasource)

    assert result is agent, (
        "add_datasource must return self for chaining (existing contract)."
    )


def test_add_datasource_chaining_works_with_real_agent(real_datasource):
    """Functional test of the chaining contract: registering one
    datasource then another via chain."""
    agent = _MinimalWorkflowAgent([SqlQueryTool()])
    second_datasource = SqliteDataSource(":memory:")

    # Chained call exercises the production API shape.
    agent.add_datasource(real_datasource).add_datasource(second_datasource)

    sql_tool = agent.tools[0]
    # Last-registered datasource wins (existing _bind_tools loop semantics).
    assert sql_tool.datasource is second_datasource
