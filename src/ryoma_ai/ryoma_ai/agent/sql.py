from typing import Dict, List, Optional

from langchain_community.utilities import SQLDatabase

from ryoma_ai.agent.workflow import WorkflowAgent
from ryoma_ai.tool.sql_tool import CreateTableTool, QueryProfileTool, SqlQueryTool
from ryoma_ai.vector_store.base import VectorStore


class SqlAgent(WorkflowAgent):
    description: str = (
        "A SQL agent that can use SQL Tools to interact with SQL schemas."
    )

    def __init__(
        self,
        model: str,
        model_parameters: Optional[Dict] = None,
        sql_db: Optional[SQLDatabase] = None,
        tools: Optional[List] = None,  # allow injection of additional tools
        vector_store: Optional[VectorStore] = None,
    ):
        # core SQL capabilities
        base_tools = [SqlQueryTool(), CreateTableTool(), QueryProfileTool()]
        tools = base_tools + (tools or [])

        # delegate to WorkflowAgent
        super().__init__(tools, model, model_parameters, vector_store=vector_store)

        # If the caller passed in a SQLDatabase, register it as a
        # datasource. The new ``add_datasource`` override on
        # WorkflowAgent then walks every tool that declares a
        # ``datasource`` Pydantic field (SqlQueryTool, QueryProfileTool,
        # CreateTableTool, QueryPlanTool, ...) and assigns the
        # datasource. Replaces the previous SqlQueryTool-only special
        # case (kept only because the old ``_bind_tools`` gate was dead
        # code; now the gate works for every SqlDataSourceTool).
        if sql_db:
            self.add_datasource(sql_db)
