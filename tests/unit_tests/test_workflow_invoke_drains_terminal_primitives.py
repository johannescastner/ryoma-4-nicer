"""Tests for the terminal-primitive drain logic in ``WorkflowAgent.invoke``.

Background: when the iteration cap fires with a queued tool call,
``WorkflowAgent.invoke`` synthesises a "best-effort" wrap-up answer
and DOES NOT run the queued tool. For ordinary tools that's
acceptable graceful degradation. But for "terminal primitives" —
tools whose execution carries a user-visible side effect that
synthesizing an answer cannot replace (``ask_human`` calls
``langgraph.types.interrupt()``, swarm handoff tools return
``Command(goto=..., graph=PARENT)``) — silently dropping the queued
call is itself a bug.

This module adds a drain block: when the cap is hit and EVERY queued
tool_call's target tool is marked as a terminal primitive, run one
more super-step so the tool actually executes. Identification is
structural via ``tool.metadata`` keys (no string-matching).

Empirical motivation: in v30 of the IntellAgent eval (build
``917605bd-1ec7-4655-9014-0ced57a22939``), two ``ask_human``
invocations both fired exactly at iteration 200; the wrap-up
truncated them. The harness never observed the interrupt, so the
HITL code path was structurally untestable.
"""
from __future__ import annotations

import ast
import inspect

import pytest


def test_terminal_primitive_metadata_key_exported():
    """The metadata key constant must be exported so callers in
    other repos (e.g. baby-NICER's ``build_ask_human_tool``) can
    stamp it on their tools without re-defining the magic string."""
    from ryoma_ai.agent.workflow import TERMINAL_PRIMITIVE_METADATA_KEY
    assert isinstance(TERMINAL_PRIMITIVE_METADATA_KEY, str)
    assert TERMINAL_PRIMITIVE_METADATA_KEY  # non-empty


def test_is_terminal_tool_recognises_explicit_marker():
    """A tool with ``metadata[TERMINAL_PRIMITIVE_METADATA_KEY] = True``
    must be classified as terminal."""
    from langchain_core.tools import tool
    from ryoma_ai.agent.workflow import (
        TERMINAL_PRIMITIVE_METADATA_KEY,
        _is_terminal_tool,
    )

    @tool
    def t_pause(question: str) -> str:
        """Stub terminal-primitive tool."""
        return f"got: {question}"

    t_pause.metadata = {TERMINAL_PRIMITIVE_METADATA_KEY: True}
    assert _is_terminal_tool(t_pause) is True


def test_is_terminal_tool_recognises_handoff_metadata_key():
    """Swarm handoff tools set ``metadata["__handoff_destination"]``
    via ``create_handoff_tool``. They must be classified as terminal
    so the drain block runs for them too — without us hard-coding
    the swarm package's marker."""
    from langchain_core.tools import tool
    from ryoma_ai.agent.workflow import _is_terminal_tool

    @tool
    def t_handoff(message: str) -> str:
        """Stub handoff tool."""
        return message

    t_handoff.metadata = {"__handoff_destination": "other_agent"}
    assert _is_terminal_tool(t_handoff) is True


def test_is_terminal_tool_returns_false_for_ordinary_tool():
    from langchain_core.tools import tool
    from ryoma_ai.agent.workflow import _is_terminal_tool

    @tool
    def t_plain(x: str) -> str:
        """Stub ordinary tool."""
        return x
    assert _is_terminal_tool(t_plain) is False


def test_is_terminal_tool_handles_none_safely():
    """The drain logic looks tools up by name and may get None back
    if the name doesn't match. Must not raise."""
    from ryoma_ai.agent.workflow import _is_terminal_tool
    assert _is_terminal_tool(None) is False


def test_drain_block_does_not_string_match_tool_names():
    """AST sentinel: the drain block must use ``_is_terminal_tool``
    (a structural metadata check) to identify terminal primitives —
    NOT substring matching on tool names like ``"ask_human"`` or
    ``"transfer_"``. Brittle string matching is forbidden by the
    project's no-regex / no-string-match policy."""
    from ryoma_ai.agent import workflow
    src = inspect.getsource(workflow.WorkflowAgent.invoke)
    tree = ast.parse(inspect.cleandoc(src))
    forbidden_literals = {
        "ask_human", "transfer_to_", "transfer_to_chat_pro",
        "handoff", "swarm_handoff",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value not in forbidden_literals, (
                f"workflow.py contains literal tool-name string "
                f"{node.value!r} — drain logic must be structural via "
                f"tool.metadata, not name-based."
            )


def test_bigquery_datasource_query_warning_includes_sql():
    """PR-A3 ryoma-side: when ``BigQueryDataSource.query()`` logs the
    400/404 exception WARNING, it must include the failing SQL so
    we can recover the LLM's actual emitted text from the build log
    without downloading SQLite memory.db from GCS."""
    from ryoma_ai.datasource import bigquery
    src = inspect.getsource(bigquery)
    # Find the WARNING block; assert it carries either ``sql=%r`` or
    # ``sql={sql}`` (any param formatter against the SQL variable).
    assert "Error executing INFORMATION_SCHEMA" in src or "Error executing" in src, (
        "ryoma's bigquery.py should log query errors — base assumption "
        "violated; check the install path."
    )
    # The fix expectation: WARNING line near the exception handler
    # references the sql variable in a format spec.
    assert "sql=%r" in src or "%(sql)" in src or "{sql!r}" in src or "sql:%s" in src, (
        "PR-A3 ryoma-side: BigQueryDataSource.query() WARNING block "
        "must include the SQL string. Currently the SQL is dropped "
        "from the log, so v30's malformed-SQL recovery required "
        "downloading the per-experiment memory.db from GCS — a "
        "logging gap that closes here."
    )
