# travel-disruption-agent

An agent that notices a travel disruption, works out its blast radius across a whole
itinerary, and proposes a replan that is actually workable.

> **Status: plan only.** No code yet. See [PLAN.md](PLAN.md) for the spec and the
> milestones we'll build against.

## The idea

You fly BOS → AMS → BCN with a 40-minute connection in Amsterdam and a prepaid hotel in
Barcelona. Your first flight is delayed 95 minutes. The connection becomes unmakeable,
the second flight auto-cancels, and now your hotel night is wasted too.

A rules engine swaps in the next flight. An agent notices the delay **cascades three
bookings downstream**, weighs a later arrival against an earlier start time you didn't
ask for, checks the replacement doesn't route you somewhere your visa doesn't reach —
and then tells you why it chose what it chose.

## Why this is hard enough to be interesting

The replacement flight is a lookup. The replan is a constraint problem with soft
preferences, and the hard part is knowing *which constraints are now in play* — which
only becomes visible after you've understood what the disruption broke.

That blast-radius step is deterministic graph work. The judgement on top of it is what
the LLM is for. [PLAN.md §2](PLAN.md#2-why-this-needs-an-agent-not-a-rules-engine) has the
comparison table.

## What the demo should look like

A ticking clock, events arriving live, the itinerary mutating underneath, and the agent
responding without being asked. [PLAN.md §4](PLAN.md#4-the-dynamic-demo-loop) has the
target output:

```
  [18:42]  ! BOS-AMS delayed 95 min, now departs 20:25
           - BOS-AMS now lands 23:10; 40-min AMS connection becomes 20 min
           - AMS-BCN at risk. Replanning...

  Candidate A  BOS-AMS 20:25 -> AMS 23:10 | AMS-BCN next day 06:15 | hotel +1 night
               total +1 day, +$180
  Candidate B  Rebook BOS-AMS 17:20 -> AMS 20:05 | AMS-BCN unchanged 23:30 | no changes after
               total +0 days, +$95, but you leave work 2h earlier
```

## Build order

M0 loop + simulated world (no LLM) → M1 single-leg LLM replan → M2 multi-leg blast
radius → M3 constraints and trade-offs → M4 approval and validator → M5 real data.

M0 is deliberately LLM-free: if the loop works with a scripted planner, the model only
has to do the judgement. Full table in [PLAN.md §8](PLAN.md#8-milestones).

## Two decisions worth knowing now

**The agent can never invent a flight.** `commit_replan` refuses any leg whose flight
number, times, or cost don't appear verbatim in what `find_alternatives` returned. That
check is code, not a prompt — same principle as `calculate` never calling `eval`.
[PLAN.md §7](PLAN.md#the-safety-property-no-invented-flights).

**Simulated data in v0.** Live flight APIs need credentials, rate-limit, and fail exactly
when a demo needs them. A seeded in-process world sits behind the same tool interface, so
the real feed is a one-module swap later. [PLAN.md §4](PLAN.md#why-simulated-data-not-live-flight-apis).

## Open questions

[PLAN.md §11](PLAN.md#11-open-questions) lists what's still undecided — most importantly
whether there's a formal problem statement with judging criteria to satisfy. The
assumptions I made are in [§12](PLAN.md#12-assumptions-to-confirm); correcting those
early is much cheaper than reworking code later.