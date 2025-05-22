from typing import Dict, Optional, List

from ryoma_ai.agent.workflow import WorkflowAgent
from ryoma_ai.tool.sql_tool import CreateTableTool, QueryProfileTool, SqlQueryTool


class SqlAgent(WorkflowAgent):
    description: str = (
        "A SQL agent that can use SQL Tools to interact with SQL schemas."
    )

    def __init__(
        self,
        model: str,
        model_parameters: Optional[Dict] = None,
        tools: Optional[List] = None,  # allow injection of additional tools
    ):
        # core SQL capabilities
        base_tools = [SqlQueryTool(), CreateTableTool(), QueryProfileTool()]
        tools = base_tools + (tools or [])

        # delegate to WorkflowAgent
        super().__init__(tools, model, model_parameters)
