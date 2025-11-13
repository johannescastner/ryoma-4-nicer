# Ryoma Nicer

**A Pydantic v2–compatible fork of Ryoma AI Platform**

&#x20;&#x20;

---

## Overview

**Ryoma Nicer** is a community-maintained fork of the
[Ryoma AI Platform](https://github.com/project-ryoma/ryoma), originally created
by the Ryoma team. The upstream project provides an AI-powered data agent
framework for seamless data analysis, engineering, and visualization.

This fork is:

* **Pydantic v2–compatible** (`ryoma-nicer` runs on Pydantic 2.x)
* **LangGraph-ready**, updated for modern LangGraph (`StateGraph` + `Pregel`) APIs
* **Actively maintained** as part of the “NICER” suite of agents (e.g. baby-NICER)

It is used in production within the NICER stack to power agents such as
`WorkflowAgent` and SQL agents that run on a LangGraph / LangChain / LangMem
tooling ecosystem.

For the original project, see [project-ryoma/ryoma](https://github.com/project-ryoma/ryoma).
All architectural credit for the base design goes to the upstream maintainers.

## Why This Fork?

Pydantic v2 introduced breaking changes in how models are defined and validated.
Many projects, including the original Ryoma AI Platform, were tightly coupled
to Pydantic v1. To allow developers to adopt the latest Pydantic improvements
without sacrificing Ryoma functionality, this fork:

1. **Migrates all core model definitions** to the Pydantic v2 `BaseModel` API.
2. **Updates validation and config patterns** to leverage v2’s faster runtime and
   stricter type checking.
3. **Aligns with a modern agent stack**, including:
   - LangGraph (tested with `langgraph==0.6.x`)
   - LangChain / LangChain Core (tested with `langchain==0.3.x`,
     `langchain-core==0.3.x`)
   - Usage inside the NICER suite of agents (e.g. baby-NICER / langgraph-slack).


## Installation

Install the Pydantic v2–compatible release from PyPI:

```bash
pip install ryoma-nicer
```

Optionally, include supported data source extras:

```bash
pip install "ryoma-nicer[snowflake,pyspark,postgres,sqlite,mysql,bigquery]"
```

## Usage

The API surface and usage mirror the original Ryoma. For example, to run a simple SQL agent:

```python
from ryoma_ai.agent.sql import SqlAgent
from ryoma_ai.datasource.postgres import PostgresDataSource

# Initialize data source
datasource = PostgresDataSource("postgresql://user:pass@host:5432/db")

# Create and run SQL agent
agent = SqlAgent("gpt-3.5-turbo").add_datasource(datasource)
agent.stream("SELECT count(*) FROM orders", display=True)
```

For conceptual details and additional examples, the
[upstream Ryoma documentation](https://github.com/project-ryoma/ryoma) still
applies in most cases. Where this fork differs is primarily in:

* Pydantic v2 model definitions
* Updated integrations used by the NICER agent stack

### Usage in the NICER agent suite

Within the NICER ecosystem (e.g. baby-NICER / `langgraph-slack`), this fork
powers agents that run on LangGraph’s compiled graphs (`StateGraph.compile(...)`
returning a `Pregel` runtime). For example, `WorkflowAgent`:

* builds a `StateGraph` over `MessageState`
* compiles it to a `Pregel` runtime with `MemorySaver` checkpointers
* uses LangGraph’s `invoke`/`stream` APIs to manage tool-augmented workflows

This ensures `ryoma-nicer` works cleanly with modern LangGraph-based systems
while preserving the spirit of the original Ryoma design.

## Contribution & Upstream

* **Forked from:** [project-ryoma/ryoma](https://github.com/project-ryoma/ryoma)
* **Upstream credit:** Architectural ideas, core abstractions, and much of the
  original code come from the Ryoma team.
* **Issue tracker & pull requests:** Please use this repository to report
  Pydantic v2 / modern-stack compatibility issues or to propose improvements
  needed by the NICER suite of agents.

## License

This project is released under the [Apache License 2.0](LICENSE).
 
