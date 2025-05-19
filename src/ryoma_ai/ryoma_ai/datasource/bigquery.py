import logging
from typing import Optional, Type, Any

import ibis
from ibis import BaseBackend
from ryoma_ai.datasource.base import SqlDataSource

# Amundsen imports for metadata pipeline
from databuilder.loader.base_loader import Loader
from databuilder.task.task import DefaultTask
from databuilder.job.job import DefaultJob
from databuilder.publisher.neo4j_csv_publisher import Neo4jCsvPublisher

# Our custom Dataplex extractor & publisher
from ryoma_ai.datasource.dataplex import DataplexMetadataExtractor, DataplexPublisher

class BigQueryDataSource(SqlDataSource):
    def __init__(
        self,
        project_id: str,
        dataset_id: Optional[str] = None,
        credentials: Optional[Any] = None,
        *,
        metadata_extractor_cls: Type = DataplexMetadataExtractor,
        metadata_publisher_cls: Type = DataplexPublisher,
    ):
        """
        A BigQuery data source that by default crawls metadata via Google Dataplex,
        but can fall back to Amundsen BigQuery extractor + Neo4j publisher if desired.
        """
        # Tell the SqlDataSource base which 'database' (dataset) to use
        super().__init__(database=dataset_id)
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.credentials = credentials

        # Pluggable extractor & publisher
        self._extractor_cls = metadata_extractor_cls
        self._publisher_cls = metadata_publisher_cls

    def _connect(self, **kwargs) -> BaseBackend:
        connect_args: dict[str, Any] = {"project_id": self.project_id, **kwargs}
        if self.dataset_id:
            connect_args["dataset_id"] = self.dataset_id
        if self.credentials:
            connect_args["credentials"] = self.credentials

        logging.info("Connecting to BigQuery with %r", connect_args)
        return ibis.bigquery.connect(**connect_args)

    def crawl_catalogs(self, loader: Loader, where_clause_suffix: Optional[str] = ""):
        """
        Dynamically discover all datasets/tables/columns by:
          1) instantiating the configured metadata extractor
          2) instantiating the configured metadata publisher
          3) running the Amundsen-style DefaultJob pipeline
        """
        logging.info(
            "Crawling data catalog from BigQuery using %s",
            self._extractor_cls.__name__,
        )

        # 1) build extractor
        extractor = self._extractor_cls(
            project_id=self.project_id,
            credentials=self.credentials,
        )

        # 2) build publisher
        publisher = self._publisher_cls()

        # 3) launch the standard Amundsen load pipeline
        task = DefaultTask(extractor=extractor, loader=loader)
        job = DefaultJob(conf={}, task=task, publisher=publisher)
        job.launch()

    def get_query_plan(self, query: str):  # noqa: N802
        """
        BigQuery supports EXPLAIN; return ibis Table for profiling.
        """
        conn = self.connect()
        return conn.sql(f"EXPLAIN {query}")


class BigqueryDataSource(BigQueryDataSource):
    """
    Deprecated alias for backwards compatibility.
    """
    def __init__(self, *args, **kwargs):
        import warnings
        warnings.warn(
            "BigqueryDataSource is deprecated; please use BigQueryDataSource instead",
            DeprecationWarning,
        )
        super().__init__(*args, **kwargs)
