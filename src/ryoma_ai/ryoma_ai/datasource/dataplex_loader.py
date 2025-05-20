# src/ryoma_ai/ryoma_ai/datasource/dataplex_loader.py
import logging
from typing import Iterator, Union

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

        # Initialize the publisher (expects project_id + credentials in conf)
        self.publisher = DataplexPublisher()
        self.publisher.init(conf)
        # If your publisher has a prepare or setup step:
        if hasattr(self.publisher, "prepare"):
            self.publisher.prepare()
            
    def get_scope(self) -> str:
        return "publisher.dataplex_metadata"

    def load(self, record: Union[Iterator, object]) -> None:
        # `record` may be a single TableMetadata or an iterator of them
        if not hasattr(record, '__iter__') or isinstance(record, (str, bytes)):
            records = iter([record])
        else:
            records = record  # already iterable
        # Delegate publishing of metadata objects
        self.publisher.publish(records)

    def close(self) -> None:
        # Finalize the publisher (flush buffers, commit transactions, etc.)
        if hasattr(self.publisher, "finish"):
            self.publisher.finish()

