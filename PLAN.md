# Plan: AI Travel Disruption & Autonomous Replanning Agent

> Status: **M0 and M1 built** (see [README](README.md#m0--m1-whats-built)). M2+ are still just
> this document. Assumptions I had to make are collected in
> [§12](#12-assumptions-to-confirm) — correct those before M2.

## 1. The problem

A traveller is mid-trip with a booked itinerary: flights, connections, hotels, car hire,
a reservation they can't easily move. Something disrupts it — a cancellation, a delay, a
gate change, a strike, weather, a missed connection. Now every downstream commitment is
at risk:

- Their 40-minute connection in Amsterdam becomes unmakeable, so leg 2 auto-cancels.
- The rebooked flight lands at 01:40, so the prepaid non-refundable hotel night is wasted.
- The only remaining option requires a visa the traveller doesn't hold for that routing.

The agent's job is to notice the disruption, understand its **blast radius across the
whole itinerary**, and produce a new plan that is actually workable — not just a
replacement flight.

## 2. Why this needs an agent, not a rules engine

A rule engine can swap a cancelled flight for the next one on the same route. It falls
over on everything that makes this interesting:

| Capability | Script | Agent |
| --- | --- | --- |
| Re-sequence or drop a leg | needs a hand-written rule per case | reasons over the graph |
| Notice a delay cascades 3 bookings downstream | no | yes, via dependency walk |
| Weigh cost against "must be home before Monday" | encode as if/else | soft constraint, judged |
| Choose between two imperfect options | impossible | explains the trade-off |
| Notice a hard constraint is unsatisfiable and *say so* | fails silently | refuses honestly |

The MVP should prove the right-hand column, on the row that matters most: **blast radius
across a multi-leg itinerary.**

## 3. MVP scope

### In scope

- A single trip with **3–5 legs** (2+ flights, a connection, a hotel, one activity).
- **Simulated** live disruption feed (see §4) — no real flight APIs in v0.
- Detect a disruption, reason over the itinerary, emit a candidate replan.
- Replan must respect **hard constraints** (connections, visa, cost ceiling) and
  **soft constraints** (preferences, minimal cost, no overnight airport).
- Human approval before anything is committed.

### Explicitly out of scope

- Real booking, ticketing, payment, or anything that spends money.
- Real airline/hotel APIs behind auth. (§4 explains why.)
- Multi-traveller coordination (groups, family splits, shared bookings).
- Guarantees, insurance claims, compensation eligibility (EU261 etc.).
- Mobile app, notifications to real people.

## 4. The "dynamic" demo loop

The demo should look *alive*: the world changes while the agent is idle and it responds
on its own. A ticking clock in the terminal, events streaming in, the itinerary mutating
underneath.

```
You:  home (BOS) 18:40  ->  BOS-AMS 21:05  ->  AMS-BCN 23:30  ->  hotel in Barcelona

  [18:42]  ! BOS-AMS delayed 95 min, now departs 20:25
           - BOS-AMS now lands 23:10; 40-min AMS connection becomes 20 min
           - AMS-BCN at risk. Replanning...

  Candidate A  BOS-AMS 20:25 -> AMS 23:10 | AMS-BCN next day 06:15 | hotel +1 night (refundable)
               total +1 day, +$180
  Candidate B  Rebook BOS-AMS 17:20 -> AMS 20:05 | AMS-BCN unchanged 23:30 | no changes after
               total +0 days, +$95, but you leave work 2h earlier

  Replan B applied. Traveller notified.
```

That output is the whole product. Everything in this repo exists to produce it.

### Why simulated data, not live flight APIs

A real feed needs credentials, is rate-limited, and returns errors exactly when a demo
needs it most. Worse, its failure modes are indistinguishable from the agent failing.
So: a deterministic in-process world with a seeded RNG, exposed behind the *same* tool
interface a real feed would use. Swapping in live data later is a one-module change, and
the demo never depends on a third party.

## 5. Architecture

Deliberately mirrors [`agent-starter`](https://github.com/himanshuxp/gemini-tool-agent) — manual function calling, no
framework, one process. You already built and debugged that harness; reuse it.

```
travel/
  main.py          # REPL + the autonomous watch loop
  world.py         # simulated disruptions, seeded RNG, event queue
  models.py        # Trip, Leg, Booking, Constraint  (dataclasses)
  tools.py         # function declarations + impls (three-places rule, see below)
  agent.py         # the dispatch loop: call -> function_calls -> FunctionResponse
  policy.py        # hard-constraint validator  <- the safety net, §7
```

Rules carried over from `agent-starter/AGENTS.md`:

- Manual dispatch. `automatic_function_calling` disabled; echo `call.id` on every
  `FunctionResponse`; append model turns **verbatim** to preserve `thought_signature`.
- Keep the retry/fallback ladder (429/503 back off 2s/5s/10s, then fail over to the
  next model) rather than reinventing it.
- Adding a tool touches three places that must stay in sync: the `FunctionDeclaration`
  list, `TOOLS`, and `TOOL_IMPLS`.

## 6. Data model

```python
@dataclass
class Leg:
    id: str                  # "AMS-BCN-1"
    kind: str                # flight | hotel | train | activity
    start: datetime          # aware, UTC
    end: datetime
    status: str              # scheduled | delayed | cancelled | done
    origin: str | None       # IATA
    destination: str | None
    booking_ref: str | None
    refundable: bool
    cost_usd: float

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
```

Timezone-aware datetimes only. Travel bugs are timezone bugs.

## 7. Tools

Six, in rough call order. Descriptions are terse on purpose — they steer model behaviour.

| Tool | Signature | Purpose |
| --- | --- | --- |
| `get_itinerary` | `() -> str` | Current legs + constraints + prefs |
| `poll_disruptions` | `() -> str` | New events since last poll (the dynamic feed) |
| `analyse_impact` | `(leg_id) -> str` | **Blast radius** — which other legs/breaks |
| `find_alternatives` | `(leg_id, avoid) -> str` | Real bookable options for that leg |
| `validate_plan` | `(plan) -> str` | Run the hard-constraint checks; pass/fail + why |
| `commit_replan` | `(plan, rationale) -> str` | Apply after approval; mutates the trip |

`analyse_impact` is the one that makes this an agent rather than a script, and the one
worth building first. It's a dependency walk over `Trip.legs` — deterministic code, no
LLM — exposed as a tool so the model *has* to consult it rather than guess.

### The safety property: no invented flights

The single failure that would sink this project is the agent confidently emitting a
replan citing flight `BA 0431 at 06:15` that was never offered by `find_alternatives`.

Therefore:

> `commit_replan` **refuses** any leg whose flight number, times, or cost don't appear
> verbatim in that leg's `find_alternatives` output. This check lives in code, not in a
> prompt, and it is not optional.

Never let the model be the authority on whether a flight exists. Same principle as
`calculate` not calling `eval`: the sandbox is the invariant, the prompt is a courtesy.

> **Observed in M1, 2026-10-01.** The live run got the *reasoning* right -- it found
> AF 0089 and correctly flagged that VY 8901 breaks the activity constraint -- while
> inventing a departure and arrival time for the already-disrupted BA 0431 (it wrote
> 00:15 EDT / 13:15 CEST; the world had 22:40 EDT / 11:40 CEST). Note it invented times
> for a flight it had not been asked to rebook. Prompted not to restate times, it
> stopped; but "stopped doing it when told" is not enforcement. **This is the concrete
> case for M4, and it is the first thing to build after M2.**

## 8. Milestones

Each one ends in something runnable and demonstrable.

| # | Deliverable | Done when |
| --- | --- | --- |
| **M0** | Loop + simulated world, **no LLM** | ✅ Ticking clock, deterministic scripted replan on the delay. `--no-llm`. |
| **M1** | LLM drives a single-leg disruption | ✅ `poll_disruptions` → `find_alternatives` → two candidates |
| **M2** | Multi-leg blast radius | `analyse_impact` implemented; delay cascades ≥2 legs correctly |
| **M3** | Constraints & trade-offs | Agent picks between two valid options and justifies it |
| **M4** | Approval + validator | `validate_plan` blocks a bad plan; commit requires approval |
| **M5** | Real data adapters | One real source behind the same tool interface |

M0 deliberately has no LLM. If the loop works with a scripted planner, the LLM only has
to do the *judgement*, which is the part it's actually good at.

## 9. How we'll know it's working

Eval is where these projects usually go to die, so keep it small and cheap:

- **Hard-constraint violation rate** — must be 0. The validator makes this measurable.
- **Does it catch the cascade?** Does `analyse_impact` surface every affected leg, or
  just the obvious one? This is the core capability; test it directly.
- **Cost delta** vs. the cheapest legal plan it found.
- **Time to replan** — seconds, not minutes.
- **Honesty** — when nothing legal exists, does it say so instead of inventing one?

A scripted suite over fixed scenarios (delay / cancellation / missed connection /
hard-constraint-unsatisfiable) beats free-form eval. No LLM-as-judge at MVP size.

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Agent invents flights | §7 validator, enforced in code |
| Scope creep into booking/money | Out-of-scope list in §3; commit is a simulation, always |
| Simulated data drifts from reality | Adapter seam (§4); real feed in M5, not before |
| LLM latency makes the demo boring | Cache the world; stream the reasoning |
| Non-determinism flakiness | Seeded RNG + scripted eval scenarios |

## 11. Open questions

1. Is there an actual problem statement to satisfy (judging criteria, submission
   format, team size)? I couldn't find one on this machine.
2. Single traveller, or groups with shared bookings?
3. Does it need to *execute* bookings, or only *propose* them? Plan assumes propose-only.
4. Is a terminal demo the target, or is a web UI expected?
5. Should the travel domain be hard-coded, or kept general enough to re-target later?

## 12. Assumptions to confirm

Made by me, unverified. Each is cheap to change now and expensive later.

1. **Proposed replans only** — never book, never spend money.
2. **Simulated disruptions in v0** — real APIs later, behind the same tools.
3. **Flights + hotel + one activity** is enough to show blast radius. Adding trains and
   car hire is a `Leg.kind` change, not new architecture.
4. **Terminal demo**, single process, no web UI in the MVP.
5. **One traveller**, personal trip, no groups or multi-party negotiation.
6. **Reuse the `agent-starter` harness** rather than CrewAI or LangChain — manual
   dispatch, single file, debuggable. (`~/crew_test` suggests CrewAI was explored;
   my read is that hand-rolled wins here because the interesting logic is the
   dependency walk, which is plain Python either way.)
7. **Gemini primary, Groq fallback**, matching `agent-starter` and `crew_test`.