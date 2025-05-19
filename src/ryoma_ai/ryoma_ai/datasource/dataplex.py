# src/ryoma_ai/ryoma_ai/datasource/dataplex.py

import logging
from typing import Iterator, Any, Dict

from pyhocon import ConfigTree
from databuilder.extractor.base_extractor import Extractor
from databuilder.task.task import DefaultTask
from databuilder.job.job import DefaultJob
from databuilder.models.table_metadata import ColumnMetadata, TableMetadata
from databuilder.publisher.base_publisher import Publisher
from ryoma_ai.datasource.dataplex_loader import DataplexLoader

from google.cloud import dataplex_v1
from google.protobuf import struct_pb2

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
        self.catalog = dataplex_v1.CatalogServiceClient()
        self.location = conf.get_string("gcp_location", "eu-west1")
        self.project = conf.get_string("project_id")

    def publish(self, records: Iterator[TableMetadata]) -> None:
        parent = f"projects/{self.project}/locations/{self.location}"
        for tbl in records:
            eg_id = tbl.database
            eg_name = f"{parent}/entryGroups/{eg_id}"
            try:
                self.catalog.get_entry_group(name=eg_name)
            except Exception:
                self.catalog.create_entry_group(
                    parent=parent,
                    entry_group_id=eg_id,
                    entry_group=dataplex_v1.EntryGroup(display_name=eg_id),
                )

            # Build schema aspect
            schema_struct = struct_pb2.Struct(
                fields={
                    "columns": struct_pb2.Value(
                        list_value=struct_pb2.ListValue(values=[
                            struct_pb2.Value(struct_value=struct_pb2.Struct(
                                fields={
                                    "name": struct_pb2.Value(string_value=c.name),
                                    "type": struct_pb2.Value(string_value=c.col_type or ""),
                                    "description": struct_pb2.Value(string_value=c.description or ""),
                                }
                            )) for c in tbl.columns
                        ])
                    )
                }
            )

            entry = dataplex_v1.Entry(
                entry_type="projects/dataplex-types/locations/global/entryTypes/generic",
                entry_source=dataplex_v1.EntrySource(description=tbl.description[:250]),
                aspects={
                    "dataplex-types.global.generic": dataplex_v1.Aspect(
                        aspect_type="projects/dataplex-types/locations/global/aspectTypes/generic",
                        data=schema_struct
                    )
                },
            )

            try:
                self.catalog.get_entry(name=f"{eg_name}/entries/{tbl.name}")
                self.catalog.update_entry(entry=entry)
            except Exception:
                self.catalog.create_entry(parent=eg_name, entry=entry, entry_id=tbl.name)


def crawl_with_dataplex(conf: ConfigTree) -> None:
    """
    Convenience: run the extractor → loader → publisher pipeline end‑to‑end.
    """
    extractor = DataplexMetadataExtractor()
    extractor.init(conf)

    loader = DataplexLoader()          # <-- concrete subclass
    loader.init(conf)                  # <-- initialise it once

    publisher = DataplexPublisher()
    publisher.init(conf)
    task = DefaultTask(extractor=extractor, loader=loader)
    
    job = DefaultJob(conf=conf, task=task, publisher=publisher)
    job.launch()
