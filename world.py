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

# The two hard rules that are not derivable from a leg's own times. They live here, not
# inside the constraint strings, because `analyse_impact` and `commit_replan` have to
# evaluate them -- and a string is not something you can compare a datetime against.
# `build_trip` interpolates these into the human-readable constraint names, so the
# numbers the model reads and the numbers the code checks cannot drift apart.
MIN_CONNECTION_MINUTES = {BOS: 40, AMS: 40, BCN: 40}   # per hub, by IATA
CHECK_IN = (2, 15, 0)                                   # day, hour, minute, local BCN


def min_connection(hub: str) -> int:
    return MIN_CONNECTION_MINUTES.get(hub, 40)

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


def hhmm(dt: datetime, zone: str) -> str:
    """Clock time only, in `zone`, e.g. '04:40 CEST'."""
    return local(dt, zone).strftime("%H:%M %Z")


def both(dt: datetime, here: str, there: str) -> str:
    """One instant, shown in two zones, e.g. 'Oct 01 22:40 EDT  (Oct 02 04:40 CEST)'.

    Converted, never typed: the caller names both zones, because guessing the counterpart
    is wrong -- AMS and BCN share an offset, so a guess prints the same clock time twice
    and reads like agreement when it is only a bug. When the two readings genuinely come
    out identical the second is dropped rather than repeated.
    """
    a, b = fmt(dt, here), fmt(dt, there)
    return a if a == b else f"{a}  ({b})"


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
            BOS, AMS, "BOS-AMS-BA0431", True, 612.0, FLIGHT_NO["BOS-AMS-1"]),
        Leg("AMS-BCN-1", "flight", l2_dep, l2_dep + FLIGHT_TIME[(AMS, BCN)], "scheduled",
            AMS, BCN, "AMS-BCN-IB3110", True, 214.0, FLIGHT_NO["AMS-BCN-1"]),
        Leg("BCN-HOTEL-1", "hotel", hotel_in, _local(3, 11, 0, BCN), "scheduled",
            None, BCN, "BCN-HTL-88421", False, 248.0),
        Leg("BCN-ACT-1", "activity", act, act + timedelta(hours=1, minutes=30), "scheduled",
            None, BCN, "BCN-SF-55210", False, 32.0),
    ]
    return Trip(
        "TRIP-8841", legs,
        [
            Constraint("hard", f"connection at AMS: {min_connection(AMS)} min minimum", 100),
            Constraint("hard", f"hotel check-in from {hhmm(_local(*CHECK_IN, BCN), BCN)}"),
            Constraint("hard", f"Sagrada Familia entry {hhmm(act, BCN)}, non-refundable ticket"),
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
        # Per-world, not the module constant: committing a replan changes which flight a
        # leg is on, and `find_alternatives` uses this to stop offering a leg its own
        # current flight. A module-level dict would go stale on the first commit.
        self.flight_no = dict(FLIGHT_NO)
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

    def option(self, leg_id: str, flight: str) -> FlightOption | None:
        """The bookable option for `leg_id` with that flight number, or None.

        The one way to get a flight out of this world. `commit_replan` goes through it
        rather than trusting any time the model wrote, so a flight's times in a committed
        plan are always the world's own numbers.
        """
        want = (flight or "").strip().upper()
        return next((o for o in self.alternatives(leg_id) if o.flight.upper() == want), None)

    # --- mutation --------------------------------------------------------------
    def apply_replan(self, leg_id: str, option: FlightOption) -> None:
        """Re-time a leg onto an option the world itself vouched for.

        Only ever called after `commit_replan` has verified the option and cleared the
        hard constraints, so the mutation is the last step, not the first.
        """
        leg = self.leg(leg_id)
        if leg is None:
            raise KeyError(leg_id)
        leg.flight = option.flight
        leg.start = option.depart
        leg.end = option.arrive
        leg.cost_usd += option.cost_delta_usd
        leg.status = "rebooked"
        leg.booking_ref = f"REBOOK-{leg_id}-{option.flight.replace(' ', '')}"
        self.flight_no[leg_id] = option.flight