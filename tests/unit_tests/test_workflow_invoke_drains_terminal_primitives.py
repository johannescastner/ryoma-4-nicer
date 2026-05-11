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


# ────────────────────────────────────────────────────────────────────────
# PR-B' (ryoma 0.8.7) — post-drain state-handling fix
#
# Background: ryoma 0.8.6 added the drain block (above tests) but did
# not handle the post-drain state correctly. After a terminal-primitive
# tool calls ``interrupt(payload)``, langgraph's Pregel loop suppresses
# the GraphInterrupt and surfaces it via BOTH ``result['__interrupt__']``
# AND ``state.tasks[*].interrupts``. But it ALSO leaves ``state.next``
# as ``('tools',)`` (per langgraph/pregel/main.py:1097 — the interrupted
# task is listed because its writes are empty). The 0.8.6 drain block
# refreshed ``current_state`` and then fell through to the existing
# wrap-up block ``if current_state.next:`` (line 352+), which fires
# because ``state.next`` is truthy.
#
# The wrap-up synthesises an AI answer and OVERWRITES ``result``,
# clobbering the ``__interrupt__`` key. Caller (eval harness or
# langgraph_slack server) never observes the pause.
#
# Empirically confirmed in v31 IntellAgent eval at 16:23:31 (build
# 82d0ec4c) and reproduced standalone in /tmp/probe_drain_state.py.
# The fix: insert a guard between the drain and the wrap-up. If any
# interrupt is present in result OR in state.tasks, return result raw
# without entering the wrap-up block.
# ────────────────────────────────────────────────────────────────────────


def _build_drain_test_agent(tool_with_interrupt):
    """Build a minimal WorkflowAgent that exercises the drain path.

    Uses a custom stub LLM that supports ``bind_tools`` (langchain's
    fake models don't) and emits a single tool_call AIMessage on each
    call. ``max_iterations`` is passed at invoke time so the drain
    triggers cleanly when the cap is hit.
    """
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage
    from ryoma_ai.agent.workflow import WorkflowAgent

    # Stub LLM that replies with a tool_call AIMessage on every call.
    # Subclass FakeMessagesListChatModel to add ``bind_tools`` (which
    # WorkflowAgent.__init__ calls at construction time). The stub
    # bind_tools is a no-op — we don't need real tool-binding because
    # the responses are pre-baked.
    ai = AIMessage(
        content="invoking tool",
        tool_calls=[{
            "name": tool_with_interrupt.name,
            "args": {"question": "USD or JPY?"},
            "id": "tc_drain_1",
            "type": "tool_call",
        }],
    )

    class _StubChatModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    llm = _StubChatModel(responses=[ai] * 20)

    agent = WorkflowAgent(
        tools=[tool_with_interrupt],
        model=llm,
    )
    return agent


def test_drain_skips_wrapup_when_interrupt_pauses_graph():
    """PR-B': when a terminal-primitive tool calls ``interrupt()``, the
    drain block must surface the interrupt and SKIP the wrap-up
    synthesis. Without this guard, the wrap-up overwrites
    ``result['__interrupt__']`` and the harness never observes the
    pause. Bug confirmed in v31 at 16:23:31.
    """
    from langchain_core.tools import tool
    from langgraph.types import interrupt
    from ryoma_ai.agent.workflow import (
        TERMINAL_PRIMITIVE_METADATA_KEY,
        ToolMode,
    )

    @tool
    def t_pause(question: str) -> str:
        """Terminal-primitive tool that pauses for human input."""
        a = interrupt({"q": question})
        return f"resumed:{a}"
    t_pause.metadata = {TERMINAL_PRIMITIVE_METADATA_KEY: True}

    agent = _build_drain_test_agent(t_pause)
    result = agent.invoke(
        question="hi",
        tool_mode=ToolMode.CONTINUOUS,
        max_iterations=2,
        display=False,
    )

    # 1. The result dict must still carry __interrupt__.
    assert isinstance(result, dict), (
        f"Expected dict result, got {type(result).__name__}. The wrap-up "
        f"likely fired and overwrote the drain's __interrupt__-carrying dict."
    )
    assert "__interrupt__" in result, (
        f"PR-B' bug: drain ran but result['__interrupt__'] is missing. "
        f"The wrap-up block fired and overwrote the dict. Result keys: "
        f"{list(result.keys())}"
    )
    assert result["__interrupt__"], (
        f"PR-B' bug: result['__interrupt__'] is present but empty. "
        f"Got: {result['__interrupt__']!r}"
    )
    # Interrupt payload should carry our question.
    first_int = result["__interrupt__"][0]
    val = getattr(first_int, "value", first_int)
    assert val == {"q": "USD or JPY?"}, (
        f"Interrupt payload mismatch: expected {{'q': 'USD or JPY?'}}, "
        f"got {val!r}"
    )

    # 2. The wrap-up's signature must NOT appear in the result messages.
    # The wrap-up code at workflow.py:354+ adds an AIMessage with
    # text like "I've reached the current tool-usage budget" — if that
    # message is in the result, the wrap-up ran on top of the
    # interrupt (the bug).
    msgs = result.get("messages", [])
    for m in msgs:
        content = getattr(m, "content", "") or ""
        assert "tool-usage budget" not in content.lower(), (
            f"PR-B' bug: wrap-up synthesis ran despite the interrupt. "
            f"Found wrap-up message: {content[:200]!r}"
        )

    # 3. The persistent state must also show the interrupt.
    state = agent.get_current_state()
    assert state.tasks, "Expected non-empty state.tasks after interrupt"
    interrupted = [t for t in state.tasks if getattr(t, "interrupts", None)]
    assert interrupted, (
        f"Expected at least one task with non-empty .interrupts. "
        f"Tasks: {[(t.name, t.interrupts) for t in state.tasks]}"
    )


