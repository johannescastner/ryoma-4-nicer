import logging
from typing import Optional

import ibis
from databuilder.job.job import DefaultJob
from databuilder.loader.base_loader import Loader
from databuilder.task.task import DefaultTask
from ibis import BaseBackend
from pyhocon import ConfigFactory
from ryoma_ai.datasource.base import SqlDataSource


class BigQueryDataSource(SqlDataSource):
    def __init__(
        self,
        project_id: str,
        dataset_id: Optional[str] = None,
        credentials: Optional[str] = None,
    ):
        # Tell the SqlDataSource base which 'database' (i.e. dataset) to use.
        super().__init__(database=dataset_id)
        self.project_id  = project_id
        self.dataset_id  = dataset_id
        self.credentials = credentials

    def _connect(self, **kwargs) -> BaseBackend:
        connect_args: dict[str, Any] = {"project_id": self.project_id, **kwargs}
        if self.dataset_id:
            connect_args["dataset_id"] = self.dataset_id
        if self.credentials:
            connect_args["credentials"] = self.credentials

        logging.info("Connecting to BigQuery with %r", connect_args)
        return ibis.bigquery.connect(**connect_args)

    def crawl_catalogs(self, loader: Loader, where_clause_suffix: Optional[str] = ""):
        from databuilder.extractor.bigquery_metadata_extractor import (
            BigQueryMetadataExtractor,
        )

        logging.info("Crawling data catalog from Bigquery")
        job_config = ConfigFactory.from_dict(
            {
                "extractor.bigquery_table_metadata.{}".format(
                    BigQueryMetadataExtractor.PROJECT_ID_KEY
                )
            }
        )
        job = DefaultJob(
            conf=job_config,
            task=DefaultTask(extractor=BigQueryMetadataExtractor(), loader=loader),
        )

        job.launch()

    def get_query_plan(self, query: str):                 # noqa: N802
        """
        BigQuery supports `EXPLAIN`.  We return an ibis Table so Ryoma
        can pretty-print it or inspect cost estimates.
        """
        conn = self.connect()
        return conn.sql(f"EXPLAIN {query}")

class BigqueryDataSource(BigQueryDataSource):
    def __init__(self, *args, **kwargs):
        import warnings
        warnings.warn(
            "BigqueryDataSource is deprecated; please use BigQueryDataSource instead",
            DeprecationWarning,
        )
        super().__init__(*args, **kwargs)
