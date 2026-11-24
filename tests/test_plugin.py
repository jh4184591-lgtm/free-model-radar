#!/usr/bin/env python3
"""Unit tests for free-model-radar. No dependencies: python3 -m unittest discover -s tests"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin.py")
spec = importlib.util.spec_from_file_location("fmr_under_test", PLUGIN)
fmr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fmr)


def nous_model(mid, prompt="0", completion="0", **kw):
    m = {"id": f"nous:{mid}", "provider": "nous", "provider_model_id": mid,
         "pricing": {"prompt": prompt, "completion": completion},
         "context_length": 1000, "created": 1700000000}
    m.update(kw)
    return m


def or_model(mid, prompt="0", completion="0", **kw):
    m = nous_model(mid, prompt, completion, **kw)
    m["provider"] = "openrouter"
    m["provider_model_id"] = mid
    m["id"] = f"openrouter:{mid}"
    return m


class IsFreeTests(unittest.TestCase):
    def test_zero_variants(self):
        for p, c in [("0", "0"), ("0.0000000000", "0.0000000000"), (0, 0), (None, None)]:
            with self.subTest(p=p, c=c):
                self.assertTrue(fmr._is_free({"provider": "nous", "pricing": {"prompt": p, "completion": c}}))

    def test_paid_and_malformed(self):
        self.assertFalse(fmr._is_free({"provider": "nous", "pricing": {"prompt": "0.001", "completion": "0"}}))
        self.assertFalse(fmr._is_free({"provider": "nous", "pricing": {"prompt": "bad", "completion": "0"}}))
        self.assertFalse(fmr._is_free({"provider": None, "pricing": {}}))

    def test_groq_is_free_tier_without_pricing(self):
        self.assertTrue(fmr._is_free({"provider": "groq", "pricing": {}}))
        self.assertTrue(fmr._is_free({"provider": "groq"}))


class MapTests(unittest.TestCase):
    def test_namespacing_and_capability_flags(self):
        models = [
            nous_model("tencent/hy4-flash:free", expiration_date=None,
                       supported_parameters=["tools"], reasoning=True,
                       architecture={"modality": "text"}),
            or_model("inclusionai/ling-3.2-flash:free", synthesizedFreeVariant=True),
            nous_model("openai/gpt-5-pro", prompt="0.01", completion="0.05"),
        ]
        out = fmr._free_map(models)
        self.assertEqual(set(out), {"nous:tencent/hy4-flash:free", "openrouter:inclusionai/ling-3.2-flash:free"})
        self.assertTrue(out["nous:tencent/hy4-flash:free"]["supports_tools"])
        self.assertTrue(out["nous:tencent/hy4-flash:free"]["supports_reasoning"])
        self.assertTrue(out["openrouter:inclusionai/ling-3.2-flash:free"]["synthesized_free_variant"])
        self.assertEqual(out["nous:tencent/hy4-flash:free"]["provider"], "nous")


class FetchTests(unittest.TestCase):
    def test_groq_skipped_when_no_key(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GROQ_API_KEY", None)
            res = fmr._fetch_provider("groq", fmr.PROVIDERS["groq"])
        self.assertFalse(res["ok"])
        self.assertTrue(res["skipped"])
        self.assertIn("GROQ_API_KEY", res["reason"])

    def test_partial_failure_keeps_other_provider(self):
        def fake(name, cfg):
            if name == "nous":
                return {"ok": True, "models": [nous_model("tencent/x:free")], "count": 1}
            return {"ok": False, "skipped": False, "reason": "boom", "models": []}
        with patch.object(fmr, "_fetch_provider", side_effect=fake):
            models, status = fmr._fetch_all()
        self.assertEqual([m["id"] for m in models], ["nous:tencent/x:free"])
        self.assertTrue(status["nous"]["ok"])
        self.assertFalse(status["openrouter"]["ok"])

    def test_fetch_provider_namespaces(self):
        payload = {"data": [{"id": "abc/def:free", "pricing": {"prompt": "0", "completion": "0"}}]}
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()
        with patch.object(fmr.urllib.request, "urlopen", return_value=Resp()):
            res = fmr._fetch_provider("openrouter", fmr.PROVIDERS["openrouter"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["models"][0]["id"], "openrouter:abc/def:free")
        self.assertEqual(res["models"][0]["provider_model_id"], "abc/def:free")


class DiffTests(unittest.TestCase):
    def test_no_false_drift_when_provider_was_down(self):
        prev = {"nous:a:free": {}, "openrouter:b:free": {}}
        cur = {"nous:a:free": {}}  # openrouter answered in neither snapshot
        d = fmr._diff(prev, cur, [], prev_ok=["nous", "openrouter"], cur_ok=["nous"])
        self.assertEqual(d["vanished"], [])
        self.assertEqual(d["compared_providers"], ["nous"])

    def test_detects_vanished_flipped_and_new(self):
        prev = {"nous:a:free": {}, "nous:b:free": {}, "openrouter:c:free": {}}
        cur = {"nous:a:free": {}, "nous:new:free": {}, "openrouter:c:free": {}}
        models = [{"id": "nous:b:free"}, {"id": "nous:a:free"}, {"id": "nous:new:free"},
                  {"id": "openrouter:c:free"}]
        d = fmr._diff(prev, cur, models, prev_ok=["nous", "openrouter"], cur_ok=["nous", "openrouter"])
        self.assertEqual(d["appeared"], ["nous:new:free"])
        self.assertEqual(d["flipped_to_paid"], ["nous:b:free"])
        self.assertEqual(d["removed_from_catalog"], [])
        self.assertTrue(d["fallback_suggestions"])

    def test_removed_from_catalog(self):
        prev = {"nous:ghost:free": {}}
        d = fmr._diff(prev, {}, [], prev_ok=["nous"], cur_ok=["nous"])
        self.assertEqual(d["removed_from_catalog"], ["nous:ghost:free"])


class StateMigrationTests(unittest.TestCase):
    def test_v1_migrates_without_false_drift(self):
        with tempfile.TemporaryDirectory() as d:
            old = fmr.STATE_FILE
            fmr.STATE_FILE = os.path.join(d, "state.json")
            try:
                with open(fmr.STATE_FILE, "w") as f:
                    json.dump({"version": 1, "history": [
                        {"ts": "old", "free": {"tencent/hy4-flash:free": {"context_length": 1048576}}}
                    ], "known": {"tencent/hy4-flash:free": {"window_end": "2026-11-30"}}}, f)
                s = fmr._load_state()
                self.assertEqual(s["version"], 2)
                self.assertIn("nous:tencent/hy4-flash:free", s["history"][0]["free"])
                self.assertIn("nous:tencent/hy4-flash:free", s["known"])
                self.assertEqual(s["history"][0]["providers_ok"], ["nous"])
                # idempotent
                self.assertIn("nous:tencent/hy4-flash:free", fmr._load_state()["history"][0]["free"])
            finally:
                fmr.STATE_FILE = old

    def test_v2_id_with_colon_not_double_prefixed(self):
        self.assertEqual(fmr._namespaced("nous:tencent/hy4-flash:free"), "nous:tencent/hy4-flash:free")
        self.assertEqual(fmr._namespaced("openrouter:a/b:free"), "openrouter:a/b:free")
        self.assertEqual(fmr._namespaced("tencent/hy4-flash:free"), "nous:tencent/hy4-flash:free")


class RetireTests(unittest.TestCase):
    def test_free_and_paid_retire_rows(self):
        models = [
            nous_model("nous:paid", prompt="0.01", expiration_date="2026-12-01"),
            or_model("dots-studio/dots-3-note-preview:free", expiration_date="2026-12-31"),
            nous_model("nous:no-exp"),
        ]
        rep = fmr._retire_report(models, within_days=90)
        self.assertEqual(len(rep["rows"]), 2)
        free_row = [r for r in rep["rows"] if r["free"]][0]
        self.assertEqual(free_row["provider"], "openrouter")
        self.assertIn("API-published", rep["source"])


class TrackAndCalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state, self.old_data = fmr.STATE_FILE, fmr.DATA_DIR
        fmr.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        fmr.DATA_DIR = self.tmp.name

    def tearDown(self):
        fmr.STATE_FILE, fmr.DATA_DIR = self.old_state, self.old_data
        self.tmp.cleanup()

    def test_track_namespaces_and_lists(self):
        r = json.loads(fmr.free_model_radar_track("tencent/hy4-flash:free", "2026-11-30", "tweet"))
        self.assertIn("nous:tencent/hy4-flash:free", r["entry"]["id"] if "id" in r["entry"] else r["report"])
        listing = json.loads(fmr.free_model_radar_track())
        self.assertIn("nous:tencent/hy4-flash:free", listing["tracked"])

    def test_calendar_writes_valid_ics(self):
        fmr.free_model_radar_track("tencent/hy4-flash:free", "2026-11-30", "tweet, with comma")
        with patch.object(fmr, "_fetch_all", return_value=(
            [or_model("dots-studio/dots-3-note-preview:free", expiration_date="2026-12-31")],
            {"nous": {"ok": True}, "openrouter": {"ok": True}},
        )):
            r = json.loads(fmr.free_model_radar_calendar())
        self.assertEqual(r["events"], 2)
        with open(r["path"]) as fh:
            txt = fh.read()
        self.assertTrue(txt.startswith("BEGIN:VCALENDAR"))
        self.assertTrue(txt.rstrip().endswith("END:VCALENDAR"))
        self.assertEqual(txt.count("BEGIN:VEVENT"), 2)
        self.assertIn("DTSTART;VALUE=DATE:20261130", txt)
        self.assertIn("tweet\\, with comma", txt)  # RFC 5545 escaping
        self.assertIn("20261231", txt)
        self.assertNotIn("%H%M", txt)
        self.assertIn("DTSTAMP:2026", txt)


class StatusAndRecommendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state, self.old_data = fmr.STATE_FILE, fmr.DATA_DIR
        fmr.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        fmr.DATA_DIR = self.tmp.name

    def tearDown(self):
        fmr.STATE_FILE, fmr.DATA_DIR = self.old_state, self.old_data
        self.tmp.cleanup()

    def test_status_baseline_then_unchanged_then_drift(self):
        models = [nous_model("tencent/hy4-flash:free")]
        status = {"nous": {"ok": True, "count": 1, "skipped": False, "reason": ""},
                  "openrouter": {"ok": False, "count": 0, "skipped": True, "reason": "no key"},
                  "groq": {"ok": False, "count": 0, "skipped": True, "reason": "no key"}}
        with patch.object(fmr, "_fetch_all", return_value=(models, status)):
            first = json.loads(fmr.free_model_radar_status())
            self.assertIn("Baseline", first["report"])
            second = json.loads(fmr.free_model_radar_status())
            self.assertIn("No drift", second["report"])
            third = json.loads(fmr.free_model_radar_status())
            self.assertEqual(third["free_count"], 1)

    def test_recommend_excludes_closing_window(self):
        fmr.free_model_radar_track("nous:tencent/hy4-flash:free", "2026-11-25")
        models = [
            nous_model("tencent/hy4-flash:free", created=1800000000),
            nous_model("meituan/longcat-2.5:free", created=1790000000, context_length=1048576),
            or_model("inclusionai/ling-3.2-flash:free", created=1780000000),
        ]
        with patch.object(fmr, "_fetch_all", return_value=(models, {"nous": {"ok": True}, "openrouter": {"ok": True}})):
            r = json.loads(fmr.free_model_radar_recommend(within_days=7))
        self.assertNotIn("nous:tencent/hy4-flash:free", r["candidates"])
        self.assertEqual(r["recommended"], "nous:meituan/longcat-2.5:free")
        self.assertIn("nous:tencent/hy4-flash:free", r["excluded_closing"])
        self.assertIn("NOT a quality benchmark", r["report"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
