# travel-disruption-agent

An agent that notices a travel disruption, works out its blast radius across a whole
itinerary, and proposes a replan that is actually workable.

Built from the [plan](PLAN.md). **M0 and M1 are done; M2+ are not.**

## The idea

You fly BOS → AMS → BCN with a 40-minute connection in Amsterdam, a prepaid
non-refundable hotel in Barcelona, and a timed Sagrada Família ticket. Your first flight
is delayed 95 minutes, so the connection becomes unmakeable and everything downstream is
in doubt.

A rules engine swaps in the next flight. The interesting part is that the delay cascades
three bookings, that the cheapest replacement fare is the *worst* outcome, and that the
best option is an earlier flight the traveller can still catch.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then paste your key in
```

**M0, no API key, no quota** — the whole simulation with a deterministic scripted replan:

```bash
.venv/bin/python main.py --no-llm
```

**M1, the real thing** — same world, the model plans the replan:

```bash
.venv/bin/python main.py
```

Useful flags: `--ticks N` (default 8), `--instant` (no sleep between ticks).

## M0 + M1: what's built

```
models.py   Leg / Constraint / Trip, exactly PLAN.md §6. Aware UTC, no exceptions.
world.py    Seeded world: the trip, the clock, the event queue, the flights that exist.
tools.py    get_itinerary / poll_disruptions / find_alternatives
agent.py    Manual dispatch loop + the retry/fallback ladder
main.py     The ticking clock
tests/      15 offline checks, stdlib only, no API key
```

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

### Quota is the design constraint

The model is **not** called on every tick. `poll_disruptions()` is called from ordinary
Python and only a non-empty result spends a request, so a full 8-tick demo costs one
agent invocation rather than eight. `LLM_REQUEST_BUDGET` (default 20) is a hard ceiling,
and the counter is printed on every tick and every request.

Worth knowing: a degraded API day makes this much more expensive. One M1 run hit a burst
of 503s and 429s, and the retry ladder burned 10 of the 20 requests on a single
disruption before the fallback model answered. The ladder is correct behaviour, but on a
bad day lower `LLM_REQUEST_BUDGET` and expect fewer retries to succeed.

### Timezones

Everything is stored as aware UTC and converted only for display, because travel bugs are
timezone bugs. The scenario is anchored in October 2026, when Boston is EDT (UTC−4, US DST
ends 1 Nov) and both Amsterdam and Barcelona are CEST (UTC+2, EU DST ends 25 Oct). So AMS
and BCN share an offset — the AMS-BCN leg crosses no timezone at all — and sit exactly 6h
ahead of Boston.

No time is written down twice. Each leg's start is a real local wall-clock time in the
right zone and its end is `start + flight_time`, which is why the delay moves both
endpoints and cannot change the 7h flight length.

### The 95-minute cascade

| | booked | after the delay |
|---|---|---|
| BA 0431 departs | Oct 01 21:05 EDT | Oct 01 22:40 EDT |
| BA 0431 lands | Oct 02 10:05 CEST | Oct 02 11:40 CEST |
| connection at AMS | +40 min | **−55 min** |

The connection is already 55 minutes gone when the delay lands, so `AMS-BCN-1` cannot
fly as booked and the hotel night and activity are suddenly in question.

### The trap

`IB 3108` at **+$60** is the cheapest fare in the whole table and the worst outcome: next
day, so the non-refundable hotel night is burned and the timed ticket is lost. The right
answer is usually `AF 0089` at +$320, because it leaves *earlier* than the delayed flight
and lands three hours before the onward connection, leaving the rest of the trip alone.

The live M1 run found that on its own, and correctly named the activity constraint as
broken when it took the cheap option instead.

## What is not built yet

`analyse_impact`, `validate_plan` and `commit_replan` are deliberately absent. The
blast-radius lines in the demo output are printed by `main.py` as a clearly-labelled
stopgap, so M1 doesn't pretend to a capability that isn't there.

**The agent has already been caught inventing times.** On the first live run it proposed
the disrupted `BA 0431` as departing `00:15 EDT` and arriving `13:15 CEST`, when the
world had `22:40 EDT` and `11:40 CEST` — and it invented them for a flight nobody asked
it to rebook. Prompted not to restate times it stopped, but a prompt is not enforcement.
`validate_plan` (M4) is the real fix, and it should be the first thing after M2.
[PLAN.md §7](PLAN.md#the-safety-property-no-invented-flights) has the write-up.

## Next

M2 — implement `analyse_impact` as a real tool and let the model walk the cascade itself,
instead of `main.py` printing it.