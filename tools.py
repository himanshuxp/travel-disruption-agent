"""The agent's tools. Three places must stay in sync: the FunctionDeclaration list,
`TOOLS`, and `TOOL_IMPLS` (built by `build_tools`). `check_tool_sync()` enforces it at
startup so a mismatch fails loudly instead of at the first tool call.

Tools return readable text and never raise: errors come back as a string the model can
read and react to, rather than killing the run.
"""
import re
from datetime import timedelta

from google.genai import types

from world import World, both, fmt, local, min_connection

# `leg = FLIGHT`, one per line. The flight number is the only required field: it is the
# key into the world's own bookable options, so the times in a committed plan are always
# the world's numbers and never the model's. Times may be added and are checked when
# present -- see `_parse_plan`.
PLAN_FORMAT = ("Format, one leg per line:\n"
               "  BOS-AMS-1 = AF 0089\n"
               "  AMS-BCN-1 = IB 3114, Oct 02 13:20 CEST -> Oct 02 15:25 CEST\n"
               "Omit any leg you are leaving as it stands. One replan per call.")

TIME_RE = re.compile(r"\d{1,2}:\d{2}(?:\s+[A-Z]{2,4})?")
ASSIGN_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)\s*[:=]\s*(.+)$", re.I)
# Tolerates the model dropping a leading zero ("AF 89"); the number is re-padded to four
# digits before it is looked up, because the world's flight numbers are zero-padded.
FLIGHT_RE = re.compile(r"\b([A-Za-z]{2})\s*(\d{1,4})\b")

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
        name="analyse_impact",
        description=(
            "Blast radius of a delay: walks every later leg and reports which ones break, by how "
            "much, and how much non-refundable spend is at risk. Every time is already converted to "
            "local zones, including the deadline each later leg imposes, so you can compare an "
            "arrival against it without doing arithmetic. Call this for the disrupted leg BEFORE "
            "find_alternatives. It never names a flight."),
        parameters_json_schema={"type": "object", "properties": {
            "leg_id": {"type": "string", "description": "Leg id, e.g. 'BOS-AMS-1'"}},
            "required": ["leg_id"]}),
    types.FunctionDeclaration(
        name="find_alternatives",
        description="Replacement flights for a leg, with fare delta and arrival time.",
        parameters_json_schema={"type": "object", "properties": {
            "leg_id": {"type": "string", "description": "Leg id, e.g. 'BOS-AMS-1'"},
            "avoid": {"type": "string", "description": "Comma-separated flight numbers to skip"}},
            "required": ["leg_id"]}),
    types.FunctionDeclaration(
        name="commit_replan",
        description=(
            "Apply ONE chosen replan, after asking the traveller. Checks every flight against "
            "find_alternatives and refuses any that is not listed there, then refuses any plan "
            "that breaks a hard constraint. Returns a refusal rather than raising, so a wrong "
            "plan costs you nothing but a retry."),
        parameters_json_schema={"type": "object", "properties": {
            "plan": {"type": "string", "description": PLAN_FORMAT},
            "rationale": {"type": "string",
                          "description": "Why this plan beats the alternative, in a sentence."}},
            "required": ["plan", "rationale"]}),
])]


def _describe(leg) -> str:
    if leg.origin:
        when = f"{fmt(leg.start, leg.origin)}  ->  {fmt(leg.end, leg.destination)}"
    else:
        when = f"from {fmt(leg.start, leg.destination)}"
    money = f"${leg.cost_usd:,.0f}" + ("" if leg.refundable else "  NON-REFUNDABLE")
    return f"  {leg.id:<14} {leg.kind:<9} {when:<46} {leg.status:<10} {money}"


