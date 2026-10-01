"""A deterministic, offline simulation of one disrupted trip.

No network, no API keys, no clock dependency. One seeded BOS -> AMS -> BCN trip
with a tight 40-minute connection, a prepaid non-refundable hotel, a non-refundable
timed activity, and one scripted event: BA 0431 delayed 95 minutes.

Times are derived, never typed twice. Each leg's start is a real local wall-clock
time in the right zone; its end is `start + flight_time`. That is what keeps the
arithmetic honest (see tests/test_world.py).

Timezone basis for October 2026: Boston is EDT (UTC-4; US DST ends Nov 1), and both
Amsterdam and Barcelona are CEST (UTC+2; EU DST ends Oct 25). So AMS and BCN share an
offset and sit exactly 6h ahead of BOS -- the AMS-BCN leg crosses no timezone at all.
"""
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from models import Constraint, Leg, Trip

UTC = timezone.utc
BOS, AMS, BCN = "BOS", "AMS", "BCN"
ZONES = {BOS: ZoneInfo("America/New_York"), AMS: ZoneInfo("Europe/Amsterdam"), BCN: ZoneInfo("Europe/Madrid")}

FLIGHT_TIME = {(BOS, AMS): timedelta(hours=7), (AMS, BCN): timedelta(hours=2, minutes=5)}
FLIGHT_NO = {"BOS-AMS-1": "BA 0431", "AMS-BCN-1": "IB 3110"}

TICK_MINUTES = 10          # simulated minutes per tick
DELAY_MINUTES = 95


def _local(day: int, hour: int, minute: int, zone: str) -> datetime:
    """A local wall-clock time on 2026-10-<day> in `zone`, as aware UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONES[zone]).astimezone(UTC)


def local(dt: datetime, zone: str) -> datetime:
    """Same instant, expressed in `zone`."""
    return dt.astimezone(ZONES[zone])


def fmt(dt: datetime, zone: str) -> str:
    """Human time in `zone`, e.g. 'Oct 01 21:05 EDT'."""
    return local(dt, zone).strftime("%b %d %H:%M %Z")


START_TIME = _local(1, 16, 45, BOS)   # Thu Oct 01 16:45 EDT = 20:45Z
EVENT_AT = _local(1, 17, 15, BOS)    # announced on tick 3, 30 simulated minutes later

DELAY_REASONS = ("departure gate change", "late inbound aircraft", "aircraft rotation",
                 "ATC flow restrictions", "weather at the departure gate")
GATES = ("C5", "E7", "B12", "A33", "D9")


def build_trip() -> Trip:
    """The seed itinerary. Arrivals are computed, so durations can't drift."""
    l1_dep = _local(1, 21, 5, BOS)                      # Oct 01 21:05 EDT = Oct 02 01:05Z
    l2_dep = _local(2, 10, 45, AMS)                     # Oct 02 10:45 CEST = 08:45Z
    hotel_in = _local(2, 15, 0, BCN)
    act = _local(2, 17, 0, BCN)
    legs = [
        Leg("BOS-AMS-1", "flight", l1_dep, l1_dep + FLIGHT_TIME[(BOS, AMS)], "scheduled",
            BOS, AMS, "BOS-AMS-BA0431", True, 612.0),
        Leg("AMS-BCN-1", "flight", l2_dep, l2_dep + FLIGHT_TIME[(AMS, BCN)], "scheduled",
            AMS, BCN, "AMS-BCN-IB3110", True, 214.0),
        Leg("BCN-HOTEL-1", "hotel", hotel_in, _local(3, 11, 0, BCN), "scheduled",
            None, BCN, "BCN-HTL-88421", False, 248.0),
        Leg("BCN-ACT-1", "activity", act, act + timedelta(hours=1, minutes=30), "scheduled",
            None, BCN, "BCN-SF-55210", False, 32.0),
    ]
    return Trip(
        "TRIP-8841", legs,
        [
            Constraint("hard", "connection at AMS: 40 min minimum", 100),
            Constraint("hard", "hotel check-in from 15:00 CEST"),
            Constraint("hard", "Sagrada Familia entry 17:00 CEST, non-refundable ticket"),
            Constraint("soft", "extra spend ceiling: $500", 90),
            Constraint("soft", "avoid overnight airport stays", 70),
            Constraint("soft", "keep the same day arrival in Barcelona", 85),
        ],
        {"seat": "aisle", "carriers": ["BA", "IB"], "cabin": "economy", "checked_bag": True},
    )


@dataclass
class Disruption:
    id: str
    at: datetime            # aware UTC: when the airline announces it
    kind: str               # delay | cancellation | gate_change
    leg_id: str
    minutes: int
    summary: str


@dataclass
class FlightOption:
    flight: str
    origin: str
    destination: str
    depart: datetime        # aware UTC
    arrive: datetime        # aware UTC
    cost_delta_usd: float
    note: str = ""


