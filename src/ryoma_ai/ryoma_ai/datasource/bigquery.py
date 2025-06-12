import logging
from typing import Optional, Type, Any
from functools import lru_cache
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
from ryoma_ai.datasource.metadata import Catalog, Schema, Table, Column  # ensure these are correct
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import reflection
from sqlalchemy.engine.reflection import Inspector
from google.cloud import bigquery
# Store the original inspect function
_original_inspect = sa_inspect

class BigQueryInspector(Inspector):
    """
    Enhanced Inspector for BigQuery that properly handles INFORMATION_SCHEMA queries
    by using the BigQuery client directly instead of SQLAlchemy's reflection.
    """
    
    def __init__(self, engine):
        super().__init__(engine)
        self._table_info_cache = {}
        self._dataset_info_cache = {}
        self._setup_bigquery_client()
        
    def _setup_bigquery_client(self):
        """Set up the BigQuery client from engine credentials if possible."""
        try:
            # Try to extract credentials from the engine
            if hasattr(self.engine, 'raw_connection'):
                conn = self.engine.raw_connection()
                if hasattr(conn, 'credentials') and hasattr(conn, 'project'):
                    self.bq_client = bigquery.Client(
                        credentials=conn.credentials,
                        project=conn.project
                    )
                    self.project_id = conn.project
                    return
            
            # If we can't get credentials from the engine, try to get them from the URL
            if hasattr(self.engine, 'url') and hasattr(self.engine.url, 'query'):
                from google.oauth2 import service_account
                import json
                
                # Try to get project_id from the URL
                if hasattr(self.engine.url, 'host'):
                    self.project_id = self.engine.url.host
                
                # Try to get credentials from the URL query parameters
                if 'credentials_info' in self.engine.url.query:
                    creds_info = json.loads(self.engine.url.query['credentials_info'])
                    credentials = service_account.Credentials.from_service_account_info(creds_info)
                    self.bq_client = bigquery.Client(
                        credentials=credentials,
                        project=self.project_id
                    )
                    return
            
            # If all else fails, try to use default credentials
            self.bq_client = bigquery.Client()
            self.project_id = self.bq_client.project
            
        except Exception as e:
            logging.warning(f"Failed to set up BigQuery client: {e}")
            self.bq_client = None
            self.project_id = None
    
    def get_schema_names(self):
        """Return all schema names (datasets in BigQuery)."""
        if not self.bq_client:
            return super().get_schema_names()
        
        try:
            datasets = list(self.bq_client.list_datasets())
            return [ds.dataset_id for ds in datasets]
        except Exception as e:
            logging.warning(f"Error getting schema names: {e}")
            return super().get_schema_names()
    
    def get_table_names(self, schema=None):
        """Return all table names for a given schema (dataset in BigQuery)."""
        if not self.bq_client or not schema:
            return super().get_table_names(schema)
        
        try:
            tables = list(self.bq_client.list_tables(f"{self.project_id}.{schema}"))
            # Filter out views
            return [table.table_id for table in tables if table.table_type == 'TABLE']
        except Exception as e:
            logging.warning(f"Error getting table names for schema {schema}: {e}")
            return super().get_table_names(schema)
    
    def get_view_names(self, schema=None):
        """Return all view names for a given schema."""
        if not self.bq_client or not schema:
            return super().get_view_names(schema)
        
        try:
            tables = list(self.bq_client.list_tables(f"{self.project_id}.{schema}"))
            # Filter to only include views
            return [table.table_id for table in tables if table.table_type == 'VIEW']
        except Exception as e:
            logging.warning(f"Error getting view names for schema {schema}: {e}")
            return super().get_view_names(schema)
    
    def get_columns(self, table_name, schema=None, **kw):
        """Get all columns for a table."""
        if not self.bq_client or not schema:
            return super().get_columns(table_name, schema, **kw)
        
        # Check cache first
        cache_key = f"{self.project_id}.{schema}.{table_name}"
        if cache_key in self._table_info_cache and 'columns' in self._table_info_cache[cache_key]:
            return self._table_info_cache[cache_key]['columns']
        
        try:
            table_ref = self.bq_client.get_table(f"{self.project_id}.{schema}.{table_name}")
            columns = []
            
            for field in table_ref.schema:
                # Convert BigQuery types to SQLAlchemy types
                col_type = self._convert_bq_type_to_sqla(field.field_type)
                
                column = {
                    'name': field.name,
                    'type': col_type,
                    'nullable': field.mode == 'NULLABLE',
                    'default': None,
                    'autoincrement': False,
                }
                columns.append(column)
            
            # Cache the result
            if cache_key not in self._table_info_cache:
                self._table_info_cache[cache_key] = {}
            self._table_info_cache[cache_key]['columns'] = columns
            
            return columns
        except Exception as e:
            logging.warning(f"Error getting columns for {schema}.{table_name}: {e}")
            return super().get_columns(table_name, schema, **kw)
    
    def _convert_bq_type_to_sqla(self, bq_type):
        """Convert BigQuery type to SQLAlchemy type string."""
        # This is a simplified mapping - you might need to expand this
        from sqlalchemy import types
        
        type_map = {
            'STRING': types.String,
            'INTEGER': types.BigInteger,
            'INT64': types.BigInteger,
            'FLOAT': types.Float,
            'FLOAT64': types.Float,
            'BOOLEAN': types.Boolean,
            'BOOL': types.Boolean,
            'TIMESTAMP': types.TIMESTAMP,
            'DATE': types.Date,
            'TIME': types.Time,
            'DATETIME': types.DateTime,
            'NUMERIC': types.Numeric,
            'BIGNUMERIC': types.Numeric,
            'BYTES': types.LargeBinary,
            'RECORD': types.JSON,  # Nested records as JSON
        }
        
        if bq_type.upper() in type_map:
            return type_map[bq_type.upper()]
        else:
            return types.String  # Default to String for unknown types
    
    def get_pk_constraint(self, table_name, schema=None, **kw):
        """Get primary key constraint for a table."""
        # BigQuery doesn't have traditional primary keys
        return {'constrained_columns': [], 'name': None}
    
    def get_foreign_keys(self, table_name, schema=None, **kw):
        """Get foreign keys for a table."""
        # BigQuery doesn't have traditional foreign keys
        return []
    
    def get_unique_constraints(self, table_name, schema=None, **kw):
        """Get unique constraints for a table."""
        # BigQuery doesn't have traditional unique constraints
        return []
    
    def has_table(self, table_name, schema=None, **kw):
        """Check if a table exists."""
        if not self.bq_client or not schema:
            return super().has_table(table_name, schema, **kw)
        
        try:
            self.bq_client.get_table(f"{self.project_id}.{schema}.{table_name}")
            return True
        except Exception:
            return False


