"""Regression tests for ``WorkflowAgent._format_messages``.

The method constructs the ``BaseMessage`` instances that become the
**initial state seed** at ``self.workflow.invoke(messages, config)``
(workflow.py:225). LangGraph's ``add_messages`` reducer
(``langgraph/graph/message.py``) auto-assigns UUIDs to messages added
through state UPDATES — but the initial invocation seed bypasses the
reducer. So messages constructed here MUST carry their own ``id`` or
downstream consumers like ``langmem._preprocess_messages``
(``langmem/short_term/summarization.py:171``) raise:

    ValueError: Messages are required to have ID field.

This bug was surfaced empirically by the IntellAgent eval (2026-05-06):
five user-facing scenario failures all rooted in this construction
site. Fixed in 0.8.0.
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.prompt_values import ChatPromptValue

from ryoma_ai.agent.workflow import WorkflowAgent


@pytest.fixture
def fake_agent():
    """A ``MagicMock(spec=WorkflowAgent)`` that lets us call
    ``WorkflowAgent._format_messages`` as an unbound method without
    constructing a full agent (which requires tools, model, graph,
    etc.). We only stub the two ``self`` methods ``_format_messages``
    actually calls (``get_current_state`` and ``get_current_tool_calls``).
    """
    return MagicMock(spec=WorkflowAgent)


def test_format_messages_assigns_id_to_human_message(fake_agent):
    """Default branch (no pending tool call): the constructed
    ``HumanMessage`` MUST have a non-None ``id``."""
    state = MagicMock()
    state.next = ()  # not in the tools branch
    fake_agent.get_current_state.return_value = state

    result = WorkflowAgent._format_messages(fake_agent, "hello")

    assert isinstance(result, ChatPromptValue)
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert isinstance(msg, HumanMessage)
    assert msg.content == "hello"
    assert msg.id is not None
    assert isinstance(msg.id, str) and len(msg.id) > 0


def test_format_messages_assigns_id_to_tool_denial_message(fake_agent):
    """Tools branch (pending tool call, user redirects): the
    ``ToolMessage`` denying the pending call MUST have a non-None
    ``id``."""
    state = MagicMock()
    state.next = ("tools",)
    fake_agent.get_current_state.return_value = state
    fake_agent.get_current_tool_calls.return_value = [
        {"id": "call_xyz", "name": "f", "args": {}}
    ]

    result = WorkflowAgent._format_messages(fake_agent, "actually nevermind")

    assert isinstance(result, ChatPromptValue)
    assert len(result.messages) == 1
    msg = result.messages[0]
    assert isinstance(msg, ToolMessage)
    assert msg.tool_call_id == "call_xyz"
    assert "actually nevermind" in msg.content
    assert msg.id is not None
    assert isinstance(msg.id, str) and len(msg.id) > 0


def test_format_messages_ids_are_unique(fake_agent):
    """Distinct invocations produce distinct ids. Catches anyone
    accidentally swapping the uuid for a constant or for a content
    hash that would collide on duplicate user input."""
    state = MagicMock()
    state.next = ()
    fake_agent.get_current_state.return_value = state

    r1 = WorkflowAgent._format_messages(fake_agent, "hello")
    r2 = WorkflowAgent._format_messages(fake_agent, "hello")

    assert r1.messages[0].id != r2.messages[0].id
