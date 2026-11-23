#!/usr/bin/env python3
"""Watchdog wrapper for free-model-radar. Silent unless action is needed.

Emits a short alert only when: a free model drifted (vanished / flipped to
paid), or a user-tracked free window is within 3 days of ending. Empty stdout
means silence under `hermes cron --no-agent`.
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

PLUGIN = os.path.expanduser("~/.hermes/plugins/free-model-radar/plugin.py")
STATE = os.path.expanduser("~/.hermes/free-model-radar/state.json")


def load_module():
    spec = importlib.util.spec_from_file_location("fmr", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    mod = load_module()
    raw = mod.free_model_radar_status()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("free-model-radar: malformed scan result")
        return
    if "error" in data:
        print("free-model-radar: " + str(data["error"]))
        return

    alerts = []
    drift = data.get("drift")
    if drift:
        if drift.get("flipped_to_paid"):
            alerts.append("Flipped to paid: " + ", ".join(drift["flipped_to_paid"]))
        if drift.get("removed_from_catalog"):
            alerts.append("Removed from catalog: " + ", ".join(drift["removed_from_catalog"]))
        if drift.get("appeared"):
            alerts.append("New free models: " + ", ".join(drift["appeared"]))
        if drift.get("fallback_suggestions"):
            alerts.append("Fallbacks: " + ", ".join(drift["fallback_suggestions"]))

    try:
        with open(STATE, "r", encoding="utf-8") as fh:
            known = json.load(fh).get("known", {})
    except (OSError, json.JSONDecodeError):
        known = {}
    today = datetime.now(timezone.utc).date()
    for mid, info in known.items():
        end = info.get("window_end")
        if not end:
            continue
        try:
            days = (datetime.fromisoformat(end).date() - today).days
        except ValueError:
            continue
        if days <= 3:
            status = f"ENDED {abs(days)}d ago" if days < 0 else (
                "ends TODAY" if days == 0 else f"ends in {days}d"
            )
            alerts.append(f"Free window for {mid}: {status} ({end}) [user-supplied, not Nous API]")
            # Suggest the best newest free alternative that is NOT itself closing.
            try:
                rec = json.loads(mod.free_model_radar_recommend(within_days=7))
                if rec.get("recommended") and rec["recommended"] != mid:
                    alerts.append(
                        f"Best newest free switch target: {rec['recommended']} "
                        "(metadata ranking, not a quality benchmark)"
                    )
            except Exception:
                pass

    if alerts:
        print("FREE MODEL RADAR\n" + "\n".join("- " + a for a in alerts))
    # else: silent


if __name__ == "__main__":
    main()