def patched_inspect(engine):
    """
    Return a BigQueryInspector for BigQuery engines; otherwise, call original inspect().
    """
    try:
        dialect_name = engine.dialect.name
    except Exception:
        return _original_inspect(engine)

    if dialect_name == "bigquery":
        return BigQueryInspector(engine)
    return _original_inspect(engine)

# Monkey-patch SQLAlchemy's inspect function
import sqlalchemy
sqlalchemy.inspect = patched_inspect

class BigQueryDataSource(SqlDataSource):
    def __init__(
        self,
        project_id: str,
        dataset_id: Optional[str] = None,
        credentials: Optional[Any] = None,
        metadata: Optional[Any] = None,
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
        self.metadata = metadata
        # Pluggable extractor & publisher
        self._extractor_cls = metadata_extractor_cls
        self._publisher_cls = metadata_publisher_cls
        self.dataplex_metadata_lookup = self._build_metadata_lookup() if metadata else {}
        
    # ------------------------------------------------------------------
    # PRIVATE – single, cached BigQuery connection
    # ------------------------------------------------------------------
    def _connect(self, **kwargs) -> BaseBackend:
        """
        Build (or reuse) the Ibis BigQuery backend.

        *   Ibis ≥5.0 expects the service-account object under the keyword
            **auth_credentials** – passing “credentials” is silently ignored and
            you end up with a None backend.
        *   We cache the backend so repeated queries don’t re-authenticate.
        """
        if hasattr(self, "_backend") and self._backend is not None:
            return self._backend                                   # reuse handle

        connect_args: dict[str, Any] = {"project_id": self.project_id}
        if self.dataset_id:
            connect_args["dataset_id"] = self.dataset_id
        if self.credentials:                                        # <- renamed
            connect_args["credentials"] = self.credentials

        logging.info("Connecting to BigQuery with %r", connect_args)
        self._backend = ibis.bigquery.connect(**connect_args)
        return self._backend
        
        # ------------------------------------------------------------
    # OPTIONAL – allow callers to provide a ready-made backend
    # ------------------------------------------------------------
    def set_backend(self, backend: BaseBackend) -> "BigQueryDataSource":
        """
        Inject an already-authenticated Ibis backend (so we don’t reconnect
        over and over).  Returns *self* so you can chain calls.
        """
        self._backend = backend
        return self

    def get_catalog(self, catalog: Optional[str] = None) -> Catalog:
        return Catalog(
            catalog_name=self.dataset_id or self.project_id or "default_catalog",
            schemas=[
                Schema(
                    schema_name=table.schema if hasattr(table, "schema") else "default_schema",
                    tables=[
                        Table(
                            table_name=table.name,
                            columns=[
                                Column(
                                    name=c.name,
                                    type=getattr(c, "col_type", getattr(c, "type", "")),  # <- key line
                                    description=getattr(c, "description", ""),
                                )
                                for c in table.columns
                            ],
                        )
                    ],
                )
                for table in (self.metadata or [])
            ],
        )

    def _build_metadata_lookup(self):
        lookup = {}
        for table in self.metadata:
            fq_name = f"{self.project_id}.{table.schema}.{table.name}"
            lookup[table.name] = fq_name
        return lookup
    # ------------------------------------------------------------------
    # PUBLIC – thin helper the SqlAgent uses to run SQL
    # ------------------------------------------------------------------
    def query(self, sql: str):  # -> pandas.DataFrame
        """
        Execute *sql* immediately and return the result as a DataFrame.
        
        For INFORMATION_SCHEMA queries, uses the BigQuery client directly
        to avoid SQLAlchemy issues.
        """
        # Check if this is an INFORMATION_SCHEMA query
        if ("INFORMATION_SCHEMA" in sql.upper() or 
            "LIST DATASETS" in sql.upper() or 
            "SHOW DATASETS" in sql.upper() or 
            "SHOW TABLES" in sql.upper()):
            try:
                # Create a BigQuery client if we don't have one
                if not hasattr(self, '_bq_client'):
                    self._bq_client = bigquery.Client(
                        project=self.project_id,
                        credentials=self.credentials
                    )
                
                # Execute the query directly with the BigQuery client
                return self._bq_client.query(sql).to_dataframe()
            except Exception as e:
                logging.warning(f"Error executing INFORMATION_SCHEMA query with BigQuery client: {e}")
                # Fall back to the standard method
        
        # Use the standard method for non-INFORMATION_SCHEMA queries
        return self._connect().raw_sql(sql).fetch()

# ------------------------------------------------------------
    # PRIVATE helper – make every table fully-qualified
    # ------------------------------------------------------------
    def _qualify(self, table_name):
        if table_name in self.metadata_lookup:
            return f"`{self.metadata_lookup[table_name]}`"
        elif self.dataset_id:
            return f"`{self.project_id}.{self.dataset_id}.{table_name}`"
        else:
            raise ValueError(f"Cannot qualify table '{table_name}' without dataset_id or metadata.")
    # ------------------------------------------------------------
    # PUBLIC – used by SqlQueryTool() under the hood
    # ------------------------------------------------------------
    @lru_cache(maxsize=1024)       # ask BigQuery only once per table
    def get_table_schema(self, table_name: str) -> str:
        fq_name = self._qualify(table_name)
        return (
            self._connect()
                .raw_sql(f"SELECT * FROM {fq_name} LIMIT 0")
                .schema()
                .to_string()
        )


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