def build_tools(world: World, approve=None) -> dict:
    """Bind the tools to this world. Called once at startup.

    `approve(summary, rationale) -> bool` is the human in the loop for `commit_replan`,
    supplied by the caller so main.py owns the prompt and tests can inject a stub. With no
    handler, `commit_replan` refuses rather than mutating the trip unattended.
    """

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

    def _blast_radius(leg) -> list[dict]:
        """Walk every leg after `leg` and classify it. Deterministic Python, no LLM.

        `reach` is when the traveller gets to the city a leg sits in, on the bookings as
        they now stand (the delay is already applied, because the world applied it). Once a
        connection is unmakeable `unfixed` goes true and no later time can be trusted, so
        downstream non-refundable bookings are AT RISK rather than BROKEN -- we cannot say
        they are missed until we know what replaces the missing flight.
        """
        legs = world.trip.legs
        rows, reach, unfixed = [], leg.end, False
        for nxt in legs[legs.index(leg) + 1:]:
            margin = (nxt.start - reach).total_seconds() / 60
            row = {"leg": nxt, "reach": reach, "margin": margin}
            if nxt.kind == "flight":
                need = min_connection(nxt.origin)
                row["need"] = need
                row["ok"] = margin >= need
                row["state"] = "OK" if row["ok"] else "BROKEN"
                unfixed = unfixed or not row["ok"]
                reach = nxt.end
            elif unfixed:
                row["state"] = "AT RISK"
            elif margin < 0:
                row["state"] = "BROKEN"
                unfixed = True
            else:
                row["state"] = "OK"
            if row["state"] != "OK" and not nxt.refundable:
                row["money"] = nxt.cost_usd
            rows.append(row)
        return rows

    def analyse_impact(leg_id: str = "") -> str:
        try:
            leg = world.leg(leg_id)
            if leg is None:
                return f"error: no leg {leg_id!r}. Known legs: {[l.id for l in world.trip.legs]}"
            if not leg.origin or not leg.destination:
                return f"error: {leg_id} is a {leg.kind}, not a flight."

            # Every downstream time is paired against Boston: that is where the traveller
            # is now and where any replacement departs from, so the pair is the comparison
            # the model actually has to make.
            home = leg.origin
            out = [f"IMPACT OF {leg_id}   world clock {both(world.now, leg.origin, leg.destination)}", ""]
            out.append(f"  {leg_id}  {leg.status.upper()}  {leg.flight or ''}".rstrip())
            out.append(f"    departs   {both(leg.start, leg.origin, leg.destination)}")
            out.append(f"    arrives   {both(leg.end, leg.destination, leg.origin)}")
            out.append("")

            rows = _blast_radius(leg)
            money, broken_conns, at_risk = 0.0, 0, []
            for row in rows:
                nxt = row["leg"]
                hurt = row["state"] != "OK"
                money_flag = (f"  ${nxt.cost_usd:,.0f} NON-REFUNDABLE"
                              if hurt and not nxt.refundable else "")
                if hurt:
                    money += row.get("money", 0.0)
                if nxt.kind == "flight":
                    if row["ok"]:
                        out.append(f"  {nxt.id}  connection holds, {row['margin'] - row['need']:+.0f} "
                                   f"min over the {row['need']} min minimum")
                        out.append(f"    departs   {both(nxt.start, nxt.origin, home)}")
                    else:
                        broken_conns += 1
                        out.append(f"  {nxt.id}  BROKEN  connection unmakeable, you land "
                                   f"{-row['margin']:.0f} min after it departs")
                        out.append(f"    departs   {both(nxt.start, nxt.origin, home)}")
                        out.append(f"    you land  {both(row['reach'], nxt.origin, home)}")
                        out.append(f"    so a replacement must reach {nxt.origin} by "
                                   f"{fmt(nxt.start - timedelta(minutes=row['need']), nxt.origin)} "
                                   f"or earlier, to keep the {row['need']} min connection")
                else:
                    if hurt:
                        at_risk.append(nxt)
                    out.append(f"  {nxt.id}  {row['state']}{money_flag}")
                    out.append(f"    {nxt.kind:<9} {both(nxt.start, nxt.destination, home)} -> "
                               f"{both(nxt.end, nxt.destination, home)}")
                    if row["state"] == "AT RISK":
                        out.append("    arrival is unfixed until a replacement is chosen, "
                                   "so this is at risk, not yet missed")
                    elif row["state"] == "BROKEN":
                        out.append(f"    you land {fmt(row['reach'], nxt.destination)}, "
                                   f"{-row['margin']:.0f} min too late to use it")
                    else:
                        out.append(f"    you land {fmt(row['reach'], nxt.destination)}, "
                                   f"{row['margin']:.0f} min before it starts")
                out.append("")

            if at_risk:
                out.append(f"AT RISK: ${money:,.0f} of non-refundable bookings, across "
                           f"{', '.join(l.id for l in at_risk)}.")
            if broken_conns:
                out.append(f"UNMAKEABLE: {broken_conns} downstream connection(s).")
            if not at_risk and not broken_conns:
                out.append("Nothing downstream of this leg is affected.")
            out.append("No flight is named here and no time is computed for one. "
                       "Call find_alternatives, then compare each arrival against the "
                       "deadlines above.")
            return "\n".join(out)
        except Exception as exc:
            return f"error: {exc}"

    def find_alternatives(leg_id: str = "", avoid: str = "") -> str:
        try:
            leg = world.leg(leg_id)
            if leg is None:
                return f"error: no leg {leg_id!r}. Known legs: {[l.id for l in world.trip.legs]}"
            if not leg.origin or not leg.destination:
                return f"error: {leg_id} is a {leg.kind}, not a flight. Only flights have alternatives."
            skip = {t.strip().upper() for t in (avoid or "").split(",") if t.strip()}
            skip.add(world.flight_no.get(leg_id, "").upper())
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

    def _parse_plan(plan: str) -> tuple[list[dict], list[str]]:
        """Pull `leg = FLIGHT` assignments out of the model's prose.

        Forgiving about shape, strict about content. Surrounding prose is skipped, so the
        model can put the assignment in a sentence. Times are collected but not required:
        the first two clock times on a line are taken as the departure and arrival and are
        checked against the tool output, so a model that volunteers times cannot slip past
        with invented ones.
        """
        known = {l.id for l in world.trip.legs}
        rows: dict[str, dict] = {}
        bad: list[str] = []
        for raw in (plan or "").splitlines():
            line = raw.strip().lstrip("-*• \t")
            if not line:
                continue
            m = ASSIGN_RE.search(line)
            if not m:
                continue                       # prose, not an assignment
            leg_id, rest = m.group(1).upper(), m.group(2)
            if leg_id not in known:
                bad.append(f"{leg_id!r} is not a leg on this trip. Legs: {sorted(known)}.")
                continue
            fm = FLIGHT_RE.search(rest)
            if not fm:
                bad.append(f"{leg_id}: no flight number in {rest.strip()!r}. "
                           f'Write it as "{leg_id} = AF 0089".')
                continue
            row = {"leg_id": leg_id,
                   "flight": f"{fm.group(1).upper()} {int(fm.group(2)):04d}",
                   "times": TIME_RE.findall(rest)[:2]}
            if leg_id in rows and rows[leg_id]["flight"] != row["flight"]:
                bad.append(f"{leg_id} appears twice with different flights "
                           f"({rows[leg_id]['flight']} and {row['flight']}). "
                           f"Commit one replan at a time.")
                continue
            rows[leg_id] = row
        return list(rows.values()), bad

    def _project(rows: list[dict]) -> dict:
        """Projected (start, end) per leg id if these assignments were committed."""
        proj = {l.id: (l.start, l.end) for l in world.trip.legs}
        for row in rows:
            o = world.option(row["leg_id"], row["flight"])
            if o is not None:
                proj[row["leg_id"]] = (o.depart, o.arrive)
        return proj

    def _hard_failures(rows: list[dict]) -> list[str]:
        """Which hard constraints the projected itinerary breaks.

        A non-refundable booking that the traveller cannot use is not a cheaper outcome,
        it is a more expensive one, and this is the check that says so. Two rules:

          - a timed slot has to still be there when you land, so arrival must be at or
            before it starts;
          - a hotel night is charged per night, so it is burned if you land on a later
            calendar day than the night begins. Arriving *after* check-in opens on the
            same day costs a few minutes of standing around, which is not a breach --
            only losing the night is.
        """
        proj, out, prev = _project(rows), [], None
        for leg in world.trip.legs:
            start, end = proj[leg.id]
            if leg.kind == "flight":
                if prev is not None and prev.destination == leg.origin:
                    need = min_connection(leg.origin)
                    margin = (start - proj[prev.id][1]).total_seconds() / 60
                    if margin < need:
                        out.append(f"{leg.id} cannot be made: it departs "
                                   f"{fmt(start, leg.origin)}, and the flight before it only "
                                   f"lands {fmt(proj[prev.id][1], leg.origin)} "
                                   f"({margin:+.0f} min connection, {need} min required).")
                prev = leg
                continue
            if prev is None or leg.refundable:
                continue
            arrival = proj[prev.id][1]
            if leg.kind == "activity" and arrival >= start:
                out.append(f"{leg.id} is missed entirely: the ${leg.cost_usd:,.0f} non-refundable "
                           f"slot runs {fmt(start, leg.destination)} to {fmt(end, leg.destination)}, "
                           f"and you only land {fmt(arrival, leg.destination)}.")
            elif leg.kind == "hotel" and local(arrival, leg.destination).date() > \
                    local(start, leg.destination).date():
                usable = (end - max(arrival, start)).total_seconds() / 3600
                out.append(f"{leg.id} is burned: the ${leg.cost_usd:,.0f} non-refundable night "
                           f"runs {fmt(start, leg.destination)} to {fmt(end, leg.destination)}, "
                           f"but you land {fmt(arrival, leg.destination)} -- {usable:.1f}h of a "
                           f"{(end - start).total_seconds() / 3600:.0f}h night, the rest gone.")
        return out

    def commit_replan(plan: str = "", rationale: str = "") -> str:
        try:
            rows, bad = _parse_plan(plan)
            if not rows:
                why = "; ".join(bad) if bad else "no `leg = FLIGHT` assignment was found in it"
                return f"REFUSED. Nothing changed: {why}.\n{PLAN_FORMAT}"
            if bad:
                return f"REFUSED. Nothing changed:\n" + "\n".join(
                    f"  - {b}" for b in bad) + f"\n{PLAN_FORMAT}"

            # Gate 2: the flight and any quoted times must be in this tool's own output for
            # that leg. `find_alternatives` filters departed flights, so this also rules
            # out anything that has already left.
            invented = []
            for row in rows:
                text = find_alternatives(row["leg_id"])
                if row["flight"].upper() not in text.upper():
                    invented.append(f"{row['leg_id']} = {row['flight']}: find_alternatives does "
                                    f"not offer that flight for this leg.")
                for t in row["times"]:
                    if t.upper() not in text.upper():
                        invented.append(f"{row['leg_id']}: you wrote the time {t!r}, which does "
                                        f"not appear in find_alternatives output for this leg.")
            if invented:
                return ("REFUSED. Nothing changed, because a leg in this plan was never offered:\n"
                        + "\n".join(f"  - {i}" for i in invented)
                        + "\nOnly propose flights find_alternatives returned, and copy their "
                          "times exactly, or omit the times entirely.")

            failures = _hard_failures(rows)
            if failures:
                return ("REFUSED. Nothing changed, because this plan breaks a hard constraint:\n"
                        + "\n".join(f"  - {f}" for f in failures)
                        + "\nA cheaper fare is not a cheaper outcome if it burns a "
                          "non-refundable booking. Pick an option that keeps the trip whole.")

            lines, delta = [], 0.0
            for row in rows:
                opt, leg = world.option(row["leg_id"], row["flight"]), world.leg(row["leg_id"])
                delta += opt.cost_delta_usd
                lines.append(f"  {leg.id}  {opt.flight}  "
                             f"{both(opt.depart, leg.origin, leg.destination)} -> "
                             f"{both(opt.arrive, leg.destination, leg.origin)}"
                             f"   +${opt.cost_delta_usd:,.0f}")
            lines.append(f"  total extra spend: +${delta:,.0f}")
            summary = "\n".join(lines)

            if approve is None:
                return ("REFUSED. Nothing changed: no approval handler is wired, so this tool "
                        "will not mutate the trip unattended.")
            if not approve(summary, rationale or "(no rationale given)"):
                return "DECLINED by the traveller. Nothing changed."

            for row in rows:
                world.apply_replan(row["leg_id"], world.option(row["leg_id"], row["flight"]))
            return (f"COMMITTED. {len(rows)} leg(s) rebooked in the simulated world. "
                    f"Nothing was booked or paid anywhere.\n{summary}\n\nThe times above are the "
                    f"world's own. Quote them exactly if you mention them again.")
        except Exception as exc:
            return f"error: {exc}"

    return {"get_itinerary": get_itinerary, "poll_disruptions": poll_disruptions,
            "analyse_impact": analyse_impact, "find_alternatives": find_alternatives,
            "commit_replan": commit_replan}


def check_tool_sync(impls: dict) -> None:
    """Fail at startup if a declaration, TOOLS and TOOL_IMPLS have drifted apart."""
    declared = {d.name for t in TOOLS for d in t.function_declarations}
    if declared != set(impls):
        raise SystemExit(f"tool mismatch: declarations={sorted(declared)} impls={sorted(impls)}")