"""Regression tests for the rewritten ``DataplexMetadataExtractor``.

The new extractor reads BigQuery metadata via Dataplex Universal
Catalog's ``@bigquery`` system EntryGroup (auto-populated by Dataplex
from every project's BQ datasets) instead of the legacy Lake/Zone/Asset
walk that required manual asset registration.

What these tests pin (one per concern):

  * ``test_iterates_tables_across_auto_discovered_regions`` —
    auto-discovers every ``@bigquery`` EntryGroup via the cross-region
    wildcard and yields one ``TableMetadata`` per table entry.
  * ``test_emits_views_with_is_view_true`` — distinguishes tables from
    views via the stable ``entry_type`` suffix contract.
  * ``test_skips_bigquery_dataset_entries`` — dataset-level entries
    (which don't have a schema) are filtered out.
  * ``test_skips_non_bigquery_system_entries`` — defense in depth: if
    ``@bigquery`` ever leaked a non-BQ entry, we wouldn't crash trying
    to fetch it as a BQ table.
  * ``test_skips_unreachable_tables`` — Dataplex sees tables the SA
    can't read; we log and continue rather than crash the iterator.
  * ``test_uses_table_reference_from_string_parser`` — the FQN parse
    flows through ``bigquery.TableReference.from_string`` (no regex,
    no manual ``.split('.')``); proven by parsing a project name with
    hyphens (which BQ supports but a naive split would mishandle).
  * ``test_honors_gcp_location_when_supplied`` — when ``gcp_location``
    is in the conf, only that region is queried (no wildcard
    discovery), preserving the legacy single-region call shape used by
    callers like ``crawl_dataplex_for_zone``.
  * ``test_handles_permission_denied_on_discovery_gracefully`` —
    project-level catalog read denial yields zero tables, never raises.

Run from the ryoma fork root::

    PYTHONPATH=src/ryoma_ai pytest tests/unit_tests/test_dataplex_extractor.py -v
"""
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import Forbidden, NotFound, PermissionDenied
from pyhocon import ConfigFactory

from ryoma_ai.datasource.dataplex import DataplexMetadataExtractor


# ============================================================================
# Helpers — build mock objects shaped like real Dataplex / BQ responses.
# ============================================================================


def _make_entry(
    *,
    fqn: str,
    entry_type_suffix: str,
    system: str = "BIGQUERY",
    location: str = "us-central1",
):
    """Build a MagicMock that quacks like a ``dataplex_v1.types.Entry``.
    The fields accessed in the production code are:
    ``entry_source.system``, ``entry_source.location``, ``entry_type``,
    ``fully_qualified_name``."""
    entry = MagicMock()
    entry.entry_source.system = system
    entry.entry_source.location = location
    entry.entry_type = (
        f"projects/655216118709/locations/global/entryTypes/{entry_type_suffix}"
    )
    entry.fully_qualified_name = fqn
    return entry


def _make_bq_table(
    *,
    project: str,
    dataset_id: str,
    table_id: str,
    description: str = "",
    table_type: str = "TABLE",
    columns=None,
):
    """Build a MagicMock that quacks like a ``bigquery.Table``."""
    if columns is None:
        columns = [
            MagicMock(name="row_id", field_type="STRING", description="row pk")
        ]
        columns[0].name = "row_id"  # MagicMock(name=...) sets internal name
    tbl = MagicMock()
    tbl.project = project
    tbl.dataset_id = dataset_id
    tbl.table_id = table_id
    tbl.description = description
    tbl.table_type = table_type
    tbl.schema = columns
    return tbl


def _column(name: str, field_type: str = "STRING", description: str = ""):
    c = MagicMock()
    c.name = name
    c.field_type = field_type
    c.description = description
    return c


def _make_extractor(*, gcp_location=None) -> DataplexMetadataExtractor:
    """Build a fresh extractor with mocked clients. The real
    ``init`` runs and constructs the iterator; tests then drive
    ``catalog.list_entry_groups`` / ``list_entries`` and ``bq.get_table``
    via ``return_value`` / ``side_effect`` on the mocked clients."""
    conf_dict = {"project_id": "test-proj"}
    if gcp_location is not None:
        conf_dict["gcp_location"] = gcp_location
    conf = ConfigFactory.from_dict(conf_dict)

    with patch(
        "ryoma_ai.datasource.dataplex.dataplex_v1.CatalogServiceClient"
    ) as catalog_cls, patch(
        "ryoma_ai.datasource.dataplex.bigquery.Client"
    ) as bq_cls:
        ext = DataplexMetadataExtractor()
        ext.init(conf)
        # Expose the mock instances that init() created so tests can
        # drive them.
        ext._mock_catalog = catalog_cls.return_value
        ext._mock_bq = bq_cls.return_value
        # init() already eagerly built the generator with the un-driven
        # mocks. Re-initialise the iterator AFTER the mocks are wired so
        # the test's return_values flow through.
        ext._iter = ext._iterate_tables()
    return ext


