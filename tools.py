"""The agent's tools. Three places must stay in sync: the FunctionDeclaration list,
`TOOLS`, and `TOOL_IMPLS` (built by `build_tools`). `check_tool_sync()` enforces it at
startup so a mismatch fails loudly instead of at the first tool call.

Tools return readable text and never raise: errors come back as a string the model can
read and react to, rather than killing the run.
"""
from google.genai import types

from world import FLIGHT_NO, World, fmt

TOOLS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="get_itinerary",
        description="Current itinerary: every leg with local times, status and cost, plus the constraints and preferences.",
        parameters_json_schema={"type": "object", "properties": {}}),
    types.FunctionDeclaration(
        name="poll_disruptions",
        description="Disruptions announced since the last poll. Empty means nothing new.",
        parameters_json_schema={"type": "object", "properties": {}}),
    types.FunctionDeclaration(
        name="find_alternatives",
        description="Replacement flights for a leg, with fare delta and arrival time.",
        parameters_json_schema={"type": "object", "properties": {
            "leg_id": {"type": "string", "description": "Leg id, e.g. 'BOS-AMS-1'"},
            "avoid": {"type": "string", "description": "Comma-separated flight numbers to skip"}},
            "required": ["leg_id"]}),
])]


def _describe(leg) -> str:
    if leg.origin:
        when = f"{fmt(leg.start, leg.origin)}  ->  {fmt(leg.end, leg.destination)}"
    else:
        when = f"from {fmt(leg.start, leg.destination)}"
    money = f"${leg.cost_usd:,.0f}" + ("" if leg.refundable else "  NON-REFUNDABLE")
    return f"  {leg.id:<14} {leg.kind:<9} {when:<46} {leg.status:<10} {money}"


def build_tools(world: World) -> dict:
    """Bind the tools to this world. Called once at startup."""

    def get_itinerary() -> str:
        try:
            t = world.trip
            hard = [c for c in t.constraints if c.kind == "hard"]
            soft = [c for c in t.constraints if c.kind == "soft"]
            lines = [
                f"Trip {t.id}   now: {fmt(world.now, 'BOS')} ({fmt(world.now, 'AMS')})",
                "",
                "LEGS",
                *[_describe(l) for l in t.legs],
                "",
                "HARD CONSTRAINTS (do not break)",
                *[f"  - {c.name}" for c in hard],
                "",
                "SOFT CONSTRAINTS (weigh up, higher weight wins)",
                *[f"  - [{c.weight}] {c.name}" for c in soft],
                "",
                "PREFERENCES",
                f"  {t.prefs}",
            ]
            return "\n".join(lines)
        except Exception as exc:
            return f"error: {exc}"

    def poll_disruptions() -> str:
        try:
            events = world.poll()
            if not events:
                return "No new disruptions."
            out = [f"{len(events)} new disruption(s):"]
            for e in events:
                leg = world.leg(e.leg_id)
                detail = ""
                if leg:
                    detail = (f"  Now departs {fmt(leg.start, leg.origin)}, "
                              f"arrives {fmt(leg.end, leg.destination)}.")
                out.append(f"  - [{e.kind.upper()}] {e.summary}. Affected leg {e.leg_id}.{detail}")
            return "\n".join(out)
        except Exception as exc:
            return f"error: {exc}"

    def find_alternatives(leg_id: str, avoid: str = "") -> str:
        try:
            leg = world.leg(leg_id)
            if leg is None:
                return f"error: no leg {leg_id!r}. Known legs: {[l.id for l in world.trip.legs]}"
            if not leg.origin or not leg.destination:
                return f"error: {leg_id} is a {leg.kind}, not a flight. Only flights have alternatives."
            skip = {t.strip().upper() for t in (avoid or "").split(",") if t.strip()}
            skip.add(FLIGHT_NO.get(leg_id, "").upper())
            options = world.alternatives(leg_id)
            kept = [o for o in options if o.flight.upper() not in skip]
            if not kept:
                return (f"No options remain for {leg_id}. Everything still listed has either departed "
                        f"or was excluded.")
            lines = [f"{len(kept)} option(s) for {leg_id} ({leg.origin} -> {leg.destination}), "
                     f"all departing after now ({fmt(world.now, leg.origin)}):", ""]
            for o in kept:
                line = (f"  {o.flight}  {fmt(o.depart, o.origin)} -> {fmt(o.arrive, o.destination)}"
                        f"   +${o.cost_delta_usd:,.0f}")
                if o.note:
                    line += f"\n      {o.note}"
                lines.append(line)
            dropped = world.excluded_count(leg_id) + (len(options) - len(kept))
            lines.append(f"\n({dropped} excluded: already departed, or in `avoid`)")
            lines.append("Only these flights exist. Do not propose any other flight number or time.")
            return "\n".join(lines)
        except Exception as exc:
            return f"error: {exc}"

    return {"get_itinerary": get_itinerary, "poll_disruptions": poll_disruptions,
            "find_alternatives": find_alternatives}


def check_tool_sync(impls: dict) -> None:
    """Fail at startup if a declaration, TOOLS and TOOL_IMPLS have drifted apart."""
    declared = {d.name for t in TOOLS for d in t.function_declarations}
    if declared != set(impls):
        raise SystemExit(f"tool mismatch: declarations={sorted(declared)} impls={sorted(impls)}")