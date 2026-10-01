"""The page's approval state machine, with no streamlit import.

app.py is a Streamlit script: importing it runs the whole page, which is why the logic that
matters cannot be tested from a test file. These three functions hold it instead, and they
take the state as an argument rather than reaching for `st.session_state`, so the tests
drive them with a plain dict.

Nothing here decides anything on its own. `deferring_approve` never returns -- a page cannot
block for a button click, so the only honest answer a hook can give mid-run is to unwind and
ask later. `deciding_commit` is what happens on the next render, and it goes back through
`commit_replan` rather than touching the world, so the gates run again on the replay.
"""
from tools import AwaitingApproval, build_tools


def deferring_approve(state, summary: str, rationale: str) -> bool:
    """An approval hook for a UI. It always raises, and that is the safety property.

    Returning False would make `commit_replan` report DECLINED and change nothing; returning
    True would commit with nobody's consent. Neither is reachable, because this function has
    no return path at all.
    """
    state["pending"] = {"summary": summary, "rationale": rationale}
    raise AwaitingApproval()


def recording_commit(state, impls: dict) -> dict:
    """Wrap `commit_replan` so the model's own plan text is kept.

    The hook only ever sees the rendered summary, never the plan. Approving later replays
    that exact text, which is what keeps the commit gates in play on the second pass.
    """
    real = impls["commit_replan"]

    def commit_replan(plan="", rationale=""):
        state["proposal"] = {"text": plan, "rationale": rationale}
        return real(plan, rationale)

    return dict(impls, commit_replan=commit_replan)


def tool_calls(trace: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
    """Group a flat (kind, text) trace into one entry per tool call: label, result lines.

    The `note` events -- the request counter, retries, the budget -- sit between calls and are
    dropped here; they are about the run, not about a tool result. A trailing call with no
    note after it still gets its own entry, which is the case that matters most: the
    commit_replan the traveller is being asked to approve.
    """
    groups: list[tuple[str, list[str]]] = []
    label: str | None = None
    for kind, text in trace:
        if kind == "call":
            label, lines = text.strip(), []
            groups.append((label, lines))     # appended now, filled below: list is mutable
        elif kind == "result" and groups:
            groups[-1][1].append(text)
    return groups


def deciding_commit(state, world, decision: bool) -> str:
    """Run the stored plan through the gates with a real human answer.

    The tools are rebuilt with a hook that returns the decision instead of raising, so this
    is the ordinary commit path: same gates, same checks, nothing short-circuited. Rebuilding
    per call also means a stale hook cannot survive a decision.
    """
    proposal = state.get("proposal")
    if not proposal:
        return "error: nothing was proposed, so there is nothing to apply."
    decided = build_tools(world, approve=lambda summary, rationale: decision)
    return decided["commit_replan"](proposal["text"], proposal["rationale"])