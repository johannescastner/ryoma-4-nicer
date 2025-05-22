# src/ryoma_ai/ryoma_ai/datasource/dataplex_loader.py
import logging
from typing import Iterator, Union, List
from databuilder.models.table_metadata import TableMetadata
from databuilder.loader.base_loader import Loader
from pyhocon import ConfigTree

from ryoma_ai.datasource.dataplex import DataplexPublisher

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

class DataplexLoader(Loader):
    """
    A concrete Loader that uses our DataplexPublisher to publish
    Dataplex-extracted metadata records back into our runtime store.
    """
    def init(self, conf: ConfigTree) -> None:
        self.conf = conf
        self.metadata = []
        self.write = (
            conf.get_bool("write_metadata", False) or
            conf.get_bool("publisher.dataplex_metadata.write_metadata", False)
        )
        # Initialize the publisher (expects project_id + credentials in conf)
        if self.write:
            self.publisher = DataplexPublisher()
            self.publisher.init(conf)
        else:
            self.publisher = None

    def get_scope(self) -> str:
        return "publisher.dataplex_metadata"

    def load(self, record: Union[Iterator, object]) -> None:
        # normalise to iterable
        records = record if hasattr(record, "__iter__") and not isinstance(record, (str, bytes)) else [record]
        self.metadata.extend(records)
        if self.write and self.publisher:
            self.publisher.publish(records)

    def close(self) -> None:
        # Finalize the publisher (flush buffers, commit transactions, etc.)
        if hasattr(self.publisher, "finish"):
            self.publisher.finish()

    def get_metadata(self) -> List[TableMetadata]:
        return self.metadata
