"""Watchdog decisions, table-driven: process / beacon / sleep / backoff / limits / desired state."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace

from lascodia_supervisor.beacons import BeaconVerdict, assess, iso_utc
from lascodia_supervisor.watchdog import Limits, Observation, WatchState, Watchdog, decide

NOW = 1_800_000_000.0
FRESH = BeaconVerdict(count=1, fresh=True, newest_tick=NOW - 10, init_failed_since_launch=False,
                      healthy_since_launch=True)
STALE = BeaconVerdict(count=1, fresh=False, newest_tick=NOW - 1200, init_failed_since_launch=False,
                      healthy_since_launch=False)
NONE = BeaconVerdict(count=0, fresh=False, newest_tick=None, init_failed_since_launch=False,
                     healthy_since_launch=False)
INIT_FAILED = BeaconVerdict(count=1, fresh=False, newest_tick=None, init_failed_since_launch=True,
                            healthy_since_launch=False, init_fail_reason="INIT_PARAMETERS_INCORRECT: InpX")
NO_SIGNALS = {"oldTick": False, "invalidBook": False, "debuggerFound": False}


def obs(**kw) -> Observation:
    base = dict(now=NOW, desired_state="Running", auto_heal=True, desired_explicit=True, process_alive=True,
                process_started_at=NOW - 3600, beacons=FRESH, woke_recently=False, journal_signals=NO_SIGNALS,
                busy=False)
    base.update(kw)
    return Observation(**base)


LIM = Limits()


class DecisionTable(unittest.TestCase):
    CASES = [
        # (name, state overrides, observation overrides, expected action, expected status)
        ("healthy terminal", {}, {}, None, "ok"),
        ("absent → start", {}, {"process_alive": False}, "start", "restarting"),
        ("absent but auto-heal off", {}, {"process_alive": False, "auto_heal": False}, None, "disabled"),
        ("absent but desired Stopped", {}, {"process_alive": False, "desired_state": "Stopped"}, None, "disabled"),
        ("unmanaged default (Stopped, not explicit)", {}, {"desired_state": "Stopped", "auto_heal": False,
                                                           "desired_explicit": False}, None, "disabled"),
        ("wedged → restart", {}, {"beacons": STALE}, "restart", "restarting"),
        ("wedged but just woke", {}, {"beacons": STALE, "woke_recently": True}, None, "ok"),
        ("wedged but just launched", {}, {"beacons": STALE, "process_started_at": NOW - 60}, None, "ok"),
        ("wedged but our launch was recent", {"last_launch_at": NOW - 100}, {"beacons": STALE}, None, "ok"),
        ("no beacons = no evidence", {}, {"beacons": NONE}, None, "ok"),
        ("journal corruption → restart", {}, {"journal_signals": {**NO_SIGNALS, "oldTick": True}}, "restart",
         "restarting"),
        ("journal corruption, restarted for it 30 min ago", {"last_journal_restart": NOW - 1800},
         {"journal_signals": {**NO_SIGNALS, "invalidBook": True}}, None, "ok"),
        ("busy (job running)", {}, {"process_alive": False, "busy": True}, None, "restarting"),
        ("start pending, process not yet up", {"pending_start": True, "last_start_attempt": NOW - 30},
         {"process_alive": False}, None, "restarting"),
        ("start just judged failed → backoff 30 s",
         {"pending_start": True, "last_start_attempt": NOW - 125, "consecutive_start_failures": 0,
          "auto_actions": [NOW - 125]},
         {"process_alive": False, "now": NOW}, None, "backoff"),
        ("second failure, 60 s into the 2 m backoff",
         {"last_start_attempt": NOW - 180, "last_failure_at": NOW - 60, "consecutive_start_failures": 2,
          "auto_actions": [NOW - 400, NOW - 180]},
         {"process_alive": False}, None, "backoff"),
        ("second failure, 2 m backoff elapsed",
         {"last_start_attempt": NOW - 300, "last_failure_at": NOW - 125, "consecutive_start_failures": 2,
          "auto_actions": [NOW - 500, NOW - 300]},
         {"process_alive": False}, "start", "restarting"),
        ("3 restarts in the last hour → gave_up", {"auto_actions": [NOW - 3000, NOW - 2000, NOW - 1000]},
         {"process_alive": False}, None, "gave_up"),
        ("8 today, last 2 h ago → hourly retry allowed",
         {"auto_actions": [NOW - 80000 + i * 1000 for i in range(7)] + [NOW - 7200]},
         {"process_alive": False}, "start", "restarting"),
        ("8 today, last 30 min ago → gave_up",
         {"auto_actions": [NOW - 80000 + i * 1000 for i in range(7)] + [NOW - 1800]},
         {"process_alive": False}, None, "gave_up"),
        ("watchdog disabled in yaml", {}, {"process_alive": False}, None, "disabled"),
    ]

    def test_table(self):
        for name, st_kw, obs_kw, action, status in self.CASES:
            with self.subTest(name):
                st = WatchState(**st_kw)
                lim = LIM if name != "watchdog disabled in yaml" else replace(LIM, enabled=False)
                d = decide(st, obs(**obs_kw), lim)
                self.assertEqual(d.action, action, f"{name}: {d.reason}")
                self.assertEqual(st.status, status, f"{name}: {d.reason}")


class Sequences(unittest.TestCase):
    def test_backoff_ladder_30s_2m_5m_15m(self):
        st = WatchState()
        t = NOW
        gaps = []
        last_start = None
        lim = replace(LIM, max_per_hour=100, max_per_day=100)
        for _ in range(1000):
            d = decide(st, obs(now=t, process_alive=False), lim)
            if d.action == "start":
                if last_start is not None:
                    gaps.append(t - last_start)
                last_start = t
            t += 5
        # Each failed attempt is judged failed START_CONFIRM (120 s) after it was made, then the
        # backoff 30 s / 2 m / 5 m / 15 m runs (5 s evaluation granularity).
        self.assertGreaterEqual(len(gaps), 5)
        self.assertEqual([round(g) for g in gaps[:5]], [150, 240, 420, 1020, 1020])

    def test_successful_start_resets_failures(self):
        st = WatchState(consecutive_start_failures=2, pending_start=True, last_start_attempt=NOW - 30)
        decide(st, obs(), LIM)
        self.assertEqual((st.pending_start, st.consecutive_start_failures), (False, 0))

    def test_init_failed_three_launches_then_gave_up_until_operator(self):
        st = WatchState()
        lim = replace(LIM, max_per_hour=100, max_per_day=100)
        t = NOW
        actions = 0
        for launch in range(3):
            st.last_launch_at = t
            st.pending_start = False
            t += lim.launch_grace_sec + 1
            d = decide(st, obs(now=t, beacons=INIT_FAILED, process_started_at=t - 400), lim)
            if d.action:
                actions += 1
        self.assertEqual(st.status, "gave_up")
        self.assertEqual(st.gave_up_kind, "init_failed")
        self.assertIn("check the EA inputs", st.last_reason)
        self.assertEqual(actions, 2)  # restarted after launch 1 and 2, not after 3
        # Still gave up later, even with the terminal gone.
        self.assertIsNone(decide(st, obs(now=t + 7200, process_alive=False), lim).action)
        # An operator restart clears it.
        with tempfile.TemporaryDirectory() as d:
            wd = Watchdog(d)
            wd.states["x"] = st
            wd.note_operator_launch("x", now=t + 7300)
        self.assertEqual(st.gave_up_kind, "")
        self.assertIsNone(decide(st, obs(now=t + 7300 + 60, process_alive=False), lim).action)  # still coming up
        self.assertEqual(st.status, "restarting")

    def test_desired_stopped_edge_stops_once(self):
        st = WatchState(desired_seen="Running")
        d = decide(st, obs(desired_state="Stopped"), LIM)
        self.assertEqual(d.action, "stop")
        # The operator opens the app by hand later: not fought.
        self.assertIsNone(decide(st, obs(desired_state="Stopped"), LIM).action)

    def test_first_contact_stopped_never_kills_running_terminal(self):
        st = WatchState()  # desired_seen unknown (fresh deploy)
        self.assertIsNone(decide(st, obs(desired_state="Stopped"), LIM).action)

    def test_stopped_with_autoheal_off_never_acts(self):
        st = WatchState(desired_seen="Running")
        self.assertIsNone(decide(st, obs(desired_state="Stopped", auto_heal=False), LIM).action)

    def test_operator_launches_do_not_count_against_limits(self):
        with tempfile.TemporaryDirectory() as d:
            wd = Watchdog(d)
            for _ in range(5):
                wd.note_operator_launch("x")
            self.assertEqual(wd.state("x").auto_actions, [])

    def test_state_persists(self):
        with tempfile.TemporaryDirectory() as d:
            wd = Watchdog(d)
            wd.evaluate("x", obs(process_alive=False), LIM)
            wd.save()
            again = Watchdog(d).state("x")
            self.assertEqual(len(again.auto_actions), 1)
            self.assertTrue(again.pending_start)

    def test_payload_shape(self):
        st = WatchState(auto_actions=[NOW], status="backoff", next_attempt_at=NOW + 30)
        p = st.to_payload()
        self.assertEqual(set(p), {"state", "restartsLastHour", "lastActionUtc", "lastReason", "nextAttemptUtc"})


class BeaconAssessment(unittest.TestCase):
    def b(self, phase, tick_ago, written_ago, reason=""):
        return {"schema": 1, "instanceId": "LASC-1", "phase": phase, "initFailReason": reason,
                "lastTimerUtc": iso_utc(NOW - tick_ago) if tick_ago is not None else "",
                "writtenAtUtc": iso_utc(NOW - written_ago)}

    def test_fresh_running(self):
        v = assess([self.b("running", 10, 10)], NOW, 300, NOW - 3600)
        self.assertTrue(v.fresh and v.healthy_since_launch)

    def test_stale_running(self):
        v = assess([self.b("running", 900, 900)], NOW, 300, NOW - 3600)
        self.assertFalse(v.fresh)
        self.assertEqual(v.count, 1)

    def test_one_fresh_of_several_is_enough(self):
        v = assess([self.b("running", 900, 900), self.b("running", 5, 5)], NOW, 300, None)
        self.assertTrue(v.fresh)

    def test_deinit_beacons_ignored(self):
        v = assess([self.b("running", 7 * 3600, 7 * 3600)], NOW, 300, None)
        self.assertEqual(v.count, 0)  # abandoned beacon (chart gone) is not evidence of a wedge
        v = assess([self.b("deinit", 9000, 9000)], NOW, 300, None)
        self.assertEqual(v.count, 0)

    def test_init_failed_after_launch(self):
        v = assess([self.b("init_failed", None, 60, "bad input")], NOW, 300, NOW - 120)
        self.assertTrue(v.init_failed_since_launch)
        self.assertEqual(v.init_fail_reason, "bad input")
        # written before this launch: does not count
        v = assess([self.b("init_failed", None, 600, "bad input")], NOW, 300, NOW - 120)
        self.assertFalse(v.init_failed_since_launch)

    def test_degraded_init_phase_counts_as_alive_by_written_at(self):
        v = assess([self.b("init", None, 20)], NOW, 300, NOW - 120)
        self.assertTrue(v.fresh)


if __name__ == "__main__":
    unittest.main()
