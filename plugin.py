#!/usr/bin/env python3
"""free-model-radar — detect Nous free-tier window drift.

The public /v1/models endpoint does NOT publish end dates for free models
(expiration_date is null on every :free slug). So this plugin uses drift
detection: snapshot the free set on each check, and report models that
vanished, appeared, or flipped from free to paid.

No credentials. One anonymous HTTPS GET to Nous. $0.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

MODELS_URL = "https://inference-api.nousresearch.com/v1/models"
HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
DATA_DIR = os.path.join(HERMES_HOME, "free-model-radar")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MAX_HISTORY = 90


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            s = json.load(fh)
        if isinstance(s, dict):
            return s
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "created": _now(), "history": [], "known": {}}


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def _fetch_models() -> list[dict]:
    req = urllib.request.Request(MODELS_URL, headers={"User-Agent": "free-model-radar/0.1"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("unexpected /v1/models response shape")
    return data


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        prompt = float(pricing.get("prompt") or 0)
        completion = float(pricing.get("completion") or 0)
    except (TypeError, ValueError):
        return False
    return prompt == 0 and completion == 0


def _free_map(models: list[dict]) -> dict[str, dict]:
    out = {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        if _is_free(m):
            out[mid] = {
                "name": m.get("name", ""),
                "context_length": m.get("context_length"),
                "created": m.get("created"),
                "expiration_date": m.get("expiration_date"),
                "first_seen": None,  # filled by caller from known state
            }
    return out


def _fallbacks(lost_ids: list[str], current_free: dict[str, dict], models: list[dict]) -> list[str]:
    """Suggest replacements: same vendor first, then largest-context free models."""
    vendors = {mid.split("/")[0] for mid in lost_ids if "/" in mid}
    same = [mid for mid in current_free if mid.split("/")[0] in vendors]
    others = sorted(
        current_free,
        key=lambda mid: (current_free[mid].get("context_length") or 0),
        reverse=True,
    )
    seen, out = set(), []
    for mid in same + others:
        if mid not in seen and mid not in lost_ids:
            seen.add(mid)
            out.append(mid)
        if len(out) >= 5:
            break
    return out


def _diff(previous: dict[str, dict], current: dict[str, dict], models: list[dict]) -> dict:
    prev_ids, cur_ids = set(previous), set(current)
    appeared = sorted(cur_ids - prev_ids)
    vanished = sorted(prev_ids - cur_ids)
    all_ids = {m.get("id") for m in models if m.get("id")}
    flipped_paid = sorted(
        mid for mid in vanished if mid in all_ids
    )  # still served, but no longer free
    gone_entirely = sorted(mid for mid in vanished if mid not in all_ids)
    return {
        "appeared": appeared,
        "vanished": vanished,
        "flipped_to_paid": flipped_paid,
        "removed_from_catalog": gone_entirely,
        "fallback_suggestions": _fallbacks(vanished, current, models) if vanished else [],
    }


def _render_report(free: dict[str, dict], diff: dict | None, history: list[dict], known: dict) -> str:
    lines = ["# Free Model Radar", ""]
    lines.append(f"Checked: {_now()}")
    lines.append(f"Free models now: {len(free)}")
    lines.append("")
    for mid in sorted(free):
        info = free[mid]
        first = known.get(mid, {}).get("first_seen")
        age = f" (tracking since {first[:10]})" if first else ""
        win = known.get(mid, {}).get("window_end")
        wintxt = f" [announced free window ends {win}]" if win else ""
        lines.append(f"- {mid}{age}{wintxt}")
    lines.append("")
    if diff is None:
        lines.append("Baseline snapshot saved — no prior scan to compare against.")
    else:
        if not (diff["appeared"] or diff["vanished"]):
            lines.append("No drift since last scan. Free set unchanged.")
        if diff["appeared"]:
            lines.append("NEW free models: " + ", ".join(diff["appeared"]))
        if diff["flipped_to_paid"]:
            lines.append("FLIPPED TO PAID (window likely closed): " + ", ".join(diff["flipped_to_paid"]))
        if diff["removed_from_catalog"]:
            lines.append("REMOVED FROM CATALOG: " + ", ".join(diff["removed_from_catalog"]))
        if diff["fallback_suggestions"]:
            lines.append("Fallbacks: " + ", ".join(diff["fallback_suggestions"]))
    lines.append("")
    lines.append(f"Scans stored: {len(history)}")
    return "\n".join(lines)


def free_model_radar_status(force_refresh: bool = False) -> str:
    """Snapshot the Nous free-model set and report drift since the last scan."""
    try:
        state = _load_state()
        models = _fetch_models()
        current = _free_map(models)

        known = state.setdefault("known", {})
        for mid, info in current.items():
            if mid not in known:
                known[mid] = {"first_seen": _now()}
            info["first_seen"] = known[mid].get("first_seen")

        history = state.setdefault("history", [])
        previous_map: dict[str, dict] = {}
        diff = None
        if history and not force_refresh:
            prev_snapshot = history[-1].get("free", {})
            previous_map = prev_snapshot if isinstance(prev_snapshot, dict) else {}
            diff = _diff(previous_map, current, models)

        snapshot = {"ts": _now(), "free": current}
        history.append(snapshot)
        state["history"] = history[-MAX_HISTORY:]
        state["last_scan"] = snapshot["ts"]
        _save_state(state)

        return json.dumps({
            "report": _render_report(current, diff, history, known),
            "free_count": len(current),
            "drift": diff,
        })
    except Exception as exc:
        return json.dumps({"error": f"radar check failed: {exc!r}"})


def free_model_radar_track(model_id: str = "", window_end: str = "", note: str = "") -> str:
    """Register or list announced free windows (e.g. 'free for two weeks').

    Call with no model_id to list tracked windows. Pass model_id and an
    optional ISO date (YYYY-MM-DD) to set a countdown. The Nous API does not
    publish these dates — they come from announcements, so they are labelled
    as user-supplied, never as API fact.
    """
    try:
        state = _load_state()
        known = state.setdefault("known", {})
        if not model_id:
            tracked = {k: v for k, v in known.items() if v.get("window_end") or v.get("note")}
            if not tracked:
                return json.dumps({"report": "No announced windows tracked yet.", "tracked": {}})
            lines = ["# Tracked free windows (user-supplied, NOT from Nous API)", ""]
            for mid, info in sorted(tracked.items()):
                end = info.get("window_end", "unknown")
                countdown = ""
                if end and end != "unknown":
                    try:
                        days = (datetime.fromisoformat(end).date() - datetime.now(timezone.utc).date()).days
                        countdown = f" — {days} day(s) remaining" if days >= 0 else f" — ENDED {abs(days)} day(s) ago"
                    except ValueError:
                        countdown = " — invalid date"
                lines.append(f"- {mid}: ends {end}{countdown}" + (f" — {info['note']}" if info.get("note") else ""))
            return json.dumps({"report": "\n".join(lines), "tracked": tracked})

        entry = known.setdefault(model_id, {"first_seen": _now()})
        if window_end:
            datetime.fromisoformat(window_end)  # validate YYYY-MM-DD
            entry["window_end"] = window_end
            entry["window_source"] = "user-supplied announcement (not Nous API)"
        if note:
            entry["note"] = note
        _save_state(state)
        return json.dumps({"report": f"Tracking {model_id}: window_end={window_end or entry.get('window_end', 'unset')}", "entry": entry})
    except Exception as exc:
        return json.dumps({"error": f"track failed: {exc!r}"})


# Hermes plugin entry point — ctx provided by the loader.
def plugin_entry(ctx):
    ctx.register_tool(
        name="free_model_radar_status",
        schema={
            "name": "free_model_radar_status",
            "description": (
                "Snapshot the current Nous free-tier model set and report drift since the last "
                "scan: new free models, models that vanished or flipped from free to paid, and "
                "fallback suggestions. Anonymous HTTPS GET only; no keys; $0."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "force_refresh": {
                        "type": "boolean",
                        "description": "Skip drift comparison and just save a fresh baseline.",
                    }
                },
                "required": [],
            },
        },
        handler=lambda args, **kw: free_model_radar_status(
            force_refresh=bool(args.get("force_refresh", False))
        ),
        check_fn=lambda: True,
    )
    ctx.register_tool(
        name="free_model_radar_track",
        schema={
            "name": "free_model_radar_track",
            "description": (
                "Register or list user-supplied free-window announcements (e.g. a model announced "
                "'free for two weeks'). Nous does not publish these dates, so they are tracked as "
                "user-supplied with countdowns. Call with no args to list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {"type": "string", "description": "Exact model slug, e.g. tencent/hy4-flash:free"},
                    "window_end": {"type": "string", "description": "Announced end date, ISO YYYY-MM-DD"},
                    "note": {"type": "string", "description": "Optional source note (tweet, announcement)"},
                },
                "required": [],
            },
        },
        handler=lambda args, **kw: free_model_radar_track(
            model_id=args.get("model_id", ""),
            window_end=args.get("window_end", ""),
            note=args.get("note", ""),
        ),
        check_fn=lambda: True,
    )
