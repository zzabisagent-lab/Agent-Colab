"""Projection framework (development plan §3.1 Projection, §6.1).

A ``Projector`` folds Events into a read model. Projections are never the command authority:
handlers read state from Event streams and update projections synchronously (read-after-write);
``runner.rebuild`` deletes a projection and replays every Event in ``recorded_seq`` order, which
must reproduce the identical canonical snapshot (V-P1-10, V-P7-08).
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session


class Projector(Protocol):
    name: str
    table: str
    primary_key: str

    def apply(self, session: Session, event: dict[str, Any]) -> None:
        """Fold one Event (envelope dict as returned by the Event store) into the read model."""
        ...


_PROJECTORS: dict[str, Projector] = {}


def register_projector(projector: Projector) -> Projector:
    _PROJECTORS[projector.name] = projector
    return projector


def get_projector(name: str) -> Projector:
    try:
        return _PROJECTORS[name]
    except KeyError as exc:
        raise KeyError(f"unknown projection {name!r}; known: {sorted(_PROJECTORS)}") from exc


def projector_names() -> list[str]:
    return sorted(_PROJECTORS)
