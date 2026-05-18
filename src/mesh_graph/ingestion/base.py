from abc import ABC, abstractmethod


class DataSource(ABC):
    @abstractmethod
    def start(self, db_path: str) -> None:
        """Begin ingesting data, writing to the DB at db_path."""

    @abstractmethod
    def stop(self) -> None:
        """Stop ingestion cleanly."""
