"""
Unit tests for BULKPEN's scoring layer (predict.py).

Covers the pure, model-free heuristics with fixed fixtures — no network, no
python-mlb-statsapi calls. These pin the JS->Python port so the scoring can't
silently drift (e.g. Python's banker's rounding vs JS Math.round).

Run:  python -m unittest test_predict      (or: python test_predict.py)
"""
import unittest
from datetime import date

import predict


# --- tiny stand-ins for the library's game-log split objects ----------------
class FakeStat:
    def __init__(self, ip):
        self.innings_pitched = ip


class FakeSplit:
    def __init__(self, d, ip):
        self.date = d
        self.stat = FakeStat(ip)


def season(name="X", g=0, gs=0, ip=0.0, era="0.00", empty=False):
    if empty:
        return {"name": name, "empty": True}
    return {"name": name, "g": g, "gs": gs, "ip": ip, "era": era, "empty": False}


class TestInningsAndRounding(unittest.TestCase):
    def test_ip_to_innings_thirds(self):
        self.assertAlmostEqual(predict.ip_to_innings("61.1"), 61 + 1 / 3)
        self.assertAlmostEqual(predict.ip_to_innings("0.2"), 2 / 3)
        self.assertEqual(predict.ip_to_innings("5.0"), 5)
        self.assertAlmostEqual(predict.ip_to_innings(61.2), 61 + 2 / 3)

    def test_ip_to_innings_bad_input(self):
        self.assertEqual(predict.ip_to_innings(None), 0.0)
        self.assertEqual(predict.ip_to_innings("not-a-number"), 0.0)

    def test_jround_is_half_up_not_bankers(self):
        # Python's round() would give 0, 2, 2 here; JS Math.round gives 1, 2, 3.
        self.assertEqual(predict.jround(0.5), 1)
        self.assertEqual(predict.jround(1.5), 2)
        self.assertEqual(predict.jround(2.5), 3)
        self.assertEqual(predict.jround(-0.5), 0)

    def test_clamp(self):
        self.assertEqual(predict.clamp(150), 100)
        self.assertEqual(predict.clamp(-5), 0)
        self.assertEqual(predict.clamp(42), 42)


class TestOpenerScore(unittest.TestCase):
    def test_no_data(self):
        self.assertIsNone(predict.opener_score(season(empty=True), 2026)["score"])
        self.assertIsNone(predict.opener_score(season(g=0), 2026)["score"])
        self.assertIsNone(predict.opener_score(None, 2026)["score"])

    def test_true_starter_is_traditional(self):
        # 10G/10GS/61IP -> ipApp 6.1 -> score clamps to 0, no relief/opener bonuses.
        o = predict.opener_score(season(g=10, gs=10, ip=61.0), 2026)
        self.assertEqual(o["score"], 0)
        self.assertEqual(o["label"], "Traditional start")
        self.assertEqual(o["cls"], "green")

    def test_pure_reliever_listed_to_start_is_likely_opener(self):
        # 20G/0GS/20IP -> ipApp 1 -> 100 +10 (relief share) +22 (gs==0) -> clamp 100.
        o = predict.opener_score(season(g=20, gs=0, ip=20.0), 2026)
        self.assertEqual(o["score"], 100)
        self.assertEqual(o["label"], "Likely opener")
        self.assertEqual(o["cls"], "red")

    def test_midrange_is_possible_opener(self):
        # Land the score in the 45-69 band: pick ipApp so 100-(ipApp-1)*22 ~ 55,
        # with no bonuses (gs>0, relief share <=0.5). 8G/5GS/24IP -> ipApp 3.0
        # -> 100-44 = 56; (g-gs)/g = 0.375 (no bonus); gs!=0 (no bonus).
        o = predict.opener_score(season(g=8, gs=5, ip=24.0), 2026)
        self.assertEqual(o["score"], 56)
        self.assertEqual(o["label"], "Possible opener")
        self.assertEqual(o["cls"], "gold")


