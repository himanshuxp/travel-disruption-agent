"""Offline consistency checks for the simulated world. No API key, no network, stdlib only.

    python -m unittest discover -s tests -t .

The point of these is that every time in the demo is derived, never typed twice. If the
delay were applied to only one endpoint, or an alternative slipped in already departed,
the arithmetic here would catch it before a jury ever saw the output.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from world import ALTERNATIVES, AMS, BOS, DELAY_MINUTES, START_TIME, World, fmt  # noqa: E402

L1, L2 = "BOS-AMS-1", "AMS-BCN-1"


def at_event() -> World:
    """A world ticked forward to the moment the disruption is announced."""
    w = World(seed=7)
    w.tick(0)
    while not w.peek():
        w.tick()
    return w


class TestTimezones(unittest.TestCase):
    def test_ams_is_ahead_of_boston(self):
        self.assertGreater(fmt(START_TIME, AMS), fmt(START_TIME, BOS))

    def test_ams_and_bcn_share_an_offset(self):
        # AMS-BCN crosses no timezone, which is why that leg's arithmetic is simple.
        self.assertEqual(fmt(START_TIME, AMS)[-4:], fmt(START_TIME, "BCN")[-4:])

    def test_flights_land_at_the_designed_local_times(self):
        w = World()
        self.assertEqual(fmt(w.leg(L1).start, BOS), "Oct 01 21:05 EDT")
        self.assertEqual(fmt(w.leg(L1).end, AMS), "Oct 02 10:05 CEST")
        self.assertEqual(fmt(w.leg(L2).start, AMS), "Oct 02 10:45 CEST")
        self.assertEqual(fmt(w.leg(L2).end, "BCN"), "Oct 02 12:50 CEST")


class TestDelay(unittest.TestCase):
    def test_both_endpoints_move_and_duration_holds(self):
        before = World().leg(L1)
        start, end = before.start, before.end
        duration = end - start
        after = at_event().leg(L1)
        self.assertEqual(after.start - start, after.end - end)
        self.assertEqual((after.start - start).total_seconds() / 60, DELAY_MINUTES)
        self.assertEqual(after.end - after.start, duration)

    def test_status_flips_to_delayed(self):
        self.assertEqual(at_event().leg(L1).status, "delayed")

    def test_original_connection_is_forty_minutes(self):
        w = World()
        gap = (w.leg(L2).start - w.leg(L1).end).total_seconds() / 60
        self.assertEqual(gap, 40)

    def test_connection_becomes_negative_after_the_delay(self):
        w = at_event()
        gap = (w.leg(L2).start - w.leg(L1).end).total_seconds() / 60
        self.assertEqual(gap, -55)  # 40 - 95
        self.assertLess(gap, 0)


class TestAlternatives(unittest.TestCase):
    def test_nothing_returned_has_already_departed(self):
        for world in (World(), at_event()):
            for leg_id in ALTERNATIVES:
                for o in world.alternatives(leg_id):
                    self.assertGreater(o.depart, world.now,
                                       f"{o.flight} departs before {world.now}")

    def test_a_departed_flight_is_actually_filtered(self):
        w = World()
        self.assertEqual(w.excluded_count(L1), 1)  # DL 0117 left at 09:15 EDT
        self.assertNotIn("DL 0117", [o.flight for o in w.alternatives(L1)])

    def test_early_option_is_still_catchable_at_the_event(self):
        """The gap is derived from world.now, never hardcoded.

        AF 0089 leaves 105 min after the clock starts but only ~75 min after the
        disruption is announced, so the agent has to recompute it at the moment it acts.
        """
        w = World()
        w.tick(0)
        af = next(o for o in ALTERNATIVES[L1] if o.flight == "AF 0089")
        gap_at_start = (af.depart - w.now).total_seconds() / 60

        w2 = at_event()
        gap_at_event = (af.depart - w2.now).total_seconds() / 60

        self.assertGreater(gap_at_event, 0, "AF 0089 must still be catchable")
        self.assertLess(gap_at_event, 120, "catchable, but not comfortably")
        # The gap shrinks by exactly the simulated time elapsed between the two readings.
        elapsed = (w2.now - w.now).total_seconds() / 60
        self.assertEqual(gap_at_start - gap_at_event, elapsed)
        self.assertTrue(gap_at_event.is_integer() and gap_at_start.is_integer())

    def test_preserving_option_makes_the_onward_connection(self):
        w = at_event()
        af = next(o for o in w.alternatives(L1) if o.flight == "AF 0089")
        self.assertLessEqual(af.arrive, w.leg(L2).start)
        self.assertGreater(af.depart, w.now)


class TestFeed(unittest.TestCase):
    def test_nothing_before_the_event_then_exactly_one(self):
        w = World()
        w.tick(0)
        self.assertEqual(w.peek(), [])
        for _ in range(3):
            w.tick()
            if w.peek():
                break
        self.assertEqual(len(w.peek()), 1)

    def test_poll_does_not_repeat(self):
        w = at_event()
        self.assertEqual(len(w.poll()), 1)
        self.assertEqual(w.poll(), [])
        self.assertEqual(w.peek(), [])

    def test_event_is_applied_only_once(self):
        w = at_event()
        first = w.leg(L1).start
        for _ in range(5):
            w.tick()
        self.assertEqual(w.leg(L1).start, first)

    def test_same_seed_gives_the_same_world(self):
        a, b = at_event(), at_event()
        self.assertEqual(a._event.summary, b._event.summary)
        self.assertEqual([(l.id, l.start) for l in a.trip.legs],
                         [(l.id, l.start) for l in b.trip.legs])


if __name__ == "__main__":
    unittest.main()