def test_drain_falls_through_to_wrapup_when_no_interrupt():
    """PR-B' regression guard: a terminal-primitive tool that does NOT
    call interrupt() (i.e., returns normally) should NOT trigger the
    new guard — the existing wrap-up still fires when the cap is hit
    without an interrupt."""
    from langchain_core.tools import tool
    from ryoma_ai.agent.workflow import (
        TERMINAL_PRIMITIVE_METADATA_KEY,
        ToolMode,
    )

    @tool
    def t_pause(question: str) -> str:
        """Stub: marked terminal but doesn't actually interrupt."""
        return f"answered:{question}"
    t_pause.metadata = {TERMINAL_PRIMITIVE_METADATA_KEY: True}

    agent = _build_drain_test_agent(t_pause)
    result = agent.invoke(
        question="hi",
        tool_mode=ToolMode.CONTINUOUS,
        max_iterations=2,
        display=False,
    )

    # No interrupt should fire because the tool doesn't call interrupt().
    # Either: (a) the tool ran successfully and the conversation
    # completes normally, OR (b) the cap fires and wrap-up synthesises
    # an answer. Both are acceptable; we only forbid the new guard
    # incorrectly firing.
    assert isinstance(result, dict)
    # No __interrupt__ key (tool didn't pause).
    assert not result.get("__interrupt__"), (
        f"Tool returned normally; result should not carry __interrupt__. "
        f"Got: {result.get('__interrupt__')!r}"
    )


def test_drain_guard_uses_structural_interrupts_check():
    """AST sentinel: the post-drain guard must reference ``tasks`` and
    ``interrupts`` attribute access (structural state-snapshot check),
    AND/OR ``__interrupt__`` key access on the result dict. Must NOT
    rely on substring matching of state.values content or any other
    brittle heuristic.

    Interrupts live in:
      • result['__interrupt__']  — the dict returned by graph.invoke()
      • state.tasks[i].interrupts — the persistent checkpoint view
      • state.interrupts          — top-level snapshot alias (since 0.6)
    Interrupts do NOT live in state.values; checking state.values for
    them is the wrong view.
    """
    import inspect
    from ryoma_ai.agent import workflow
    src = inspect.getsource(workflow.WorkflowAgent.invoke)

    # The guard must reference at least one of the two canonical views.
    references_task_interrupts = (
        ".interrupts" in src and ".tasks" in src
    )
    references_result_interrupt = '"__interrupt__"' in src or "'__interrupt__'" in src
    assert references_task_interrupts or references_result_interrupt, (
        "PR-B': WorkflowAgent.invoke must reference either "
        "`state.tasks[*].interrupts` or `result['__interrupt__']` after "
        "the drain block, to detect when a terminal-primitive tool "
        "paused the graph. Neither attribute access found in source."
    )
