"""The §21.1 load profiles as data (P7-04).

`normal` is the development plan's normal functional load; `peak` is 3x that, which V-P7-03
sustains for 30 minutes. Every field is a rate or a population size, so a run is fully described
by the profile plus its duration.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Profile:
    name: str
    humans: int
    agents: int
    channels: int
    bridges: int
    api_writes_per_s: float
    messages_per_s: float
    schedules: int
    due_per_minute: int

    def scaled(self, factor: float, name: str) -> Profile:
        """Population stays as seeded; the *rates* and due volume scale (development plan §21.1)."""
        return replace(
            self,
            name=name,
            api_writes_per_s=self.api_writes_per_s * factor,
            messages_per_s=self.messages_per_s * factor,
            due_per_minute=int(self.due_per_minute * factor),
        )


NORMAL = Profile(
    name="normal",
    humans=50,
    agents=20,
    channels=100,
    bridges=20,
    api_writes_per_s=20.0,
    messages_per_s=10.0,
    schedules=100,
    due_per_minute=20,
)
PEAK = NORMAL.scaled(3.0, "peak")
SMOKE = NORMAL.scaled(0.1, "smoke")

PROFILES = {p.name: p for p in (NORMAL, PEAK, SMOKE)}

# V-P7-03 / §21.1 pass criteria
WRITE_P95_MS = 500.0
READ_P95_MS = 300.0
MAX_5XX_RATE = 0.01
