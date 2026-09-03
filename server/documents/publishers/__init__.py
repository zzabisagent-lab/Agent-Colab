"""Document publishers (development plan §10.3). Built-in kinds register on import."""

from server.documents.publishers.base import (
    Publisher,
    PublishError,
    PublishRecord,
    PublishTarget,
    publisher_for,
    publisher_kinds,
    register_publisher,
)

__all__ = [
    "PublishError",
    "PublishRecord",
    "PublishTarget",
    "Publisher",
    "publisher_for",
    "publisher_kinds",
    "register_publisher",
]
