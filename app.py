"""A Streamlit page over the same code `main.py` runs. Nothing here is reimplemented.

    streamlit run app.py

Simulated world only; nothing is ever booked or paid. Streamlit reruns this whole file on
every click, so two rules are load-bearing:

  * `World`, the tool dict and the run state live in `st.session_state`. A world built in
    module scope would be rebuilt on every click, so the clock would never advance.
  * The model is reachable from exactly one place: inside the "Find replan" button when
    Live mode is on. Importing `agent` is inert (it reads .env, defines functions);
    `make_client_and_config` is called lazily, never at import and never on a bare rerun.
  * The tool trace renders once the run returns. Streamlit draws nothing while the script is
    blocked on `generate_content`, so "as it happens" is not available; order is preserved
    and the commit call is expanded, since that is the one a human has to read.

Approval cannot block. `commit_replan` calls its `approve` hook synchronously, and a page
cannot wait for a button click mid-script. So the hook records the proposal and raises
`AwaitingApproval`; the tool returns a pending result, the dispatch loop stops, and the
Approve/Decline buttons appear on the next render. Approving replays the model's own plan
text through the same tool, so the commit gates run again and nothing bypasses them. That
replay spends no request, because the model is not asked a second time. The three functions
that implement it live in uicore.py, which has no streamlit import, so tests can drive them
without running the page.
"""
import contextlib
import io

import streamlit as st

import agent
from main import scripted_replan
from tools import TOOLS, World, build_tools, check_tool_sync
from uicore import deciding_commit, deferring_approve, recording_commit, tool_calls
from world import fmt

st.set_page_config(page_title="Travel disruption agent", layout="wide")

MAX_TICKS_TO_EVENT = 200   # the event is minutes away; this only exists to bound a spin


# --- session state ---------------------------------------------------------------
def reset_world() -> None:
    """Rebuild the world and the tools bound to it.

    Deliberately does not touch the request counter: a spent LLM_REQUEST_BUDGET is a real
    limit and Reset is not a way around it.
    """
    world = World(seed=7)
    st.session_state.world = world
    # The hook's signature is commit_replan's contract, so the state is bound by a closure.
    st.session_state.impls = build_tools(
        world, approve=lambda s, r: deferring_approve(st.session_state, s, r))
    st.session_state.event = None
    st.session_state.impact = None
    st.session_state.trace = []
    st.session_state.proposal = None
    st.session_state.pending = None
    st.session_state.result = None


if "world" not in st.session_state:
    reset_world()

world: World = st.session_state.world
impls: dict = st.session_state.impls

# --- sidebar ---------------------------------------------------------------------
with st.sidebar:
    live = st.toggle("Live model", value=False, help="Live mode spends API requests")
    st.caption("Live mode spends API requests")   # st.toggle has no `caption` kwarg
    st.divider()
    st.markdown("**Request budget**")
    st.progress(min(agent.REQUESTS_USED / max(agent.REQUEST_BUDGET, 1), 1.0))
    st.caption(f"{agent.REQUESTS_USED} of {agent.REQUEST_BUDGET} used")
    st.caption("`LLM_REQUEST_BUDGET`, read once at import.")
    if agent.BUDGET_SPENT:
        st.warning("Budget spent. Restart Streamlit to clear it -- Reset only rebuilds the "
                   "world, so the counter cannot be laundered.")
    if st.button("Reset", use_container_width=True):
        reset_world()
        st.rerun()

check_tool_sync(impls)

# --- the itinerary, read straight off the live world ------------------------------
l1, l2 = world.trip.legs[0], world.trip.legs[1]
st.title(f"Trip {world.trip.id}")
st.caption(f"Connection at {l2.origin}: "
           f"{(l2.start - l1.end).total_seconds() / 60:.0f} min. Simulated world: nothing is "
           f"ever booked or paid.")