def _drain(extractor) -> list:
    """Pull every TableMetadata via the public ``extract()`` interface
    until None — what databuilder's ``DefaultJob`` does."""
    out = []
    while True:
        item = extractor.extract()
        if item is None:
            return out
        out.append(item)


# ============================================================================
# Tests
# ============================================================================


class TestDiscoveryAndIteration:

    def test_iterates_tables_across_auto_discovered_regions(self):
        """Two ``@bigquery`` groups, one bigquery-table entry each ->
        two ``TableMetadata`` yielded with ``cluster`` set from
        ``entry_source.location``."""
        ext = _make_extractor()  # no gcp_location -> auto-discover

        ext._mock_catalog.list_entry_groups.return_value = [
            MagicMock(name="g1"),  # mock_name attr used internally; we set .name below
            MagicMock(name="g2"),
            MagicMock(name="non-bq"),
        ]
        # MagicMock(name=) sets *internal* name; we need .name explicitly:
        ext._mock_catalog.list_entry_groups.return_value[0].name = (
            "projects/test-proj/locations/us-central1/entryGroups/@bigquery"
        )
        ext._mock_catalog.list_entry_groups.return_value[1].name = (
            "projects/test-proj/locations/eu/entryGroups/@bigquery"
        )
        ext._mock_catalog.list_entry_groups.return_value[2].name = (
            "projects/test-proj/locations/europe-west1/entryGroups/@cloudsql"
        )

        # list_entries returns one BQ table per group.
        def _list_entries_side_effect(request):
            if request.parent.endswith("/us-central1/entryGroups/@bigquery"):
                return iter([
                    _make_entry(
                        fqn="bigquery:test-proj.ds_a.tbl_a",
                        entry_type_suffix="bigquery-table",
                        location="us-central1",
                    )
                ])
            if request.parent.endswith("/eu/entryGroups/@bigquery"):
                return iter([
                    _make_entry(
                        fqn="bigquery:test-proj.ds_b.tbl_b",
                        entry_type_suffix="bigquery-table",
                        location="eu",
                    )
                ])
            return iter([])

        ext._mock_catalog.list_entries.side_effect = _list_entries_side_effect

        ext._mock_bq.get_table.side_effect = lambda ref: _make_bq_table(
            project=ref.project,
            dataset_id=ref.dataset_id,
            table_id=ref.table_id,
            columns=[_column("c1")],
        )

        results = _drain(ext)

        assert len(results) == 2
        clusters = sorted(r.cluster for r in results)
        names = sorted(r.name for r in results)
        assert clusters == ["eu", "us-central1"]
        assert names == [
            "test-proj.ds_a.tbl_a",
            "test-proj.ds_b.tbl_b",
        ]
        # @cloudsql group never queried (filtered by suffix match).
        called_parents = [
            c.kwargs["request"].parent
            if "request" in c.kwargs
            else c.args[0].parent
            for c in ext._mock_catalog.list_entries.call_args_list
        ]
        assert all("@bigquery" in p for p in called_parents)

    def test_emits_views_with_is_view_true(self):
        ext = _make_extractor(gcp_location="us")
        ext._mock_catalog.list_entries.return_value = iter([
            _make_entry(
                fqn="bigquery:p.d.v_my_view",
                entry_type_suffix="bigquery-view",
                location="us",
            )
        ])
        ext._mock_bq.get_table.return_value = _make_bq_table(
            project="p", dataset_id="d", table_id="v_my_view",
            table_type="VIEW", columns=[_column("x")],
        )

        results = _drain(ext)

        assert len(results) == 1
        assert results[0].is_view is True
        assert results[0].name == "p.d.v_my_view"


