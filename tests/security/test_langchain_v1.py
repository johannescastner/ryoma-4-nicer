"""The maintained agent library runs without notebook-only provider discovery."""

import importlib.util

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from ryoma_ai.agent.sql import SqlAgent
from ryoma_ai.llm.provider import load_model_provider


class ToolCallingModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def test_sql_agent_constructs_and_runs_with_a_native_provider_object():
    assert importlib.util.find_spec("jupyter_ai_magics") is None
    model = ToolCallingModel(responses=[AIMessage(content="ready")])
    agent = SqlAgent(model=model)
    response = agent.invoke("hello", display=False)
    assert response["messages"][-1].content == "ready"
    assert response["messages"][0].content == "hello"


@pytest.mark.parametrize("tool_mode", ["disallowed", "continuous"])
def test_intentionally_stateless_agent_public_helpers_and_invoke(tool_mode):
    from langgraph.graph import END, START, StateGraph

    from ryoma_ai.states import MessageState

    class StatelessAgent(SqlAgent):
        def _build_workflow(self, graph):
            workflow = StateGraph(MessageState)
            workflow.add_node("agent", self.call_model)
            workflow.add_edge(START, "agent")
            workflow.add_edge("agent", END)
            return workflow.compile()

    agent = StatelessAgent(
        model=ToolCallingModel(responses=[AIMessage(content="ready")])
    )
    assert agent.get_current_state() is None
    assert agent.get_current_state_messages() == []
    assert agent.get_current_tool_calls() == []
    response = agent.invoke("hello", tool_mode=tool_mode, display=False)
    assert response["messages"][-1].content == "ready"


def test_chat_provider_uses_native_registry_without_an_api_call():
    from langchain_openai import ChatOpenAI

    parameters = {"api_key": "software-test-only", "temperature": 0}
    model = load_model_provider("openai:gpt-4o-mini", model_parameters=parameters)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-4o-mini"
    assert parameters == {"api_key": "software-test-only", "temperature": 0}


@pytest.mark.parametrize(
    "identifier", ["openai:gpt-4o-mini", "openai-chat:gpt-4o-mini"]
)
def test_published_openai_chat_alias_constructs_the_same_native_model(identifier):
    from langchain_openai import ChatOpenAI

    model = load_model_provider(
        identifier, model_parameters={"api_key": "software-test-only"}
    )
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-4o-mini"


@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("bedrock-chat:anthropic.claude-v2:1", "bedrock:anthropic.claude-v2:1"),
        ("azure-chat-openai:deployment", "azure_openai:deployment"),
        ("nvidia-chat:model", "nvidia:model"),
        ("gemini:model", "google_genai:model"),
        ("huggingface_hub:organization/model", "huggingface:organization/model"),
        ("togetherai:organization/model", "together:organization/model"),
        ("mistralai:model", "mistralai:model"),
    ],
)
def test_legacy_provider_prefixes_preserve_the_exact_model_identifier(
    monkeypatch, identifier, expected
):
    import langchain.chat_models

    calls = []
    monkeypatch.setattr(
        langchain.chat_models,
        "init_chat_model",
        lambda model, **kwargs: calls.append(model),
    )
    load_model_provider(identifier)
    assert calls == [expected]


def test_embedding_provider_uses_native_registry_without_an_api_call():
    from langchain_openai import OpenAIEmbeddings

    model = load_model_provider(
        "openai:text-embedding-3-small", "embedding", {"api_key": "software-test-only"}
    )
    assert isinstance(model, OpenAIEmbeddings)
    assert model.model == "text-embedding-3-small"


def test_unknown_model_kind_fails_with_an_actionable_error():
    with pytest.raises(ValueError, match="use chat or embedding"):
        load_model_provider("unused", "unrecognized")
