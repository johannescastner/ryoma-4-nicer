from typing import Dict, Optional, List
from ryoma_ai.vector_store.base import VectorStore
from ryoma_ai.agent.workflow import WorkflowAgent
from ryoma_ai.tool.sql_tool import CreateTableTool, QueryProfileTool, SqlQueryTool
from langchain_community.utilities import SQLDatabase 


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

        # if the caller passed in a SQLDatabase, bind it right away:
        if sql_db:
            for t in self.tools:
                if isinstance(t, SqlQueryTool):
                    t.datasource = sql_db               
                    break
