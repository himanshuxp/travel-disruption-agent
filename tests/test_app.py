"""Offline tests for app.py, driven through Streamlit's own AppTest harness.

    python -m unittest discover -s tests -t .

No model calls, no server: AppTest runs the page's script in-process, so every button below
is a real click against the real page. Streamlit is a UI-only dependency, so the whole
module skips if it is not installed -- `requirements.txt` stays the core list.

What is worth testing here is the state machine, not the layout: the tick loop that must not
spin, the hook that must never return a bool, and the approve replay that must go through
the commit gates rather than mutating the world directly.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    from google.genai import types
    from streamlit.testing.v1 import AppTest
    HAVE_STREAMLIT = True
except ImportError:                                      # streamlit is optional
    HAVE_STREAMLIT = False

from tools import AwaitingApproval  # noqa: E402
from uicore import deciding_commit, deferring_approve, recording_commit  # noqa: E402
from world import fmt  # noqa: E402

APP = str(pathlib.Path(__file__).resolve().parent.parent / "app.py")


def page() -> "AppTest":
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    return at


def button(at, label):
    for b in at.button:
        if b.label == label:
            return b
    raise AssertionError(f"no button labelled {label!r}; have {[b.label for b in at.button]}")


def _fn(name, call_id="c1", args=None):
    return types.FunctionCall(id=call_id, name=name, args=args or {})


class _ScriptedClient:
    """Replays canned turns in place of the real client. No network, no quota."""

    def __init__(self, turns):
        self.turns = list(turns)

    class _Models:
        def __init__(self, outer):
            self.outer = outer

        def generate_content(self, **kwargs):
            if not self.outer.turns:
                raise AssertionError("the loop asked for more turns than were scripted")
            return self.outer.turns.pop(0)

    @property
    def models(self):
        return self._Models(self)


def text(at) -> str:
    """Every piece of rendered text, so an assertion can find a string wherever it landed."""
    parts = [t.value for t in at.title] + [t.value for t in at.caption]
    parts += [m.value for m in at.markdown] + [w.value for w in at.warning]
    parts += [e.value for e in at.error] + [i.value for i in at.info]
    for code in at.code:
        parts.append(code.value)
    for exp in at.expander:
        parts.append(exp.label)
    return "\n".join(str(p) for p in parts)


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed (see requirements-ui.txt)")
class TestPageLoads(unittest.TestCase):
    """The page must render with Live mode off and spend nothing doing it."""

    def test_it_renders_without_a_model(self):
        import agent
        before = agent.REQUESTS_USED
        at = page()
        self.assertEqual([t.value for t in at.title], ["Trip TRIP-8841"])
        self.assertEqual(len(at.dataframe), 1)
        self.assertEqual(agent.REQUESTS_USED, before, "rendering spent a request")

    def test_live_mode_is_off_by_default(self):
        self.assertFalse([t for t in page().toggle if t.label == "Live model"][0].value)

    def test_the_budget_is_shown(self):
        import agent
        body = text(page())
        self.assertIn(f"{agent.REQUEST_BUDGET}", body)

    def test_the_itinerary_table_has_every_leg_and_no_error(self):
        at = page()
        self.assertFalse(at.error)
        frame = at.dataframe[0].value
        self.assertEqual(len(frame), len(at.session_state.world.trip.legs))
        self.assertEqual(list(frame.columns),
                         ["leg", "kind", "flight", "depart", "arrive", "status",
                          "cost_usd", "refundable"])
        # The table's times are formatted by world.fmt off the live leg, not typed in here.
        legs = at.session_state.world.trip.legs
        self.assertEqual(list(frame["depart"]),
                         [fmt(l.start, l.origin) if l.origin else "" for l in legs])
        self.assertEqual(list(frame["arrive"]),
                         [fmt(l.end, l.destination) if l.destination else "" for l in legs])


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed (see requirements-ui.txt)")
class TestFireDisruption(unittest.TestCase):
    def test_it_shows_the_blast_radius_with_the_worlds_own_times(self):
        at = page()
        button(at, "Fire disruption").click().run()
        self.assertFalse(at.error)
        impact = at.session_state.impact
        self.assertIn("AT RISK: $280", impact)
        self.assertIn("11:40 CEST", impact)
        self.assertIn("04:40 CEST", at.session_state.impact)

    def test_firing_twice_does_not_spin(self):
        """`while not world.peek()` would loop forever once the event has been polled."""
        at = page()
        button(at, "Fire disruption").click().run()
        ticks = at.session_state.world.tick_count
        button(at, "Fire disruption").click().run()
        self.assertEqual(at.session_state.world.tick_count, ticks, "the clock advanced again")
        self.assertIn("Already fired", text(at))


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed (see requirements-ui.txt)")
class TestFindReplanWithLiveOff(unittest.TestCase):
    def test_it_shows_the_scripted_candidates_and_spends_nothing(self):
        import agent
        before = agent.REQUESTS_USED
        at = page()
        button(at, "Fire disruption").click().run()
        button(at, "Find replan").click().run()
        body = text(at)
        self.assertIn("Candidate A", body)
        self.assertIn("Candidate B", body)
        self.assertIn("nothing committed", body)
        self.assertEqual(agent.REQUESTS_USED, before, "Live mode off still called the model")

    def test_it_will_not_replan_before_the_disruption_fires(self):
        at = page()
        button(at, "Find replan").click().run()
        self.assertIn("Fire the disruption first", text(at))


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed (see requirements-ui.txt)")
class TestApprovalIsNeverUnattended(unittest.TestCase):
    """The safety property: the page cannot commit without a human clicking Approve.

    Driven through app.py's own hook and replay, which is what the buttons call. No model
    is involved, so this costs nothing.
    """

    def setUp(self):
        self.at = page()
        button(self.at, "Fire disruption").click().run()

    def _propose(self, plan="BOS-AMS-1 = AF 0089", rationale="lands in time"):
        state, world = self.at.session_state, self.at.session_state.world
        return recording_commit(state, self.at.session_state.impls), world

    def test_the_hook_raises_rather_than_answering(self):
        state = {}
        with self.assertRaises(AwaitingApproval):
            deferring_approve(state, "summary", "rationale")
        self.assertEqual(state["pending"]["summary"], "summary")

    def test_a_pending_proposal_leaves_the_trip_untouched(self):
        from tests.test_tools import snapshot
        impls, world = self._propose()
        before = snapshot(world)
        out = impls["commit_replan"]("BOS-AMS-1 = AF 0089", "lands in time")
        self.assertTrue(out.startswith("AWAITING_APPROVAL"))
        self.assertNotIn("COMMITTED", out)
        self.assertEqual(snapshot(world), before)

    def test_approving_commits_and_spends_no_request(self):
        import agent
        impls, world = self._propose()
        impls["commit_replan"]("BOS-AMS-1 = AF 0089", "lands in time")  # the model proposes
        before_requests = agent.REQUESTS_USED
        state = self.at.session_state
        result = deciding_commit(state, world, True)                  # the traveller approves
        self.assertTrue(result.startswith("COMMITTED"))
        self.assertEqual(agent.REQUESTS_USED, before_requests, "the replay called the model")
        self.assertEqual(world.leg("BOS-AMS-1").flight, "AF 0089")

    def test_declining_changes_nothing(self):
        from tests.test_tools import snapshot
        impls, world = self._propose()
        impls["commit_replan"]("BOS-AMS-1 = AF 0089", "lands in time")
        before = snapshot(world)
        self.assertIn("DECLINED", deciding_commit(self.at.session_state, world, False))
        self.assertEqual(snapshot(world), before)

    def test_the_replay_still_runs_the_gates(self):
        """Approving cannot smuggle in a plan the gates would have refused."""
        impls, world = self._propose()
        impls["commit_replan"]("BOS-AMS-1 = KL 0603", "made up")   # invented flight
        self.assertIn("REFUSED", deciding_commit(self.at.session_state, world, True))

    def test_no_proposal_means_nothing_to_apply(self):
        world = self.at.session_state.world
        self.assertIn("nothing to apply", deciding_commit({}, world, True))


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed (see requirements-ui.txt)")
class TestReset(unittest.TestCase):
    def test_it_rebuilds_the_world_but_not_the_quota(self):
        import agent
        at = page()
        button(at, "Fire disruption").click().run()
        self.assertTrue(at.session_state.event)
        used = agent.REQUESTS_USED
        button(at, "Reset").click().run()
        self.assertIsNone(at.session_state.event)
        self.assertFalse(at.session_state.impact)
        self.assertEqual(at.session_state.world.tick_count, 0)
        self.assertEqual(agent.REQUESTS_USED, used, "Reset laundered the request counter")
        self.assertIn("TRIP-8841", text(at))


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed (see requirements-ui.txt)")
class TestLiveRunRendersEveryToolCall(unittest.TestCase):
    """The whole trace must show, including the commit call the traveller has to judge.

    Stubbed client: the point is the page's rendering of the loop's events, and a real call
    would spend quota to learn nothing more. Note the commit is the LAST event, with no
    request counter after it -- an earlier version of the page dropped exactly that one.
    """

    def setUp(self):
        import agent
        # REQUESTS_USED and BUDGET_SPENT are process globals, shared by every page in this
        # process. Each test is a fresh page, so each test gets a fresh budget -- otherwise
        # the third run here would be refused by the real quota guard.
        self.saved = (agent.REQUESTS_USED, agent.BUDGET_SPENT, agent.make_client_and_config)
        agent.REQUESTS_USED, agent.BUDGET_SPENT = 0, False
        self.addCleanup(setattr, agent, "REQUESTS_USED", self.saved[0])
        self.addCleanup(setattr, agent, "BUDGET_SPENT", self.saved[1])

    def _run(self, turns):
        import agent
        client = _ScriptedClient(turns)
        agent.make_client_and_config = lambda tools: (client, None)
        self.addCleanup(setattr, agent, "make_client_and_config", self.saved[2])
        at = page()
        at.toggle[0].set_value(True).run()
        button(at, "Fire disruption").click().run()
        button(at, "Find replan").click().run()
        return at, agent.REQUESTS_USED

    def _turns(self, *calls):
        return [types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            role="model", parts=[types.Part(function_call=c) for c in calls]))]) for c in calls]

    def test_one_expander_per_call_including_the_last(self):
        at, _ = self._run(self._turns(
            _fn("get_itinerary", "c1"),
            _fn("analyse_impact", "c2", {"leg_id": "BOS-AMS-1"}),
            _fn("commit_replan", "c3", {"plan": "BOS-AMS-1 = AF 0089",
                                              "rationale": "lands in time"})))
        labels = [e.label for e in at.expander]
        self.assertEqual(len(labels), 3, f"an expander went missing: {labels}")
        self.assertIn("-> commit_replan", labels[2])
        self.assertTrue(any("AWAITING_APPROVAL" in e.value
                            for e in at.code if e.value), "the pending result is not shown")

    def test_it_waits_for_a_human_and_leaves_the_trip_alone(self):
        from tests.test_tools import snapshot
        at, _ = self._run(self._turns(
            _fn("get_itinerary", "c1"),
            _fn("commit_replan", "c2", {"plan": "BOS-AMS-1 = AF 0089", "rationale": "ok"})))
        before = snapshot(at.session_state.world)
        self.assertIsNotNone(at.session_state.pending)
        self.assertEqual(snapshot(at.session_state.world), before)
        self.assertEqual(at.session_state.world.leg("BOS-AMS-1").status, "delayed")
        self.assertIn("Approve", [b.label for b in at.button])

    def test_a_refused_plan_offers_nothing_to_approve(self):
        """A gate refusal must not leave a phantom approval request behind."""
        at, _ = self._run(self._turns(
            _fn("get_itinerary", "c1"),
            _fn("commit_replan", "c2", {"plan": "BOS-AMS-1 = KL 0603", "rationale": "made up"})))
        self.assertIsNone(at.session_state.pending)
        self.assertNotIn("Approve", [b.label for b in at.button])
        self.assertTrue(any("REFUSED" in c.value for c in at.code))

    def test_the_approve_button_commits_the_stored_plan(self):
        at, before_requests = self._run(self._turns(
            _fn("get_itinerary", "c1"),
            _fn("commit_replan", "c2", {"plan": "BOS-AMS-1 = AF 0089", "rationale": "ok"})))
        button(at, "Approve").click().run()
        self.assertTrue(at.session_state.result.startswith("COMMITTED"))
        self.assertEqual(at.session_state.world.leg("BOS-AMS-1").flight, "AF 0089")
        self.assertIsNone(at.session_state.pending)

    def test_a_spent_budget_blocks_live_mode_rather_than_spending_more(self):
        """The quota guard is shared with the CLI, and it has to hold in the page too."""
        import agent
        at, _ = self._run(self._turns(_fn("get_itinerary", "c1")))
        agent.REQUESTS_USED = agent.REQUEST_BUDGET      # simulate a spent budget
        agent.BUDGET_SPENT = True
        button(at, "Find replan").click().run()
        self.assertTrue(any("budget" in e.value.lower() for e in at.error), at.error)
        self.assertEqual(agent.REQUESTS_USED, agent.REQUEST_BUDGET, "it spent another request")

    def test_the_decline_button_changes_nothing(self):
        from tests.test_tools import snapshot
        at, _ = self._run(self._turns(
            _fn("get_itinerary", "c1"),
            _fn("commit_replan", "c2", {"plan": "BOS-AMS-1 = AF 0089", "rationale": "ok"})))
        before = snapshot(at.session_state.world)
        button(at, "Decline").click().run()
        self.assertIn("DECLINED", at.session_state.result)
        self.assertEqual(snapshot(at.session_state.world), before)


if __name__ == "__main__":
    unittest.main()