st.dataframe(
    [{"leg": l.id, "kind": l.kind,
      "flight": (l.flight or world.flight_no.get(l.id, "")) if l.kind == "flight" else "",
      "depart": fmt(l.start, l.origin) if l.origin else "",
      "arrive": fmt(l.end, l.destination) if l.destination else "",
      "status": l.status, "cost_usd": l.cost_usd, "refundable": l.refundable}
     for l in world.trip.legs],
    use_container_width=True, hide_index=True)

# --- buttons ---------------------------------------------------------------------
fire_col, replan_col = st.columns(2)
fire = fire_col.button("Fire disruption", use_container_width=True)
replan = replan_col.button("Find replan", use_container_width=True)

if fire:
    if st.session_state.event:
        st.info("Already fired -- Reset to run the disruption again.")
    else:
        # Bounded on purpose. `while not world.peek()` alone would spin forever once the
        # event has been polled, because a consumed event never comes back.
        for _ in range(MAX_TICKS_TO_EVENT):
            if world.peek():
                break
            world.tick()
        fresh = world.peek()
        if not fresh:
            st.warning("The event did not fire within the tick bound.")
        else:
            st.session_state.event = fresh[0]
            st.session_state.impact = "\n\n".join([
                impls["poll_disruptions"](),
                impls["analyse_impact"](fresh[0].leg_id)])

if st.session_state.impact:
    st.subheader("Blast radius")
    st.caption("What the delay breaks downstream, and how much non-refundable money it puts "
               "at risk. The model's first tool call is this one.")
    st.code(st.session_state.impact, language="text")

if replan:
    if not st.session_state.event:
        st.warning("Fire the disruption first -- there is nothing to replan yet.")
    elif not live:
        # M0's deterministic candidates, captured rather than reimplemented, so the page
        # and the CLI cannot drift apart.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scripted_replan(world, st.session_state.event)
        st.subheader("Scripted candidates")
        st.caption("M0: no model call, no quota. It ranks by rule, not judgement.")
        st.code(buf.getvalue(), language="text")
    elif agent.BUDGET_SPENT:
        st.error(agent.budget_message("Live mode is off until the budget is raised."))
    else:
        st.warning(f"A live run spends requests: {agent.budget_left()} of "
                   f"{agent.REQUEST_BUDGET} left. Nothing was called yet.")
        client, config = agent.make_client_and_config(TOOLS)
        trace: list[tuple[str, str]] = []
        agent.handle_disruption(
            client, config, recording_commit(st.session_state, impls), world,
            [st.session_state.event],
            emit=lambda kind, text: trace.append((kind, text)))
        st.session_state.trace = trace

if st.session_state.trace:
    st.subheader("Agent run")
    st.caption("Rendered in order once the run returns -- Streamlit draws nothing while the "
               "script is still blocked on the model.")
    for label, lines in tool_calls(st.session_state.trace):
        with st.expander(label, expanded=label.startswith("-> commit_replan")):
            st.code("\n".join(lines), language="text")
    for kind, text in st.session_state.trace:
        if kind == "agent":
            st.markdown(text.strip())

# --- approval --------------------------------------------------------------------
# Only ever set by the hook, so a plan the gates refused leaves nothing to approve.
if st.session_state.pending:
    pending = st.session_state.pending
    st.subheader("Approve the replan?")
    st.warning("Nothing is booked or paid. Approving only moves the simulated world.")
    st.code(pending["summary"], language="text")
    st.caption(f"rationale: {pending['rationale']}")
    yes_col, no_col = st.columns(2)
    approve_now = yes_col.button("Approve", type="primary", use_container_width=True)
    decline_now = no_col.button("Decline", use_container_width=True)
    if approve_now or decline_now:
        st.session_state.result = deciding_commit(st.session_state, world, approve_now)
        st.session_state.pending = None
        st.rerun()

if st.session_state.result:
    st.subheader("Result")
    if st.session_state.result.startswith("COMMITTED"):
        st.success("Committed to the simulated world. The itinerary above is re-read from it, "
                   "and the times in that text are the world's own.")
    else:
        st.warning("Nothing was changed.")
    st.code(st.session_state.result, language="text")