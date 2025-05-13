# Ryoma Nicer

**A Pydantic v2–compatible fork of Ryoma AI Platform**

&#x20;&#x20;

---

## Overview

**Ryoma Nicer** is a community-maintained fork of the [Ryoma AI Platform](https://github.com/project-ryoma/ryoma) designed to ensure compatibility with **Pydantic v2**. The original Ryoma provides an AI-powered data agent framework for seamless data analysis, engineering, and visualization. This fork:

* Upgrades internal data models to use Pydantic v2 APIs
* Preserves feature parity with the upstream Ryoma project
* Offers a drop-in replacement package (`ryoma-nicer`) for users on Pydantic v2

For the original project, see [project-ryoma/ryoma](https://github.com/project-ryoma/ryoma). Thanks to the upstream maintainers for their work!

## Why This Fork?

Pydantic v2 introduced breaking changes in how models are defined and validated. Many projects, including Ryoma AI Platform, were tightly coupled to Pydantic v1. To allow developers to adopt the latest Pydantic improvements without sacrificing Ryoma functionality, this fork:

1. **Migrates all ****\`\`**** model definitions** to the new `BaseModel` API in v2.
2. **Updates validation logic** to leverage v2’s faster runtime and stricter type checks.
3. \*\*Releases under \*\*\`\` as a PyPI package, ensuring it can be installed alongside Pydantic v2.

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

See the [Ryoma Nicer documentation](https://github.com/your-username/ryoma-nicer/tree/main/docs) for full examples.

## Contribution & Upstream

* **Forked from:** [project-ryoma/ryoma](https://github.com/project-ryoma/ryoma)
* **Issue tracker & pull requests:** Please use this repository to report Pydantic v2 compatibility issues or propose improvements.

## License

This project is released under the [Apache License 2.0](LICENSE).
 
