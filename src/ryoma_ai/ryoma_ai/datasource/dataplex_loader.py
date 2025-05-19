# src/ryoma_ai/ryoma_ai/datasource/dataplex_loader.py

from databuilder.loader.base_loader import Loader
from pyhocon import ConfigTree
from ryoma_ai.datasource.dataplex import DataplexMetadataExtractor, DataplexPublisher

class DataplexLoader(Loader):
    """
    A concrete Loader that uses our DataplexPublisher to publish
    Dataplex-extracted metadata records back into our runtime store.
    """
    def init(self, conf: ConfigTree) -> None:
        # Extract the same config keys used by our extractor
        project_id  = conf.get_string(f"{DataplexMetadataExtractor.SCOPE}.project_id")
        credentials = conf.get_string(f"{DataplexMetadataExtractor.SCOPE}.credentials", None)

        # Instantiate the publisher
        self.publisher = DataplexPublisher(
            project_id=project_id,
            credentials=credentials,
        )
        # Prepare any publisher state (schema/table creation, etc.)
        self.publisher.prepare()

    def load(self, record) -> None:
        # `record` is one of our Pydantic models (Catalog, Schema, Table, Column)
        # Delegate publishing of each metadata object
        self.publisher.publish_record(record)

    def close(self) -> None:
        # Finalize the publisher (flush buffers, commit transactions, etc.)
        self.publisher.finish()
