# travel-disruption-agent

An agent that notices a travel disruption, works out its blast radius across a whole
itinerary, and proposes a replan that is actually workable.

Built from the [plan](PLAN.md). **M0, M1 and M2 are done, along with the core of M4.**

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

**Optional: the Streamlit page.** Same code, wrapped — no logic of its own.

```bash
.venv/bin/pip install -r requirements-ui.txt     # streamlit only
.venv/bin/streamlit run app.py
```

Live mode is off by default. Turn it on to spend requests; the sidebar shows the counter and
the budget, and approval is an Approve/Decline pair rather than a terminal prompt.

## M0 + M1: what's built

```
models.py   Leg / Constraint / Trip, exactly PLAN.md §6. Aware UTC, no exceptions.
world.py    Seeded world: the trip, the clock, the event queue, the flights that exist.
tools.py    get_itinerary / poll_disruptions / analyse_impact / find_alternatives / commit_replan
agent.py    Manual dispatch loop + the retry/fallback ladder
main.py     The ticking clock
app.py      The same world as a page, wrapping main.py. uicore.py holds its approval logic.
tests/      79 offline checks, stdlib only, no API key
```

Run the tests with:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

### Quota is the design constraint

The model is **not** called on every tick. `poll_disruptions()` is called from ordinary
Python and only a non-empty result spends a request, so a full 8-tick demo costs one
agent invocation rather than eight. `LLM_REQUEST_BUDGET` (default 8) is a hard ceiling,
and the counter is printed on every tick and every request.

Worth knowing: a degraded API day makes this much more expensive. One M1 run hit a burst
of 503s and 429s, and the retry ladder burned 10 requests on a single disruption before
the fallback model answered. The ladder is correct behaviour, but the cap is now low
enough to bite: when it is hit the run says so and stops rather than quietly retrying.

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

`validate_plan` is still absent, and M3 is untouched. The M1 invention below is now
*enforced* rather than merely discouraged: `commit_replan` resolves each leg's flight
through the world itself, so the times in a committed plan are always the world's
numbers, and it refuses any flight or time that `find_alternatives` did not return.

### The commit gate

`commit_replan(plan, rationale)` runs four gates and mutates nothing until the last one:

1. **Parse** `leg = FLIGHT` lines. Prose is tolerated; one replan per call.
2. **Verify** every flight and any quoted times against `find_alternatives` output *for
   that leg*. `find_alternatives` also filters departed flights, so this rules out
   anything that has already left.
3. **Refuse** any plan that breaks a hard constraint: an unmakeable connection, a missed
   non-refundable timed slot, or a non-refundable hotel night landed on a later calendar
   day than it begins.
4. **Ask.** An explicit `y/n` prompt in `main.py`, injected as
   `build_tools(world, approve=...)`. No handler wired means no mutation.

Gate 4 has two shapes, because a terminal can block and a web page cannot. `main.py` waits
on `input()`. The page's hook records the proposal and raises `AwaitingApproval` instead, so
the tool returns a pending result and the Approve/Decline buttons appear on the next
render. Approving replays the model's own plan text through the same tool, so the gates run
again — and the model is not asked a second time, so it costs no request.

**The agent has already been caught inventing times.** On the first live run it proposed
the disrupted `BA 0431` as departing `00:15 EDT` and arriving `13:15 CEST`, when the
world had `22:40 EDT` and `11:40 CEST` — and it invented them for a flight nobody asked
it to rebook. Prompted not to restate times it stopped, but a prompt is not enforcement.
Gate 2 is the enforcement, and `tests/test_tools.py` asserts it on exactly that case.
[PLAN.md §7](PLAN.md#the-safety-property-no-invented-flights) has the write-up.

## Next

M3 — constraints and trade-offs: have the agent pick between two valid options and
justify the choice, rather than proposing two and letting a human choose.