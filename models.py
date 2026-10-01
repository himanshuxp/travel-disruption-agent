"""Trip data model. Exactly PLAN.md §6.

Every datetime is timezone-aware UTC. Travel bugs are timezone bugs, so the model
stores UTC and converts to a local zone only when printing.
"""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Leg:
    id: str                  # "AMS-BCN-1"
    kind: str                # flight | hotel | train | activity
    start: datetime          # aware, UTC
    end: datetime            # aware, UTC
    status: str              # scheduled | delayed | cancelled | rebooked | done
    origin: str | None       # IATA
    destination: str | None
    booking_ref: str | None
    refundable: bool
    cost_usd: float
    flight: str | None = None   # "AF 0089", set on rebooking; None for hotel/activity


@dataclass
class Constraint:
    kind: str                # hard | soft
    name: str                # "connection AMS 40min", "no overnight airport"
    weight: int = 100        # soft only: higher = more important


@dataclass
class Trip:
    id: str
    legs: list[Leg]
    constraints: list[Constraint]
    prefs: dict              # seat, aisle, no red-eyes, loyalty carriers...