# src/ryoma_ai/ryoma_ai/datasource/dataplex.py

import logging
from typing import Iterator, Any, Dict, List, Optional

from pyhocon import ConfigTree
from databuilder.extractor.base_extractor import Extractor
from databuilder.task.task import DefaultTask
from databuilder.job.job import DefaultJob
from databuilder.models.table_metadata import ColumnMetadata, TableMetadata
from databuilder.publisher.base_publisher import Publisher


from google.cloud import dataplex_v1, bigquery
from google.cloud.dataplex_v1.types import Entry
from google.api_core.exceptions import (
    BadRequest, Forbidden, MethodNotImplemented, NotFound, PermissionDenied,
)
from google.protobuf import struct_pb2

#–– magic identifiers for the “generic” table entry and aspect in Dataplex Catalog:
ENTRY_TYPE  = "projects/dataplex-types/locations/global/entryTypes/generic"
ASPECT_TYPE = "projects/dataplex-types/locations/global/aspectTypes/generic"

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)


class DataplexMetadataExtractor(Extractor):
    """Extract BigQuery metadata via Dataplex Universal Catalog.

    Enumerates BQ tables and views through the system-managed
    ``@bigquery`` EntryGroup that Dataplex auto-populates from every
    project's BigQuery datasets. Replaces the legacy Lake/Zone/Asset
    walk, which required manual ``DataplexServiceClient`` Asset
    registration that production deployments never set up — empirically
    verified by 30+ days of "Found 0 tables in Dataplex" logs.

    All entry filtering uses Google's documented contract fields
    (``entry_source.system``, ``entry_type`` suffix,
    ``fully_qualified_name`` prefix) and the BigQuery client's structured
    ``TableReference.from_string`` parser. No regex, no path slicing.

    Authoritative schema is fetched via ``bigquery.Client.get_table``
    (same call the legacy code used). Tables Dataplex knows about that
    the SA can't read (``Forbidden``/``NotFound``) are skipped with a
    debug-level log rather than crashing the iterator.
    """

    # System-contract suffixes for BQ Universal Catalog. The
    # ``projects/<google-internal>/...entryTypes/`` prefix varies (it's
    # Google's project-number for the type registry), but the trailing
    # name is the stable contract.
    _BQ_TABLE_TYPE_SUFFIX = "/bigquery-table"
    _BQ_VIEW_TYPE_SUFFIX = "/bigquery-view"
    _BQ_FQN_PREFIX = "bigquery:"
    # entry_source.system value Dataplex assigns to BQ-imported entries.
    _SYSTEM_BIGQUERY = "BIGQUERY"

    def init(self, conf: ConfigTree) -> None:
        self.project = conf.get_string("project_id")
        # pick up explicit credentials if provided, else fallback to ADC
        self.creds = conf.get("credentials", None)
        # Optional single-region scope. If absent, the iterator
        # auto-discovers every region with an ``@bigquery`` EntryGroup
        # via ``list_entry_groups(parent="projects/.../locations/-")``
        # (the ``-`` wildcard works on entry-group listing — it does NOT
        # work on ``list_entries`` parent, which is why we need the
        # explicit per-region loop).
        self.location: Optional[str] = conf.get("gcp_location", None) or None
        self.catalog = dataplex_v1.CatalogServiceClient(credentials=self.creds)
        self.bq = bigquery.Client(project=self.project, credentials=self.creds)
        self._iter = self._iterate_tables()

    def _iterate_tables(self) -> Iterator[TableMetadata]:
        for group_parent in self._discover_bigquery_entry_groups():
            for entry in self._list_entries(group_parent):
                tm = self._entry_to_table_metadata(entry)
                if tm is not None:
                    yield tm

    def _discover_bigquery_entry_groups(self) -> List[str]:
        """Return the resource names of every ``@bigquery`` EntryGroup
        for this project. Honors ``gcp_location`` if set; else
        auto-discovers via the cross-region wildcard."""
        if self.location:
            return [
                f"projects/{self.project}/locations/{self.location}/entryGroups/@bigquery"
            ]
        try:
            return [
                g.name
                for g in self.catalog.list_entry_groups(
                    request=dataplex_v1.ListEntryGroupsRequest(
                        parent=f"projects/{self.project}/locations/-",
                    )
                )
                if g.name.endswith("/entryGroups/@bigquery")
            ]
        except (PermissionDenied, NotFound) as exc:
            LOGGER.warning(
                "Dataplex EntryGroup discovery failed for project %s: %s",
                self.project, exc,
            )
            return []

    def _list_entries(self, group_parent: str) -> Iterator[Entry]:
        """List BQ entries in one ``@bigquery`` EntryGroup. Catalog-side
        benign failures are logged and skipped:

        * ``NotFound`` / ``PermissionDenied`` — some regions
          legitimately lack a ``@bigquery`` group when there's no BQ
          data there, or the caller's SA lacks ``catalogViewer`` on
          that region.
        * ``MethodNotImplemented`` — ``list_entries`` returns gRPC
          ``UNIMPLEMENTED`` (mapped from HTTP 404) when the region
          itself doesn't have Dataplex Catalog enabled. Empirically
          observed on the EU multi-region: the API endpoint exists but
          rejects the call with ``Received http2 header with status:
          404``. Treated as a benign empty region rather than an error.

        Other ``GoogleAPICallError`` subclasses (e.g. ``BadRequest``,
        ``InternalServerError``) propagate so genuine misconfiguration
        surfaces rather than silently zero-result.
        """
        try:
            yield from self.catalog.list_entries(
                request=dataplex_v1.ListEntriesRequest(parent=group_parent),
            )
        except (NotFound, PermissionDenied, MethodNotImplemented) as exc:
            LOGGER.warning(
                "Dataplex list_entries failed for %s: %s",
                group_parent, exc,
            )

    def _entry_to_table_metadata(
        self, entry: Entry,
    ) -> Optional[TableMetadata]:
        """Convert one Dataplex Catalog entry into a ``TableMetadata`` if
        (and only if) it represents a BigQuery table or view. Returns
        ``None`` for datasets, malformed FQNs, and tables the SA can't
        read."""
        if entry.entry_source.system != self._SYSTEM_BIGQUERY:
            return None
        is_view = entry.entry_type.endswith(self._BQ_VIEW_TYPE_SUFFIX)
        is_table = entry.entry_type.endswith(self._BQ_TABLE_TYPE_SUFFIX)
        if not (is_table or is_view):
            return None  # bigquery-dataset entry or unknown subtype — skip
        fqn = entry.fully_qualified_name
        if not fqn.startswith(self._BQ_FQN_PREFIX):
            return None
        try:
            # ``TableReference.from_string`` is the BigQuery client's own
            # grammar parser — accepts ``project.dataset.table`` and
            # raises ``ValueError`` on anything else (e.g. dataset-level
            # ``project.dataset`` form).
            ref = bigquery.TableReference.from_string(
                fqn[len(self._BQ_FQN_PREFIX):]
            )
        except ValueError:
            return None
        # Universal Catalog FQNs include literal backticks around BQ
        # identifiers with special characters (spaces, hyphens etc. — e.g.
        # ``bigquery:proj.dataset.`Table With Spaces```). The parser
        # preserves the backticks, but ``get_table`` URL-encodes them and
        # BQ rejects the request. Strip the SQL-quoting backticks so the
        # client gets the raw identifier.
        unquoted_project = ref.project.strip("`")
        unquoted_dataset = ref.dataset_id.strip("`")
        unquoted_table = ref.table_id.strip("`")
        if (unquoted_project, unquoted_dataset, unquoted_table) != (
            ref.project, ref.dataset_id, ref.table_id,
        ):
            ref = bigquery.TableReference(
                bigquery.DatasetReference(unquoted_project, unquoted_dataset),
                unquoted_table,
            )
        try:
            table_obj = self.bq.get_table(ref)
        except (NotFound, Forbidden, BadRequest) as exc:
            # NotFound: Dataplex sees a table that's since been dropped.
            # Forbidden: SA lacks read on this specific table.
            # BadRequest: identifier still rejected by BQ (e.g., contains
            #   chars that even backtick-stripping can't normalise — rare
            #   but skipping is safer than crashing the iterator).
            LOGGER.debug(
                "Skipping inaccessible BQ table %s: %s", ref, exc,
            )
            return None
        return TableMetadata(
            database=table_obj.dataset_id,
            cluster=entry.entry_source.location,
            schema=table_obj.dataset_id,
            name=f"{table_obj.project}.{table_obj.dataset_id}.{table_obj.table_id}",
            description=table_obj.description or "",
            columns=[
                ColumnMetadata(
                    name=f.name,
                    col_type=f.field_type,
                    description=f.description or "",
                    sort_order=i,
                )
                for i, f in enumerate(table_obj.schema)
            ],
            is_view=is_view,
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

    def publish(self, records: Iterator[TableMetadata]) -> None:
        """
        Public entry-point used by Databuilder Loader.
        Simply forwards to publish_impl() so the existing logic
        continues to work unchanged.
        """
        self.publish_impl(records)


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
