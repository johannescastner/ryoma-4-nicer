# src/ryoma_ai/ryoma_ai/datasource/dataplex.py

import logging
from typing import Iterator, Any, Dict

from pyhocon import ConfigTree
from databuilder.extractor.base_extractor import Extractor
from databuilder.task.task import DefaultTask
from databuilder.job.job import DefaultJob
from databuilder.models.table_metadata import ColumnMetadata, TableMetadata
from databuilder.publisher.base_publisher import Publisher


from google.cloud import dataplex_v1
from google.protobuf import struct_pb2

#–– magic identifiers for the “generic” table entry and aspect in Dataplex Catalog:
ENTRY_TYPE  = "projects/dataplex-types/locations/global/entryTypes/generic"
ASPECT_TYPE = "projects/dataplex-types/locations/global/aspectTypes/generic"

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
        # pick up explicit credentials if provided, else fallback to ADC
        self.creds = conf.get("credentials", None)
        # Dataplex Content API for listing assets
        self.client = dataplex_v1.DataplexServiceClient(
            credentials=self.creds
        )
        # Parent path covers all locations: projects/{project}/locations/-
        self.parent = f"projects/{project}/locations/-"
        self._iter = self._iterate_tables()

    def _iterate_tables(self) -> Iterator[TableMetadata]:
        # use a single service client to walk through lakes → zones → assets
        for lake in self.client.list_lakes(request={"parent": self.parent}):
            for zone in self.client.list_zones(request={"parent": lake.name}):
                for asset in self.client.list_assets(request={"parent": zone.name}):
                    if asset.resource_spec.type_ not in ("TABLE", "STREAM"):
                        continue

                    cols = [
                        ColumnMetadata(
                            name=field.name,
                            col_type=field.type_,
                            description=field.description or "",
                            sort_order=i,
                        )
                        for i, field in enumerate(asset.resource_spec.schema.fields)
                    ]

                    yield TableMetadata(
                        database   = zone.name.split("/")[-1],
                        cluster    = lake.name.split("/")[-1],
                        schema     = zone.name.split("/")[-1],
                        name       = asset.resource_spec.name,
                        description=asset.description or "",
                        columns    = cols,
                        is_view    = False,
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
        self.creds = conf.get("credentials", None)
        if self.creds:
            self.catalog = dataplex_v1.CatalogServiceClient(credentials=self.creds)
        else:
            self.catalog = dataplex_v1.CatalogServiceClient()
        self.location = conf.get_string("gcp_location", "eu-west1")
        self.project = conf.get_string("project_id")
        self.logger = LOGGER

    def get_scope(self) -> str:
        return "publisher.dataplex_metadata"

    def publish_impl(self, records: Iterator[TableMetadata]) -> None:
        parent = f"projects/{self.project}/locations/{self.location}"
        for tbl in records:
            try:
                eg_id = tbl.database
                eg_name = f"{parent}/entryGroups/{eg_id}"
                # ensure entry group exists
                try:
                    self.catalog.get_entry_group(name=eg_name)
                except Exception:
                    self.catalog.create_entry_group(
                        parent=parent,
                        entry_group_id=eg_id,
                        entry_group=dataplex_v1.EntryGroup(display_name=eg_id),
                    )

                # build schema aspect
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
                    entry_type=ENTRY_TYPE,
                    entry_source=dataplex_v1.EntrySource(description=tbl.description[:250]),
                    aspects={
                        ASPECT_TYPE: dataplex_v1.Aspect(
                            aspect_type=ASPECT_TYPE,
                            data=schema_struct,
                        )
                    },
                )
                entry.name = f"{eg_name}/entries/{tbl.name}"
                # upsert entry
                try:
                    self.catalog.update_entry(entry=entry)
                except Exception:
                    self.catalog.create_entry(
                        parent=eg_name,
                        entry=entry,
                        entry_id=tbl.name,
                    )
            except Exception as e:
                self.logger.error(
                    "Error publishing table %s: %s", tbl.name, e, exc_info=True
                )




def crawl_with_dataplex(conf: ConfigTree) -> None:
    """
    Convenience: run the extractor → loader → publisher pipeline end‑to‑end.
    """
    extractor = DataplexMetadataExtractor()
    extractor.init(conf)
    from ryoma_ai.datasource.dataplex_loader import DataplexLoader    # defer import to break the cycle    
    loader = DataplexLoader()          # <-- concrete subclass
    loader.init(conf)                  # <-- initialise it once

    publisher = DataplexPublisher()
    publisher.init(conf)
    task = DefaultTask(extractor=extractor, loader=loader)
    
    job = DefaultJob(conf=conf, task=task, publisher=publisher)
    job.launch()
    # ensure the loader is closed (flush buffers, etc.)
    loader.close()
