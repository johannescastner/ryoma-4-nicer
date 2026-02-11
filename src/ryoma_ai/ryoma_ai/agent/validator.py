import logging
from typing import Dict, List, Literal, Optional, Type, Union

from langchain_core.messages import ToolMessage
from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableSerializable
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ValidationError

from ryoma_ai.agent.workflow import WorkflowAgent
from ryoma_ai.states import MessageState

_logger = logging.getLogger(__name__)

# Hard cap on validation retries to prevent runaway LLM loops.
_MAX_VALIDATION_RETRIES: int = 3


class ValidatorAgent(WorkflowAgent):
    """A WorkflowAgent with an LLM-powered validation loop.

    Graph topology::

        agent → [_should_call_tool] → tools → agent       (tool loop)
                                    → validator            (validation)
                                    → END                  (done)

        validator → [_should_validate] → validation → validator  (retry)
                                       → END                     (done)

    Subclasses override:
        - ``_needs_validation(state)``  to control when validation fires
        - ``state_schema``              to use a richer state dict
        - ``_interrupt_before``         to control interactive breakpoints
    """

    # ── Subclass configuration points ──────────────────────────
    state_schema = MessageState
    _interrupt_before: Optional[List[str]] = ["validator", "tools"]

    def __init__(
        self,
        validator: Type[BaseModel],
        model: Union[RunnableSerializable, str],
        model_parameters: Optional[Dict] = None,
        validator_chain: Optional[RunnableSerializable] = None,
        tools: Optional[List[BaseTool]] = None,
        base_prompt_template: Optional[PromptTemplate] = None,
        **kwargs,
    ):
        # ── Store validator schema + chain ─────────────────────
        self.validator: Type[BaseModel] = validator
        self.validator_tool: Type[BaseModel] = validator  # backward compat
        self.validator_chain: Optional[RunnableSerializable] = validator_chain

        # ── Separate actual tools from validator schema ────────
        self._actual_tools: List[BaseTool] = list(tools or [])
        self._actual_tool_names: set = {
            t.name for t in self._actual_tools
        }
        self._validator_schema_name: str = validator.__name__

        # For bind_tools: both real tools + validator schema
        all_bindable = list(self._actual_tools) + [validator]

        # ── Default prompt if none provided ────────────────────
        if base_prompt_template is None:
            base_prompt_template = PromptTemplate(
                template=(
                    "Please return the output given messages and "
                    "following the feature validation\n{messages}"
                ),
                input_variables=["messages"],
            )

        # ── Call WorkflowAgent.__init__ ────────────────────────
        super().__init__(
            all_bindable,
            model,
            model_parameters,
            base_prompt_template=base_prompt_template,
            **kwargs,
        )

        # Override self.tools (set by WorkflowAgent) to expose only
        # real BaseTool instances.  Prevents call_tool() and external
        # callers from seeing the Pydantic validator schema.
        self.tools = list(self._actual_tools)

    # ──────────────────────────────────────────────────────────────
    # Graph construction
    # ──────────────────────────────────────────────────────────────

    def _build_workflow(self, graph=None):
        """Build the agent → tools → validator → validation graph.

        ``graph`` is accepted for WorkflowAgent.__init__ compat but ignored.
        """
        workflow = StateGraph(self.state_schema)

        workflow.add_node("agent", self.call_model)
        workflow.add_node("tools", self.build_tool_node(self._actual_tools))
        workflow.add_node("validator", self.call_model)
        workflow.add_node("validation", self._validate)

        workflow.add_conditional_edges("agent", self._should_call_tool)
        workflow.add_conditional_edges("validator", self._should_validate)
        workflow.add_edge("tools", "agent")
        workflow.add_edge("validation", "validator")

        workflow.set_entry_point("agent")
        return workflow.compile(
            checkpointer=self.memory,
            interrupt_before=self._interrupt_before,
        )

    # ──────────────────────────────────────────────────────────────
    # Graph nodes
    # ──────────────────────────────────────────────────────────────

    def call_model(self, state: dict, config: RunnableConfig):
        chain = self._build_chain()
        response = chain.invoke(state, config)
        return {"messages": [response]}

    def _validate(self, state: dict, config: RunnableConfig):
        """Parse the last message as structured output, then run the
        validator chain to produce a validated response.

        On failure, returns a *tagged* ``ToolMessage`` so that
        ``_should_validate`` can distinguish validation errors from
        real tool results when computing scope boundaries and retry
        counts.
        """
        # Configuration error — must propagate, NOT be caught by the
        # broad except Exception below (Bug G).
        if self.validator_chain is None:
            raise ValueError(
                "validator_chain is not set. Pass it to __init__ "
                "or set self.validator_chain in a subclass."
            )

        message = state["messages"][-1]
        try:
            parser = PydanticToolsParser(tools=[self.validator])
            parser.invoke(message)

            response = self.validator_chain.invoke(state, config)

            # Mark as validated so _should_validate can route to END.
            if hasattr(response, "additional_kwargs"):
                response.additional_kwargs["validated"] = True
            return {"messages": [response]}

        except ValidationError as e:
            # Schema validation failed — structured retry feedback
            return {
                "messages": [
                    self._make_validation_error_message(
                        (
                            f"{repr(e)}\n\n"
                            "Pay close attention to the function feature.\n\n"
                            "Respond by fixing all validation errors."
                        ),
                        message,
                    )
                ]
            }

        except Exception as e:
            # Parser errors, chain timeouts, rate limits, etc.
            _logger.warning(
                "[ValidatorAgent] _validate failed with %s: %s",
                type(e).__name__,
                e,
            )
            return {
                "messages": [
                    self._make_validation_error_message(
                        (
                            f"Validation error ({type(e).__name__}): {e}\n\n"
                            "Please retry with a corrected response."
                        ),
                        message,
                    )
                ]
            }

    def _make_validation_error_message(
        self, content: str, source_message
    ) -> ToolMessage:
        """Create a ToolMessage tagged as a validation error.

        The ``is_validation_error`` tag allows ``_should_validate`` to
        distinguish these from real tool-execution results when
        computing scope boundaries and retry counts.
        """
        return ToolMessage(
            content=content,
            tool_call_id=self._extract_tool_call_id(source_message),
            additional_kwargs={"is_validation_error": True},
        )

    @staticmethod
    def _extract_tool_call_id(message) -> str:
        """Safely extract tool_call_id from a message, or return a fallback."""
        if hasattr(message, "tool_calls") and message.tool_calls:
            return message.tool_calls[0]["id"]
        return "validation_error"

    # ──────────────────────────────────────────────────────────────
    # Graph routing
    # ──────────────────────────────────────────────────────────────

    def _should_call_tool(
        self, state: dict
    ) -> Literal["tools", "validator", "__end__"]:
        if isinstance(state, list):
            ai_message = state[-1]
        elif messages := state.get("messages", []):
            ai_message = messages[-1]
        else:
            raise ValueError(
                f"No messages found in input state to tool_edge: {state}"
            )

        if hasattr(ai_message, "tool_calls") and ai_message.tool_calls:
            # Distinguish real tool calls from validator schema calls.
            call_names = {tc["name"] for tc in ai_message.tool_calls}
            if call_names & self._actual_tool_names:
                return "tools"
            # All calls target the validator schema — route to validation
            if self._needs_validation(state):
                return "validator"
            return END

        # No tool calls
        if self._needs_validation(state):
            return "validator"
        return END

    def _needs_validation(self, state: dict) -> bool:
        """Whether the current state requires validation.

        Default: always validate.  Subclasses override to validate
        selectively (e.g. only after chart creation).
        """
        return True

    def _should_validate(
        self, state: dict
    ) -> Literal["validation", "__end__"]:
        """Decide whether to (re)validate or exit the validation loop.

        Scope rules:
        - A **real** ``ToolMessage`` (from tool execution) starts a new
          validation scope.  Validation errors from ``_validate`` are
          tagged with ``is_validation_error`` and do NOT start new scopes.
        - The "validated" flag and retry counter are checked within the
          current scope only.  This ensures that chart-1's validation
          does not leak into chart-2's scope, while also allowing the
          retry counter to see ALL attempts within one validation cycle.
        """
        messages = state.get("messages", [])
        if not messages:
            return END

        # ── Collect messages in current validation scope ───────
        # Walk backward until we hit a REAL tool message (one that
        # is NOT a validation error).  Validation-error ToolMessages
        # are transparent to scope boundaries.
        recent_msgs: list = []
        for msg in reversed(messages):
            recent_msgs.append(msg)
            if isinstance(msg, ToolMessage):
                is_val_error = (
                    hasattr(msg, "additional_kwargs")
                    and msg.additional_kwargs.get("is_validation_error")
                )
                if not is_val_error:
                    # Real tool result — this is the scope boundary
                    break

        # ── Already validated in this scope? ───────────────────
        for msg in recent_msgs:
            if (
                hasattr(msg, "additional_kwargs")
                and msg.additional_kwargs.get("validated")
            ):
                return END

        # ── Retry limit ────────────────────────────────────────
        error_count = sum(
            1
            for msg in recent_msgs
            if isinstance(msg, ToolMessage)
            and hasattr(msg, "additional_kwargs")
            and msg.additional_kwargs.get("is_validation_error")
        )
        if error_count >= _MAX_VALIDATION_RETRIES:
            _logger.warning(
                "[ValidatorAgent] Validation retry limit (%d) reached; "
                "exiting validation loop.",
                _MAX_VALIDATION_RETRIES,
            )
            return END

        # ── Route based on last message ────────────────────────
        last_msg = messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "validation"
        return END