class TestBulkScore(unittest.TestCase):
    def test_too_few_appearances(self):
        self.assertIsNone(predict.bulk_score(season(g=1, gs=0, ip=2.0)))

    def test_real_starter_excluded(self):
        # gs/g > 0.6 and ipApp > 4 -> not a bulk arm.
        self.assertIsNone(predict.bulk_score(season(g=10, gs=10, ip=61.0)))

    def test_empty_or_none(self):
        self.assertIsNone(predict.bulk_score(season(empty=True)))
        self.assertIsNone(predict.bulk_score(None))

    def test_swingman_scores_high(self):
        # 20G/3GS/55IP -> ipApp 2.75 -> 2.75*38 + 12 + min(55,40)*0.4 = 120.5 -> clamp 100.
        b = predict.bulk_score(season(name="Swing", g=20, gs=3, ip=55.0))
        self.assertEqual(b["base"], 100)
        self.assertTrue(b["swing"])          # gs > 0
        self.assertTrue(b["multiInning"])    # ipApp >= 1.6
        self.assertEqual(b["name"], "Swing")

    def test_low_leverage_single_inning_arm(self):
        # 30G/0GS/30IP -> ipApp 1.0 -> 1*38 + 0 + 30*0.4 = 50.
        b = predict.bulk_score(season(g=30, gs=0, ip=30.0))
        self.assertEqual(b["base"], 50)
        self.assertFalse(b["swing"])
        self.assertFalse(b["multiInning"])   # ipApp 1.0 < 1.6


class TestComputeAvail(unittest.TestCase):
    REF = date(2026, 6, 5)

    def test_no_logs(self):
        av = predict.compute_avail([], self.REF)
        self.assertEqual(av["label"], "No recent logs")
        self.assertEqual(av["adj"], 0)
        self.assertIsNone(av["restDays"])

    def test_gassed(self):
        # Pitched 2.0 IP yesterday (restDays 1, lastIP >= 1.8) -> Gassed.
        av = predict.compute_avail([FakeSplit("2026-06-04", "2.0")], self.REF)
        self.assertEqual(av["label"], "Gassed")
        self.assertEqual(av["adj"], -26)

    def test_worked_recently(self):
        # 1.0 IP yesterday: restDays 1 (<=1) but not gassed -> Worked recently.
        av = predict.compute_avail([FakeSplit("2026-06-04", "1.0")], self.REF)
        self.assertEqual(av["label"], "Worked recently")
        self.assertEqual(av["adj"], -12)

    def test_fresh(self):
        # Last outing 4 days ago, light -> Fresh.
        av = predict.compute_avail([FakeSplit("2026-06-01", "1.0")], self.REF)
        self.assertEqual(av["label"], "Fresh")
        self.assertEqual(av["adj"], 6)

    def test_available_default(self):
        # 2 days rest, light workload -> none of the special bands -> Available.
        av = predict.compute_avail([FakeSplit("2026-06-03", "1.0")], self.REF)
        self.assertEqual(av["label"], "Available")
        self.assertEqual(av["adj"], 0)
        self.assertEqual(av["restDays"], 2)

    def test_future_outings_ignored(self):
        # A log dated after the reference date must not count.
        av = predict.compute_avail([FakeSplit("2026-06-10", "3.0")], self.REF)
        self.assertEqual(av["label"], "No recent logs")

    def test_l7_workload_triggers_worked_recently(self):
        # Several outings inside 7 days summing >= 4 IP, last one 3 days back
        # (so not Fresh-eligible via restDays>=3 branch order) -> Worked recently.
        splits = [FakeSplit("2026-06-02", "1.5"),
                  FakeSplit("2026-05-31", "1.5"),
                  FakeSplit("2026-05-30", "1.5")]
        av = predict.compute_avail(splits, self.REF)
        self.assertGreaterEqual(av["ipLast7"], 4)
        self.assertEqual(av["label"], "Worked recently")


class TestStatusInfo(unittest.TestCase):
    def test_live(self):
        s = predict._status_info({"status": {"abstractGameState": "Live",
                                              "detailedState": "In Progress"}})
        self.assertTrue(s["live"])
        self.assertEqual(s["text"], "In Progress")

    def test_final(self):
        s = predict._status_info({"status": {"abstractGameState": "Final"}})
        self.assertFalse(s["live"])
        self.assertEqual(s["text"], "Final")

    def test_scheduled_uses_detailed_when_no_date(self):
        s = predict._status_info({"status": {"abstractGameState": "Preview",
                                             "detailedState": "Scheduled"}})
        self.assertFalse(s["live"])
        self.assertEqual(s["text"], "Scheduled")


class TestTTLCache(unittest.TestCase):
    def test_expired_entry_is_dropped(self):
        store = {}
        predict._cache_set(store, "k", "v", ttl=-1)   # already expired
        self.assertIsNone(predict._cache_get(store, "k"))
        self.assertNotIn("k", store)                  # evicted on read

    def test_live_entry_returns_value(self):
        store = {}
        predict._cache_set(store, "k", {"x": 1}, ttl=60)
        self.assertEqual(predict._cache_get(store, "k"), {"x": 1})

    def test_past_date_gets_long_ttl(self):
        self.assertEqual(predict._payload_ttl("2000-01-01"), predict.PAYLOAD_TTL_PAST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