def _option(flight: str, o: str, d: str, day: int, hh: int, mm: int, zone: str, cost: float, note: str = "") -> FlightOption:
    dep = _local(day, hh, mm, zone)
    return FlightOption(flight, o, d, dep, dep + FLIGHT_TIME[(o, d)], cost, note)


# DL 0117 is a real option on the BOS-AMS route but it departed at 09:15 EDT, hours
# before the clock even starts. It is here so the "departed options are never
# returned" rule has something to actually filter.
ALTERNATIVES = {
    "BOS-AMS-1": [
        _option("AF 0089", BOS, AMS, 1, 18, 30, BOS, 320.0,
                "lands 3h15 before the AMS-BCN departure, so the rest of the trip is untouched"),
        _option("KL 0603", BOS, AMS, 1, 23, 55, BOS, 240.0,
                "lands after the AMS-BCN departure, so that leg still needs rebooking"),
        _option("BA 0432", BOS, AMS, 2, 9, 40, BOS, 410.0,
                "next morning; the whole trip slips a day and the hotel night is wasted"),
        _option("DL 0117", BOS, AMS, 1, 9, 15, BOS, 190.0, "morning departure, already gone"),
    ],
    "AMS-BCN-1": [
        _option("IB 3114", AMS, BCN, 2, 13, 20, AMS, 180.0,
                "lands 15:25 CEST, just after hotel check-in"),
        _option("VY 8901", AMS, BCN, 2, 17, 45, AMS, 95.0, "cheapest same-day option"),
        _option("IB 3108", AMS, BCN, 3, 6, 35, AMS, 60.0,
                "cheapest fare, but next day: hotel night is non-refundable and the Sagrada Familia slot is lost"),
    ],
}


class World:
    """The simulated world: a clock, an event queue, and the trip they mutate."""

    def __init__(self, seed: int = 7, start: datetime | None = None):
        self.rng = random.Random(seed)
        self.now = start or START_TIME
        self.trip = build_trip()
        self.tick_count = 0
        self._queued: list[Disruption] = []   # fired but not yet polled
        self._fired: set[str] = set()          # applied to the trip exactly once
        self._sent: set[str] = set()           # returned by poll exactly once
        # Built once from the seeded rng so the text is stable across ticks.
        self._event = Disruption(
            "EV-1", EVENT_AT, "delay", "BOS-AMS-1", DELAY_MINUTES,
            f"{FLIGHT_NO['BOS-AMS-1']} delayed {DELAY_MINUTES} min "
            f"({self.rng.choice(DELAY_REASONS)}), gate {self.rng.choice(GATES)}")

    # --- clock -----------------------------------------------------------------
    def tick(self, minutes: int = TICK_MINUTES) -> None:
        """Advance the simulated clock and fire the event once it is due."""
        self.now += timedelta(minutes=minutes)
        self.tick_count += 1
        if self._event.id not in self._fired and self._event.at <= self.now:
            self._apply(self._event)
            self._fired.add(self._event.id)
            self._queued.append(self._event)

    def _apply(self, event: Disruption) -> None:
        leg = self.leg(event.leg_id)
        if leg is None:
            return
        # Both endpoints move by the same amount, so the flight stays 7h long.
        leg.start += timedelta(minutes=event.minutes)
        leg.end += timedelta(minutes=event.minutes)
        leg.status = "delayed"

    # --- state -----------------------------------------------------------------
    def leg(self, leg_id: str) -> Leg | None:
        return next((l for l in self.trip.legs if l.id == leg_id), None)

    def next_leg(self, leg_id: str) -> Leg | None:
        """The first leg that starts after `leg_id` -- what a delay can cascade into."""
        leg = self.leg(leg_id)
        if leg is None:
            return None
        return next((l for l in self.trip.legs if l.start > leg.start), None)

    # --- the disruption feed ---------------------------------------------------
    def peek(self) -> list[Disruption]:
        """Fired but not yet polled. Does not consume -- safe to gate on."""
        return [e for e in self._queued if e.id not in self._sent]

    def poll(self) -> list[Disruption]:
        """Events fired since the last poll. Empty on a second call: no duplicates."""
        fresh = self.peek()
        self._sent.update(e.id for e in fresh)
        self._queued = [e for e in self._queued if e.id not in self._sent]
        return fresh

    # --- the bookable world ---------------------------------------------------
    def alternatives(self, leg_id: str) -> list[FlightOption]:
        """Options still in the future. Anything departing at or before `now` is gone.

        This filter is the reason no agent can ever be handed a flight that has
        already left. It lives in code because a prompt is not a guarantee.
        """
        return [o for o in ALTERNATIVES.get(leg_id, []) if o.depart > self.now]

    def excluded_count(self, leg_id: str) -> int:
        """How many options were dropped because they had already departed."""
        return len(ALTERNATIVES.get(leg_id, [])) - len(self.alternatives(leg_id))