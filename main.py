"""The watch loop: a simulated clock that ticks, and an agent that only wakes up when
the world actually changes something.

The LLM is not consulted on every tick. `poll_disruptions()` is called from ordinary
Python, and only a non-empty result spends a request -- at ~20 requests/day/model that
is the whole difference between a demo you can run all day and one you cannot.

`--no-llm` runs the identical world with a deterministic scripted replan and zero model
requests. That is the M0 acceptance test: if the plumbing is wrong, --no-llm shows it.
"""
import argparse
import sys
import time

from tools import TOOLS, build_tools, check_tool_sync
from world import FLIGHT_NO, TICK_MINUTES, World, fmt

TICKS = 8
TICK_SECONDS = 1.5


def show_cascade(world: World, event) -> None:
    """Placeholder for `analyse_impact`, which is M2. Plain Python, no LLM.

    Walks forward from the disrupted leg and reports what the delay now makes impossible.
    """
    leg, nxt = world.leg(event.leg_id), world.next_leg(event.leg_id)
    print(f"           - {leg.id} now lands {fmt(leg.end, leg.destination)}")
    if nxt is None:
        return
    slack = (nxt.start - leg.end).total_seconds() / 60
    verdict = "unmakeable as booked" if slack < 0 else "still makeable"
    print(f"           - {nxt.id} departs {fmt(nxt.start, nxt.origin or nxt.destination)}; "
          f"connection is {slack:+.0f} min -> {verdict}")
    if slack >= 0:
        return
    print("           - downstream commitments now at risk:")
    for later in world.trip.legs:
        if later.start <= leg.end:
            continue
        flag = " NON-REFUNDABLE" if not later.refundable else ""
        when = f"check-in {fmt(later.start, later.destination)}" if later.kind == "hotel" \
            else f"starts {fmt(later.start, later.destination)}"
        print(f"             {later.id} ({later.kind}) {when}{flag}")


def scripted_replan(world: World, event) -> None:
    """M0's deterministic replanner: no model, no quota. Ranks by rule, not judgement."""
    leg, nxt = world.leg(event.leg_id), world.next_leg(event.leg_id)
    opts = world.alternatives(event.leg_id)
    preserving = [o for o in opts if nxt is not None and o.arrive <= nxt.start]
    same_day = [o for o in opts if o.arrive.date() == leg.end.date()]
    a = min(preserving, key=lambda o: o.arrive) if preserving else None
    b = min(same_day, key=lambda o: o.cost_delta_usd) if same_day else None

    print("           - Replanning (scripted, M0: no model call)...\n")
    if a:
        print(f"  Candidate A  {a.flight}  {fmt(a.depart, a.origin)} -> {fmt(a.arrive, a.destination)}"
              f"   +${a.cost_delta_usd:,.0f}")
        print(f"               {a.note}")
        print(f"               nothing after {leg.id} has to change")
    else:
        print("  Candidate A  (none) -- no option still makes the onward connection")
    if b:
        print(f"  Candidate B  {b.flight}  {fmt(b.depart, b.origin)} -> {fmt(b.arrive, b.destination)}"
              f"   +${b.cost_delta_usd:,.0f}")
        print(f"               {b.note}")
    print(f"\n  [M0] {len(opts)} option(s) returned; nothing committed.\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Travel disruption agent - M0 + M1.")
    ap.add_argument("--no-llm", action="store_true", help="scripted replan, zero model requests")
    ap.add_argument("--ticks", type=int, default=TICKS, help=f"clock ticks to run (default {TICKS})")
    ap.add_argument("--instant", action="store_true", help="no sleep between ticks")
    args = ap.parse_args()

    world = World(seed=7)
    impls = build_tools(world)
    check_tool_sync(impls)

    agent = client = config = None
    if not args.no_llm:
        import agent as agent_mod
        from agent import make_client_and_config
        agent = agent_mod
        client, config = make_client_and_config(TOOLS)
    else:
        print("M0 mode: simulated world + scripted replan, zero model requests.\n")

    l1, l2 = world.trip.legs[0], world.trip.legs[1]
    print(f"Trip {world.trip.id}: home ({l1.origin}) {fmt(l1.start, l1.origin)}  ->  "
          f"{l1.origin}-{l1.destination}  ->  {l2.origin}-{l2.destination}  ->  hotel in {l2.destination}")
    print(f"Connection at {l2.origin}: {(l2.start - l1.end).total_seconds() / 60:.0f} min. "
          f"Flight {FLIGHT_NO[l1.id]} is the one at risk.\n")

    for tick in range(1, args.ticks + 1):
        world.tick(TICK_MINUTES if tick > 1 else 0)
        stamp = fmt(world.now, "BOS")
        counter = "llm off" if agent is None else f"llm {agent.REQUESTS_USED}/{agent.REQUEST_BUDGET}"
        print(f"  [tick {tick}] {stamp}  ({counter})")

        fresh = world.peek()
        if not fresh:
            if not args.instant:
                time.sleep(TICK_SECONDS)
            continue

        # The tool consumes these same events, so its text and the objects agree.
        for line in impls["poll_disruptions"]().splitlines():
            print(f"{' ' * 11}{line}")
        for event in fresh:
            show_cascade(world, event)
            if args.no_llm:
                scripted_replan(world, event)
            else:
                print("           - Replanning (agent)...\n")
                agent.handle_disruption(client, config, impls, world, [event])

    print("Loop finished. Nothing was booked or committed.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped.")