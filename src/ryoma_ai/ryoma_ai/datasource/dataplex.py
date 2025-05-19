# src/ryoma_ai/ryoma_ai/datasource/dataplex.py

import logging
from typing import Iterator, Any, Dict

from pyhocon import ConfigTree
from databuilder.extractor.base_extractor import Extractor
from databuilder.loader.base_loader import Loader
from databuilder.task.task import DefaultTask
from databuilder.job.job import DefaultJob
from databuilder.models.table_metadata import ColumnMetadata, TableMetadata
from databuilder.publisher.base_publisher import Publisher

from google.cloud import dataplex_v1

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


class DataplexMetadataExtractor(Extractor):
    """
    Extract metadata from Cloud Dataplex:
      - list Lakes → Zones → Assets
      - for TABLE/STREAM assets, pull out schema fields
      - emit TableMetadata(ColumnMetadata…) for each table
    """

    def init(self, conf: ConfigTree) -> None:
        project = conf.get_string("project_id")
        # Dataplex Content API for listing assets
        self.content_client = dataplex_v1.ContentServiceClient()
        # Parent path covers all locations: projects/{project}/locations/-
        self.parent = f"projects/{project}/locations/-"
        self._iter = self._iterate_tables()

    def _iterate_tables(self) -> Iterator[TableMetadata]:
        lakes = dataplex_v1.LakesClient().list_lakes(parent=self.parent).lakes
        for lake in lakes:
            zones = dataplex_v1.ZonesClient().list_zones(parent=lake.name).zones
            for zone in zones:
                assets = dataplex_v1.AssetsClient().list_assets(parent=zone.name).assets
                for asset in assets:
                    typ = asset.resource_spec.type_
                    if typ not in ("TABLE", "STREAM"):
                        continue
                    schema = asset.resource_spec.schema
                    cols = [
                        ColumnMetadata(
                            name=field.name,
                            col_type=field.type_,
                            description=field.description or "",
                            sort_order=i,
                        )
                        for i, field in enumerate(schema.fields)
                    ]
                    yield TableMetadata(
                        database=zone.name.split("/")[-1],
                        cluster=lake.name.split("/")[-1],
                        schema=zone.name.split("/")[-1],
                        name=asset.resource_spec.name,
                        description=asset.description or "",
                        columns=cols,
                        is_view=False,
                    )

    def extract(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            return None

    def get_scope(self) -> str:
        return "extractor.dataplex_metadata"


class DataplexPublisher(Publisher):
    """
    Publish TableMetadata back into Cloud Data Catalog:
      - ensures an EntryGroup per dataset
      - upserts a TABLE‑typed Entry with schema
    """

    def init(self, conf: ConfigTree) -> None:
        self.dc = datacatalog_v1.DataCatalogClient()
        self.location = conf.get_string("gcp_location", "us-central1")
        self.project = conf.get_string("project_id")

    def publish(self, records: Iterator[TableMetadata]) -> None:
        parent = f"projects/{self.project}/locations/{self.location}"
        for tbl in records:
            eg_id = tbl.database
            eg_name = f"{parent}/entryGroups/{eg_id}"
            try:
                self.dc.get_entry_group(name=eg_name)
            except Exception:
                self.dc.create_entry_group(
                    parent=parent,
                    entry_group_id=eg_id,
                    entry_group={"display_name": eg_id},
                )

            entry_id = tbl.name
            entry = datacatalog_v1.Entry(
                display_name=tbl.name,
                type_=datacatalog_v1.EntryType.TABLE,
                schema=datacatalog_v1.Schema(
                    columns=[
                        datacatalog_v1.ColumnSchema(
                            column=col.name,
                            type_=col.col_type or "",
                            description=col.description or "",
                        )
                        for col in tbl.columns
                    ]
                ),
            )
            try:
                existing = self.dc.get_entry(name=f"{eg_name}/entries/{entry_id}")
                self.dc.update_entry(entry=entry)
            except Exception:
                self.dc.create_entry(
                    parent=eg_name,
                    entry_id=entry_id,
                    entry=entry,
                )


def crawl_with_dataplex(conf: ConfigTree) -> None:
    """
    Convenience: run the extractor → loader → publisher pipeline end‑to‑end.
    """
    extractor = DataplexMetadataExtractor()
    extractor.init(conf)

    loader = Loader()
    task = DefaultTask(extractor=extractor, loader=loader)

    publisher = DataplexPublisher()
    publisher.init(conf)

    job = DefaultJob(conf=conf, task=task, publisher=publisher)
    job.launch()
