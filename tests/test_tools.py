"""Offline tests for the M2/M4 tools: the blast radius and the commit gate.

    python -m unittest discover -s tests -t .

No API key, no network, stdlib only. `build_tools` takes an `approve` callback, so the
whole commit path -- including the human gate -- runs here without a terminal and without
spending a request. The refusal tests are the important ones: the point of commit_replan
is that a wrong plan changes nothing, so each one asserts the trip is byte-identical
afterwards.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
from tools import build_tools, check_tool_sync  # noqa: E402
from world import ALTERNATIVES, World, fmt  # noqa: E402

L1, L2 = "BOS-AMS-1", "AMS-BCN-1"
HOTEL, ACT = "BCN-HOTEL-1", "BCN-ACT-1"


def at_event() -> World:
    """A world ticked forward to the moment the delay is announced and applied."""
    w = World(seed=7)
    w.tick(0)
    while not w.peek():
        w.tick()
    return w


def snapshot(world: World) -> list[tuple]:
    return [(l.id, l.start, l.end, l.status, l.flight, l.cost_usd, l.booking_ref)
            for l in world.trip.legs]


class ToolHarness(unittest.TestCase):
    def setUp(self):
        self.world = at_event()
        self.asked = []
        self.impls = build_tools(self.world, approve=self._approve)
        check_tool_sync(self.impls)

    def _approve(self, summary, rationale):
        self.asked.append((summary, rationale))
        return True

    def commit(self, plan, rationale="because"):
        return self.impls["commit_replan"](plan, rationale)


class TestToolSurface(ToolHarness):
    def test_all_five_tools_are_registered(self):
        self.assertEqual(set(self.impls), {"get_itinerary", "poll_disruptions",
                                            "analyse_impact", "find_alternatives",
                                            "commit_replan"})

    def test_no_tool_raises_on_nonsense(self):
        """The rule is that a tool returns text.

        Junk in its own arguments comes back as a readable string, because the model has to
        be able to read the failure and try again. Wrong *keyword names* are a different
        thing -- the SDK validates args against the schema -- so each tool is called with
        arguments it actually declares.
        """
        junk = {
            "get_itinerary": [{}],
            "poll_disruptions": [{}],
            "analyse_impact": [{"leg_id": "NOPE-1"}, {"leg_id": ""}, {}],
            "find_alternatives": [{"leg_id": "NOPE-1"}, {"leg_id": ""}, {},
                                  {"leg_id": L1, "avoid": "ZZ 9999"}],
            "commit_replan": [{"plan": "total +$180, see above"}, {"plan": ""}, {}],
        }
        self.assertEqual(set(junk), set(self.impls))
        for tool, arglist in junk.items():
            for kwargs in arglist:
                out = self.impls[tool](**kwargs)
                self.assertIsInstance(out, str)
                self.assertTrue(out.strip(), f"{tool}{kwargs} returned nothing")
        self.assertTrue(self.commit("total +$180, see above").startswith("REFUSED"))
        self.assertTrue(self.commit("").startswith("REFUSED"))

    def test_commit_needs_a_human(self):
        """With no approval handler wired, the tool must refuse rather than mutate."""
        w = at_event()
        impls = build_tools(w)
        check_tool_sync(impls)
        before = snapshot(w)
        out = impls["commit_replan"](f"{L1} = AF 0089", "no handler here")
        self.assertIn("REFUSED", out)
        self.assertEqual(snapshot(w), before)

    def test_declining_changes_nothing(self):
        w = at_event()
        impls = build_tools(w, approve=lambda s, r: False)
        before = snapshot(w)
        out = impls["commit_replan"](f"{L1} = AF 0089", "declined")
        self.assertIn("DECLINED", out)
        self.assertEqual(snapshot(w), before)


class TestConstraintStringsDidNotDrift(unittest.TestCase):
    """The numbers the model reads come from the constants the code checks."""

    def test_hard_constraints_read_exactly_as_before(self):
        names = [c.name for c in World(seed=7).trip.constraints if c.kind == "hard"]
        self.assertEqual(names, [
            "connection at AMS: 40 min minimum",
            "hotel check-in from 15:00 CEST",
            "Sagrada Familia entry 17:00 CEST, non-refundable ticket",
        ])


class TestAnalyseImpact(ToolHarness):
    """(a) the blast radius of the 95-minute delay."""

    def setUp(self):
        super().setUp()
        self.out = self.impls["analyse_impact"](L1)

    def test_reports_the_delay_and_both_endpoint_conversions(self):
        # The user caught this: 22:40 EDT is 04:40 CEST, not 02:40. Both readings must be
        # printed and both must be converted from UTC, never typed.
        self.assertIn("Oct 01 22:40 EDT  (Oct 02 04:40 CEST)", self.out)
        self.assertIn("Oct 02 11:40 CEST", self.out)
        self.assertIn("departs", self.out)
        self.assertIn("arrives", self.out)

    def test_names_the_unmakeable_connection_and_its_margin(self):
        self.assertIn(f"{L2}  BROKEN", self.out)
        self.assertIn("55 min", self.out)          # 10:45 departure vs 11:40 landing
        # the deadline the model compares a replacement arrival against
        self.assertIn("Oct 02 10:05 CEST", self.out)

    def test_reports_hotel_and_activity_at_risk_with_the_money(self):
        self.assertIn(f"{HOTEL}  AT RISK", self.out)
        self.assertIn(f"{ACT}  AT RISK", self.out)
        self.assertIn("$248", self.out)
        self.assertIn("$32", self.out)
        self.assertIn("$280", self.out)
        self.assertIn("at risk, not yet missed", self.out)

    def test_never_names_a_replacement_flight(self):
        """find_alternatives must stay the only source of bookable options.

        If analyse_impact leaked a flight, commit_replan's check could be satisfied by
        text from the wrong tool.
        """
        for options in ALTERNATIVES.values():
            for o in options:
                self.assertNotIn(o.flight, self.out,
                                 f"analyse_impact leaked the replacement {o.flight}")

    def test_rejects_a_non_flight_leg_without_raising(self):
        self.assertIn("error", self.impls["analyse_impact"](HOTEL))

    def test_before_the_delay_nothing_downstream_breaks(self):
        """Same code, no delay: the trip is intact, so the tool must say so."""
        out = build_tools(World(seed=7), approve=lambda s, r: True)["analyse_impact"](L1)
        self.assertNotIn("BROKEN", out)
        self.assertNotIn("AT RISK", out)


class TestCommitRefusesInventedFlights(ToolHarness):
    """(b) the safety property. The model is not the authority on what exists."""

    def test_refuses_the_flight_the_m1_run_invented(self):
        before = snapshot(self.world)
        out = self.commit(f"{L1} = BA 0431")
        self.assertIn("REFUSED", out)
        self.assertIn("does not offer that flight", out)
        self.assertEqual(snapshot(self.world), before)
        self.assertEqual(self.asked, [])          # never got as far as asking a human

    def test_refuses_invented_times_for_a_real_flight(self):
        """A real flight number does not license made-up times."""
        before = snapshot(self.world)
        out = self.commit(f"{L1} = AF 0089, Oct 01 00:15 EDT -> Oct 02 13:15 CEST")
        self.assertIn("REFUSED", out)
        self.assertIn("00:15 EDT", out)
        self.assertIn("13:15 CEST", out)
        self.assertEqual(snapshot(self.world), before)

    def test_refuses_a_flight_that_has_already_departed(self):
        before = snapshot(self.world)
        out = self.commit(f"{L1} = DL 0117")
        self.assertIn("REFUSED", out)
        self.assertEqual(snapshot(self.world), before)

    def test_tolerates_a_dropped_leading_zero(self):
        """Forgiving about shape: 'AF 89' is the same flight as 'AF 0089'."""
        out = self.commit(f"{L1} = AF 89")
        self.assertIn("COMMITTED", out)

    def test_refuses_an_unknown_leg_id(self):
        out = self.commit("AMS-BCN-9 = IB 3114")
        self.assertIn("REFUSED", out)
        self.assertIn("not a leg", out)

    def test_refuses_two_plans_at_once(self):
        out = self.commit(f"{L1} = AF 0089\n{L1} = KL 0603")
        self.assertIn("REFUSED", out)
        self.assertIn("twice", out)


class TestCommitAcceptsAF0089(ToolHarness):
    """(c) the plan that leaves the rest of the trip alone."""

    def setUp(self):
        super().setUp()
        self.out = self.commit(f"{L1} = AF 0089",
                               "leaves earlier than the delayed flight and lands in time")

    def test_commits(self):
        self.assertIn("COMMITTED", self.out)
        self.assertIn("+320", self.out.replace("+$320", "+320"))

    def test_the_trip_actually_moved(self):
        leg = self.world.leg(L1)
        self.assertEqual(leg.flight, "AF 0089")
        self.assertEqual(leg.status, "rebooked")
        self.assertEqual(fmt(leg.start, "BOS"), "Oct 01 18:30 EDT")
        self.assertEqual(fmt(leg.end, "AMS"), "Oct 02 07:30 CEST")
        self.assertEqual(leg.cost_usd, 612.0 + 320.0)
        self.assertEqual(leg.booking_ref, "REBOOK-BOS-AMS-1-AF0089")

    def test_the_onward_leg_is_untouched(self):
        """The whole point of AF 0089: nothing after it has to change."""
        l2 = self.world.leg(L2)
        self.assertEqual(l2.flight, "IB 3110")
        self.assertEqual(l2.status, "scheduled")
        self.assertEqual((l2.start - self.world.leg(L1).end).total_seconds() / 60, 195)

    def test_it_went_through_the_human(self):
        self.assertEqual(len(self.asked), 1)
        summary, rationale = self.asked[0]
        self.assertIn("AF 0089", summary)
        self.assertIn("leaves earlier", rationale)

    def test_the_committed_times_are_the_worlds_own(self):
        """The tool prints authoritative times back, so the model cannot misquote them."""
        self.assertIn("Oct 01 18:30 EDT", self.out)
        self.assertIn("Oct 02 07:30 CEST", self.out)

    def test_the_new_flight_is_no_longer_offered_for_that_leg(self):
        """flight_no is per-world; the module constant would have gone stale here."""
        self.assertEqual(self.world.flight_no[L1], "AF 0089")
        again = self.impls["find_alternatives"](L1)
        self.assertNotIn("AF 0089", again)
        self.assertIn("BA 0432", again)


class TestCommitRefusesTheCheapBadPlan(ToolHarness):
    """(d) IB 3108 is the cheapest fare in the table and the worst outcome."""

    def test_is_refused_for_burning_the_hotel_night(self):
        before = snapshot(self.world)
        out = self.commit(f"{L1} = KL 0603\n{L2} = IB 3108")
        self.assertIn("REFUSED", out)
        self.assertIn(f"{HOTEL} is burned", out)
        self.assertIn("$248", out)
        self.assertIn("2.3h of a 20h night", out)
        self.assertIn(f"{ACT} is missed entirely", out)
        self.assertIn("not a cheaper outcome", out)
        self.assertEqual(snapshot(self.world), before)
        self.assertEqual(self.asked, [])

    def test_the_cheap_same_day_fare_is_also_refused(self):
        """VY 8901 lands 19:50, after the 18:30 activity ends. Cheap, still wrong."""
        out = self.commit(f"{L2} = VY 8901")
        self.assertIn("REFUSED", out)
        self.assertIn(f"{ACT} is missed entirely", out)


class TestCommitAcceptsIB3114(ToolHarness):
    """(e) rebooking only the onward leg, leaving the delayed flight in place."""

    def setUp(self):
        super().setUp()
        self.out = self.commit(f"{L2} = IB 3114", "keeps the 95-minute delay and lands in time")

    def test_commits(self):
        self.assertIn("COMMITTED", self.out)
        self.assertIn("IB 3114", self.out)
        self.assertIn("Oct 02 15:25 CEST", self.out)

    def test_the_delayed_leg_is_left_as_it_was(self):
        l1 = self.world.leg(L1)
        self.assertEqual(l1.flight, "BA 0431")
        self.assertEqual(l1.status, "delayed")
        self.assertEqual(fmt(l1.end, "AMS"), "Oct 02 11:40 CEST")

    def test_the_connection_margin_is_the_one_that_matters(self):
        """13:20 departure against an 11:40 landing: 100 min against a 40 min minimum."""
        margin = (self.world.leg(L2).start - self.world.leg(L1).end).total_seconds() / 60
        self.assertEqual(margin, 100)
        self.assertGreaterEqual(margin, 40)

    def test_the_hotel_is_still_usable_so_it_is_not_refused(self):
        """Landing 25 min after check-in opens is a wait, not a lost night."""
        arrival = self.world.leg(L2).end
        check_in = self.world.leg(HOTEL).start
        self.assertLess(arrival, self.world.leg(HOTEL).end)
        self.assertEqual(arrival.date(), check_in.date())


class _StubClient:
    """Raises a retryable error, so the ladder runs without a network."""

    def __init__(self, code=503):
        self.code = code
        self.calls = 0

    class _Models:
        def __init__(self, outer):
            self.outer = outer

        def generate_content(self, **kwargs):
            self.outer.calls += 1
            raise agent.errors.APIError(self.outer.code, {"error": "stub"})

    @property
    def models(self):
        return self._Models(self)


class TestBudgetDefault(unittest.TestCase):
    """The default is the whole safety margin, so it is asserted on its own."""

    def test_default_is_eight(self):
        self.assertEqual(agent.REQUEST_BUDGET, 8)
        self.assertEqual(agent.budget_left(), 8)


class TestBudgetStopsTheLadder(unittest.TestCase):
    """The budget is the reason a degraded API day is survivable. No live calls."""

    def setUp(self):
        self.saved = (agent.REQUESTS_USED, agent.BUDGET_SPENT,
                      agent.REQUEST_BUDGET, agent.RETRY_DELAYS)
        agent.REQUESTS_USED, agent.BUDGET_SPENT = 0, False
        agent.REQUEST_BUDGET = 3
        agent.RETRY_DELAYS = ()          # no sleeping in tests

    def tearDown(self):
        (agent.REQUESTS_USED, agent.BUDGET_SPENT,
         agent.REQUEST_BUDGET, agent.RETRY_DELAYS) = self.saved

    def test_ladder_stops_at_the_cap(self):
        client = _StubClient()
        for _ in range(3):
            agent.generate(client, None, [], "stub-model")
        self.assertEqual(agent.REQUESTS_USED, 3)
        self.assertEqual(client.calls, 3)
        self.assertTrue(agent.BUDGET_SPENT)

    def test_no_request_is_made_once_the_budget_is_spent(self):
        client = _StubClient()
        for _ in range(6):
            agent.generate(client, None, [], "stub-model")
        self.assertEqual(client.calls, 3, "the ladder kept retrying past the cap")

    def test_a_404_is_not_retried(self):
        client = _StubClient(code=404)
        self.assertIsNone(agent.generate(client, None, [], "bad-model"))
        self.assertEqual(client.calls, 1)


if __name__ == "__main__":
    unittest.main()