class TestFiltering:

    def test_skips_bigquery_dataset_entries(self):
        """Dataset-level entries (entry_type=...bigquery-dataset) have no
        schema and must not be yielded as TableMetadata."""
        ext = _make_extractor(gcp_location="us")
        ext._mock_catalog.list_entries.return_value = iter([
            _make_entry(
                fqn="bigquery:p.dataset_only",
                entry_type_suffix="bigquery-dataset",
                location="us",
            )
        ])
        # If get_table is called we'd know we leaked a dataset through.
        ext._mock_bq.get_table.side_effect = AssertionError(
            "get_table must not be called for dataset entries"
        )

        results = _drain(ext)

        assert results == []

    def test_skips_non_bigquery_system_entries(self):
        """Defense in depth: an entry with ``entry_source.system != BIGQUERY``
        is skipped without crashing — even if the EntryGroup is
        @bigquery (in case Google ever leaks cross-system entries)."""
        ext = _make_extractor(gcp_location="us")
        ext._mock_catalog.list_entries.return_value = iter([
            _make_entry(
                fqn="bigquery:p.d.t",
                entry_type_suffix="bigquery-table",
                system="CLOUDSQL",  # not BIGQUERY
                location="us",
            )
        ])
        ext._mock_bq.get_table.side_effect = AssertionError(
            "get_table must not be called for non-BIGQUERY entries"
        )

        results = _drain(ext)

        assert results == []

    def test_skips_unreachable_tables(self):
        """Dataplex may surface tables the SA can't read (Forbidden) or
        tables that have since been dropped (NotFound). Iterator must
        continue, not raise."""
        ext = _make_extractor(gcp_location="us")
        ext._mock_catalog.list_entries.return_value = iter([
            _make_entry(
                fqn="bigquery:p.d.forbidden",
                entry_type_suffix="bigquery-table",
                location="us",
            ),
            _make_entry(
                fqn="bigquery:p.d.gone",
                entry_type_suffix="bigquery-table",
                location="us",
            ),
            _make_entry(
                fqn="bigquery:p.d.ok",
                entry_type_suffix="bigquery-table",
                location="us",
            ),
        ])

        def _get_table_side_effect(ref):
            if ref.table_id == "forbidden":
                raise Forbidden("403")
            if ref.table_id == "gone":
                raise NotFound("404")
            return _make_bq_table(
                project=ref.project,
                dataset_id=ref.dataset_id,
                table_id=ref.table_id,
                columns=[_column("c")],
            )

        ext._mock_bq.get_table.side_effect = _get_table_side_effect

        results = _drain(ext)

        assert len(results) == 1
        assert results[0].name == "p.d.ok"


class TestStructuredParse:

    def test_uses_table_reference_from_string_parser(self):
        """A naive ``str.split('.')`` would mishandle BQ projects with
        hyphens or treat the FQN as ambiguous; ``TableReference.from_string``
        knows the BQ grammar. Verifying with a hyphenated project name."""
        ext = _make_extractor(gcp_location="us")
        # Real-world tenant project: viebeg-data-vault (two hyphens).
        ext._mock_catalog.list_entries.return_value = iter([
            _make_entry(
                fqn="bigquery:viebeg-data-vault.VIEBEG_Sales_analysis_dataset.amount",
                entry_type_suffix="bigquery-table",
                location="us-central1",
            )
        ])
        ext._mock_bq.get_table.side_effect = lambda ref: _make_bq_table(
            project=ref.project,
            dataset_id=ref.dataset_id,
            table_id=ref.table_id,
            columns=[_column("c")],
        )

        results = _drain(ext)

        assert len(results) == 1
        # Confirm the structured parse identified the project correctly:
        ref_arg = ext._mock_bq.get_table.call_args.args[0]
        assert ref_arg.project == "viebeg-data-vault"
        assert ref_arg.dataset_id == "VIEBEG_Sales_analysis_dataset"
        assert ref_arg.table_id == "amount"
        assert results[0].name == (
            "viebeg-data-vault.VIEBEG_Sales_analysis_dataset.amount"
        )


class TestConfiguration:

    def test_honors_gcp_location_when_supplied(self):
        """When ``gcp_location`` is in conf, only that region is hit —
        no cross-region wildcard discovery."""
        ext = _make_extractor(gcp_location="europe-west1")
        ext._mock_catalog.list_entries.return_value = iter([])

        _drain(ext)

        # Cross-region discovery must NOT have been invoked.
        ext._mock_catalog.list_entry_groups.assert_not_called()
        # The single explicit region was queried.
        ext._mock_catalog.list_entries.assert_called_once()
        called_parent = (
            ext._mock_catalog.list_entries.call_args.kwargs.get("request")
            or ext._mock_catalog.list_entries.call_args.args[0]
        ).parent
        assert called_parent == (
            "projects/test-proj/locations/europe-west1/entryGroups/@bigquery"
        )


class TestPermissionDeniedGraceful:

    def test_handles_permission_denied_on_discovery_gracefully(self):
        """SA without ``dataplex.entryGroups.list`` (= no
        ``catalogViewer``) should yield zero tables, not raise.
        Production observability tells us this is the silent failure
        mode that broke the read path for over a year."""
        ext = _make_extractor()  # auto-discover path
        ext._mock_catalog.list_entry_groups.side_effect = PermissionDenied(
            "no catalogViewer"
        )

        results = _drain(ext)

        assert results == []
        # We did NOT proceed to list_entries / get_table after the denial.
        ext._mock_catalog.list_entries.assert_not_called()
        ext._mock_bq.get_table.assert_not_called()
