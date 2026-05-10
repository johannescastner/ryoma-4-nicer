"""Tests for ``WorkflowAgent.build_tool_node`` GraphBubbleUp propagation.

Background: pre-fix, ``build_tool_node`` wrapped ``ToolNode`` in
``with_fallbacks([handle_tool_error], exception_key="error")`` with the
default ``exceptions_to_handle=(Exception,)`` — which catches
``GraphInterrupt`` (subclass of ``GraphBubbleUp`` subclass of ``Exception``).
This silently broke ``ask_human`` end-to-end since the wrapper was
introduced: tools that called ``interrupt(payload)`` had their
``GraphInterrupt`` swallowed and converted into an error
``ToolMessage(content="Error: GraphInterrupt(...)\\n please fix your mistakes.")``
instead of pausing the Pregel runtime so the harness could resume with
``Command(resume=...)``. Empirically reproduced in
``/tmp/test_pr1pp_shape_b.py`` (5 cases, 3 configurations).

Fix (Shape C, empirically verified): replace the ``with_fallbacks``
wrapper entirely with langgraph's native ``ToolNode(handle_tool_errors=...)``
parameter. langgraph's ToolNode already correctly excludes
``GraphBubbleUp`` from its internal error handling (see
``langgraph/prebuilt/tool_node.py:447-455``: ``except GraphBubbleUp as
e: raise e`` BEFORE the generic ``except Exception``). The callable we
pass preserves the exact pre-fix error message format
(``"Error: {repr(e)}\\n please fix your mistakes."``).

Tests use REAL instances throughout (real Pregel graph, real ToolNode,
real langgraph errors) — no mocks per the project's no-mocks principle.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphBubbleUp, GraphInterrupt, ParentCommand
from langgraph.graph import START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.types import Command, interrupt

from ryoma_ai.agent.workflow import WorkflowAgent


# ──────────────────────────────────────────────────────────────────
# Tools simulating each scenario
# ──────────────────────────────────────────────────────────────────


@tool
def t_interrupt(message: str) -> str:
    """Tool that calls interrupt() — should pause Pregel, not error."""
    payload = interrupt({"kind": "ask_human", "message": message})
    return f"resumed: {payload}"


@tool
def t_parent_command(payload: str) -> str:
    """Tool that raises ParentCommand — handoff to parent graph."""
    raise ParentCommand(Command(goto="parent_node"))


@tool
def t_value_error(query: str) -> str:
    """Tool that raises ValueError — should be caught by ToolNode's
    internal error handling, returning an error ToolMessage."""
    raise ValueError("boom")


class _CustomError(Exception):
    """Custom Exception subclass — direct Exception, not a built-in."""


@tool
def t_custom_error(query: str) -> str:
    """Tool that raises a custom Exception subclass."""
    raise _CustomError("custom boom")


@tool
def t_success(query: str) -> str:
    """Tool that returns successfully."""
    return f"result for {query}"


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _build_graph(tools):
    """Build a Pregel graph using ``WorkflowAgent.build_tool_node``."""
    node = WorkflowAgent.build_tool_node(tools)
    workflow = StateGraph(MessagesState)
    workflow.add_node("tools", node)
    workflow.add_edge(START, "tools")
    return workflow.compile(checkpointer=MemorySaver())


def _ai_with_tool_call(name: str, args: dict, call_id: str = "tc1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


# ──────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────


def test_interrupt_propagates_to_pregel_runtime():
    """Tool calls ``interrupt(...)`` → Pregel pauses with ``state.next``
    and ``state.interrupts`` populated; NO error ``ToolMessage`` is
    emitted.

    Pre-fix: this test FAILED — ``with_fallbacks`` swallowed the
    GraphInterrupt and emitted ``ToolMessage(content="Error: GraphInterrupt(...)")``.
    """
    graph = _build_graph([t_interrupt])
    cfg = {"configurable": {"thread_id": "test-interrupt"}}
    initial = {"messages": [_ai_with_tool_call("t_interrupt", {"message": "what?"})]}

    result = graph.invoke(initial, cfg)
    state = graph.get_state(cfg)

    # Pregel paused — runtime can resume with Command(resume=...)
    assert state.next == ("tools",), (
        f"Expected state.next=('tools',) (pause point); got {state.next}. "
        "GraphInterrupt was swallowed instead of propagating."
    )
    assert state.interrupts, (
        "Expected state.interrupts to be populated; was empty. "
        "GraphInterrupt did not reach the Pregel runtime."
    )
    last_msg = result["messages"][-1] if result.get("messages") else None
    if last_msg is not None and isinstance(last_msg, ToolMessage):
        assert "Error: GraphInterrupt" not in (last_msg.content or ""), (
            "Found error ToolMessage from swallowed GraphInterrupt. "
            "The wrapper is still catching bubble-ups."
        )


def test_parent_command_propagates():
    """Tool raises ``ParentCommand(Command(...))`` → propagates as a
    bubble-up; not converted into an error ToolMessage.

    Pre-fix: this also failed for the same reason as the interrupt test.
    """
    graph = _build_graph([t_parent_command])
    cfg = {"configurable": {"thread_id": "test-parent-command"}}
    initial = {
        "messages": [_ai_with_tool_call("t_parent_command", {"payload": "x"})]
    }

    # ParentCommand should not be converted into an error ToolMessage.
    # Either it propagates as an exception OR the runtime captures it.
    try:
        result = graph.invoke(initial, cfg)
    except GraphBubbleUp:
        # Acceptable outcome — bubble-up reached the outer caller.
        return
    except Exception as e:  # noqa: BLE001
        # ParentCommand is a GraphBubbleUp — must not surface as
        # any other exception type.
        if not isinstance(e, GraphBubbleUp):
            raise AssertionError(
                f"Expected GraphBubbleUp to propagate; got {type(e).__name__}: {e}"
            )
        return

    # If the runtime captured the bubble-up, it must NOT be an error
    # ToolMessage:
    last_msg = result["messages"][-1] if result.get("messages") else None
    if isinstance(last_msg, ToolMessage):
        assert "Error: ParentCommand" not in (last_msg.content or ""), (
            "ParentCommand was swallowed and converted to an error "
            "ToolMessage — the wrapper is still catching bubble-ups."
        )


def test_value_error_becomes_error_tool_message():
    """Tool raises ``ValueError`` → converted into an error ``ToolMessage``
    matching ryoma's pre-fix format.

    This is the preserved behavior — the fix only changes how bubble-ups
    are handled, not how legitimate tool errors are handled.
    """
    graph = _build_graph([t_value_error])
    cfg = {"configurable": {"thread_id": "test-value-error"}}
    initial = {"messages": [_ai_with_tool_call("t_value_error", {"query": "boom"})]}

    result = graph.invoke(initial, cfg)
    last_msg = result["messages"][-1]

    assert isinstance(last_msg, ToolMessage)
    assert "Error: " in (last_msg.content or "")
    assert "boom" in (last_msg.content or "")
    assert "please fix" in (last_msg.content or "").lower(), (
        f"Expected ryoma's 'please fix your mistakes' template; got: "
        f"{last_msg.content!r}"
    )


def test_custom_exception_becomes_error_tool_message():
    """Tool raises a custom Exception subclass → converted into an
    error ``ToolMessage`` (not propagated).

    Critical regression guard against an alternative fix shape that
    would have used an explicit allow-list of exception types — that
    approach would have let custom subclasses escape Pregel as raw
    exceptions, breaking ``AmbiguousDatasetError`` / ``AmbiguousDashboardError``
    in baby-NICER (PR-2's typed exceptions, both direct ``Exception``
    subclasses).
    """
    graph = _build_graph([t_custom_error])
    cfg = {"configurable": {"thread_id": "test-custom-error"}}
    initial = {
        "messages": [_ai_with_tool_call("t_custom_error", {"query": "boom"})]
    }

    result = graph.invoke(initial, cfg)
    last_msg = result["messages"][-1]

    assert isinstance(last_msg, ToolMessage)
    assert "Error: " in (last_msg.content or "")
    assert "_CustomError" in (last_msg.content or "") or "custom boom" in (
        last_msg.content or ""
    )


def test_successful_tool_emits_result_tool_message():
    """Tool returns successfully → result ``ToolMessage``. Regression
    guard for the success path."""
    graph = _build_graph([t_success])
    cfg = {"configurable": {"thread_id": "test-success"}}
    initial = {"messages": [_ai_with_tool_call("t_success", {"query": "ok"})]}

    result = graph.invoke(initial, cfg)
    last_msg = result["messages"][-1]

    assert isinstance(last_msg, ToolMessage)
    assert "result for ok" in (last_msg.content or "")


def test_build_tool_node_no_longer_uses_with_fallbacks():
    """AST sentinel: ``build_tool_node`` must not re-introduce
    ``with_fallbacks`` (the source of the original bug).

    Uses ``ast.parse`` + ``ast.walk`` — no regex per the project's
    no-regex ironclad rule.
    """
    import ast
    import inspect

    source = inspect.getsource(WorkflowAgent.build_tool_node)
    tree = ast.parse(inspect.cleandoc(source))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "with_fallbacks":
            raise AssertionError(
                "WorkflowAgent.build_tool_node still calls .with_fallbacks(). "
                "This was the source of the GraphInterrupt-swallowing bug. "
                "The fix uses langgraph's native ToolNode(handle_tool_errors=...) "
                "instead — no with_fallbacks needed."
            )
