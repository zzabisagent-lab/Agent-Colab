"""Setup apply-order enforcer (development plan §8.1) — P0-09.

``DB/migration → master key/Secret provider → Owner account/TOTP/recovery code → integration
settings → atomic CONFIGURED/LOCKED commit``. A later step cannot begin before every earlier
step is complete, and Owner/TOTP records are never reported as created before the DB and the key
provider are ready (V-P4-28).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from server.setup.errors import SetupError


class ApplyStep(IntEnum):
    DB_MIGRATION = 1
    KEY_PROVIDER = 2
    OWNER_TOTP = 3
    INTEGRATIONS = 4
    COMMIT = 5


@dataclass
class ApplyOrder:
    completed: set[ApplyStep] = field(default_factory=set)
    in_progress: ApplyStep | None = None
    log: list[str] = field(default_factory=list)

    def begin(self, step: ApplyStep) -> None:
        missing = [s for s in ApplyStep if s < step and s not in self.completed]
        if missing:
            raise SetupError(
                "SETUP_ORDER_VIOLATION",
                f"{step.name} requires {', '.join(m.name for m in missing)} first",
            )
        if step in self.completed:
            raise SetupError("SETUP_ORDER_VIOLATION", f"{step.name} already completed")
        self.in_progress = step
        self.log.append(f"begin {step.name}")

    def complete(self, step: ApplyStep) -> None:
        if self.in_progress is not step:
            raise SetupError("SETUP_ORDER_VIOLATION", f"{step.name} was not begun")
        self.completed.add(step)
        self.in_progress = None
        self.log.append(f"complete {step.name}")

    def fail(self, step: ApplyStep) -> None:
        """A failed step resets itself and every later step; earlier steps stay complete."""
        if self.in_progress is not step:
            raise SetupError("SETUP_ORDER_VIOLATION", f"{step.name} was not begun")
        self.in_progress = None
        self.completed = {s for s in self.completed if s < step}
        self.log.append(f"fail {step.name}")

    @property
    def owner_created_visible(self) -> bool:
        """UI may show "owner created" only when DB, key provider, and Owner/TOTP are complete."""
        return {
            ApplyStep.DB_MIGRATION,
            ApplyStep.KEY_PROVIDER,
            ApplyStep.OWNER_TOTP,
        } <= self.completed

    @property
    def committed(self) -> bool:
        return ApplyStep.COMMIT in self.completed
