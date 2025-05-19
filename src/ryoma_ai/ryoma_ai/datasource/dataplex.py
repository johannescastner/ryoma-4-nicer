import logging
from typing import Optional, List

from google.cloud import dataplex_v1
from google.oauth2.service_account import Credentials
from pydantic import BaseModel
from databuilder.extractor.base_extractor import Extractor
from databuilder.loader.base_loader import Loader

from ryoma_ai.datasource.metadata import Catalog, Schema, Table, Column


_LOG = logging.getLogger(__name__)


class DataplexMetadataExtractor(Extractor):
    """
    Extract metadata from Google Cloud Dataplex (universal Data Catalog replacement),
    producing Ryoma’s Pydantic models (Catalog, Schema, Table, Column) for downstream loading.

    Workflow:
      1. List Lakes in the given project/location.
      2. For each Lake, list Zones.
      3. For each Zone, list Assets of type TABLE or STREAM.
      4. For each Asset, fetch its schema fields and map to Column.

    Yields (in extract):
      Catalog, then Schema, then Table, then Column instances, in depth-first order.
    """

    def __init__(
        self,
        project_id: str,
        location: str,
        credentials: Credentials,
    ):
        self.project_id = project_id
        self.location = location
        self.credentials = credentials
        self.client = dataplex_v1.DataplexServiceClient(credentials=credentials)
        self._items: List[BaseModel] = []
        self._index = 0

    def init(self) -> None:
        parent = f"projects/{self.project_id}/locations/{self.location}"
        _LOG.info("Listing Dataplex lakes under %s", parent)
        for lake in self.client.list_lakes(request={"parent": parent}):
            # Treat each lake as a 'Catalog'
            catalog = Catalog(catalog_name=lake.name)
            self._items.append(catalog)

            zone_parent = lake.name
            _LOG.info("  Listing zones under lake %s", lake.name)
            for zone in self.client.list_zones(request={"parent": zone_parent}):
                schema = Schema(schema_name=zone.name)
                catalog.schemas = catalog.schemas or []
                catalog.schemas.append(schema)
                self._items.append(schema)

                asset_parent = zone.name
                filter_str = "asset_type=TABLE OR asset_type=STREAM"
                _LOG.info("    Listing assets in zone %s (filter=%s)", zone.name, filter_str)
                for asset in self.client.list_assets(request={"parent": asset_parent, "filter": filter_str}):
                    tbl = Table(table_name=asset.name.split('/')[-1], columns=[])
                    schema.tables = schema.tables or []
                    schema.tables.append(tbl)
                    self._items.append(tbl)

                    # Fetch schema fields (Dataplex resource_spec.schema.fields)
                    resource_schema = getattr(asset.resource_spec, "schema", None)
                    if resource_schema and hasattr(resource_schema, 'fields'):
                        for field in resource_schema.fields:
                            col = Column(
                                name=field.name,
                                type=field.type_,
                                nullable=(field.mode_ != dataplex_v1.Field.Mode.REQUIRED),
                                primary_key=False,
                            )
                            tbl.columns.append(col)
                            self._items.append(col)

    def extract(self) -> Optional[BaseModel]:
        if self._index < len(self._items):
            item = self._items[self._index]
            self._index += 1
            return item
        return None

    def get_scope(self) -> dict:
        """
        Return context for loader (unused here)."""
        return { }


# Example of wiring into a DefaultJob:
#
# from databuilder.task.task import DefaultTask
# from databuilder.job.job import DefaultJob
#
# loader = Loader()  # your existing loader
# job = DefaultJob(
#     conf=None,
#     task=DefaultTask(
#         extractor=DataplexMetadataExtractor(
#             project_id=PROJECT_ID,
#             location=LOCATION,
#             credentials=CREDENTIALS,
#         ),
#         loader=loader,
#     )
# )
# job.launch()
