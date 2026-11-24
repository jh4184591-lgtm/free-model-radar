#!/usr/bin/env python3
"""free-model-radar — detect free-tier window drift across model providers.

The public /v1/models endpoints do NOT reliably publish end dates for free
models, so this plugin uses drift detection: snapshot the free set on each
check, and report models that vanished, appeared, or flipped from free to paid.

Providers:
  - Nous (keyless)
  - OpenRouter (keyless)
  - Groq (optional, needs GROQ_API_KEY; skipped cleanly if absent)

A provider being unavailable is NEVER reported as "all its models vanished":
diffs only compare providers that answered successfully in both snapshots.

No credentials required for the default path. $0.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROVIDERS = {
    "nous": {
        "url": "https://inference-api.nousresearch.com/v1/models",
        "key_env": None,
        "free_policy": "pricing_zero",
        "label": "Nous",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/models",
        "key_env": None,
        "free_policy": "pricing_zero",
        "label": "OpenRouter",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/models",
        "key_env": "GROQ_API_KEY",
        "free_policy": "all_free_tier",  # every Groq model is on the free tier
        "label": "Groq",
    },
}

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
DATA_DIR = os.path.join(HERMES_HOME, "free-model-radar")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MAX_HISTORY = 90
STATE_VERSION = 2
USER_AGENT = "free-model-radar/0.5"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)


def _namespaced(mid: str) -> str:
    """Add a provider prefix to a v1 (Nous-only) model id, idempotently."""
    if not mid:
        return mid
    if mid.split(":", 1)[0] in PROVIDERS:
        return mid
    return f"nous:{mid}"


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            s = json.load(fh)
        if not isinstance(s, dict):
            raise ValueError("state is not an object")
    except (OSError, json.JSONDecodeError, ValueError):
        return {"version": STATE_VERSION, "created": _now(), "history": [], "known": {}}

    # v1 -> v2 migration: prefix old Nous-only ids so a provider upgrade does
    # not look like every model vanished and a new set appeared.
    if int(s.get("version", 1)) < STATE_VERSION:
        for snap in s.get("history", []):
            free = snap.get("free")
            if isinstance(free, dict):
                snap["free"] = {_namespaced(k): v for k, v in free.items()}
            snap["providers_ok"] = ["nous"]
        s["known"] = {_namespaced(k): v for k, v in (s.get("known") or {}).items()}
        s["version"] = STATE_VERSION
        _save_state(s)
    return s


def _fetch_provider(name: str, cfg: dict) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    key_env = cfg.get("key_env")
    if key_env:
        key = os.environ.get(key_env)
        if not key:
            return {"ok": False, "skipped": True, "reason": f"{key_env} not set", "models": []}
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(cfg["url"], headers=headers)
    with urllib.request.urlopen(req, timeout=25) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected /v1/models response shape from {name}")
    out = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        item = dict(m)
        original = item["id"]
        item["provider"] = name
        item["provider_model_id"] = original
        item["id"] = f"{name}:{original}"
        out.append(item)
    return {"ok": True, "models": out, "count": len(out)}


def _fetch_all() -> tuple[list[dict], dict]:
    """Fetch every configured provider. Never let one failure erase the others."""
    models: list[dict] = []
    status: dict = {}
    for name, cfg in PROVIDERS.items():
        try:
            res = _fetch_provider(name, cfg)
            status[name] = {
                "ok": bool(res["ok"]),
                "count": len(res.get("models", [])),
                "skipped": bool(res.get("skipped")),
                "reason": res.get("reason", ""),
            }
            if res["ok"]:
                models.extend(res["models"])
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
            status[name] = {"ok": False, "count": 0, "skipped": False, "reason": repr(exc)}
    return models, status


def _is_free(model: dict) -> bool:
    provider = model.get("provider")
    policy = PROVIDERS.get(provider, {}).get("free_policy", "pricing_zero")
    if policy == "all_free_tier":
        return True
    pricing = model.get("pricing") or {}
    # An absent pricing object means "unknown", not "free". Never guess free
    # from missing data — that would silently mislabel every model on a shape
    # change. Require at least one explicit price field to be present.
    if "prompt" not in pricing and "completion" not in pricing:
        return False
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
            arch = m.get("architecture") or {}
            out[mid] = {
                "provider": m.get("provider", "nous"),
                "provider_model_id": m.get("provider_model_id", mid.split(":", 1)[-1]),
                "name": m.get("name", ""),
                "context_length": m.get("context_length"),
                "created": m.get("created"),
                "expiration_date": m.get("expiration_date"),
                "synthesized_free_variant": bool(m.get("synthesizedFreeVariant")),
                "supports_tools": "tools" in (m.get("supported_parameters") or []),
                "supports_reasoning": bool(m.get("reasoning")),
                "modality": arch.get("modality"),
                "first_seen": None,  # filled by caller from known state
            }
    return out


def _fallbacks(lost_ids: list[str], current_free: dict[str, dict], models: list[dict]) -> list[str]:
    """Suggest replacements: same provider first, then largest-context free models."""
    providers = {mid.split(":", 1)[0] for mid in lost_ids if ":" in mid}
    same = [mid for mid in current_free if mid.split(":", 1)[0] in providers]
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


def _diff(previous: dict, current: dict, models: list[dict],
          prev_ok: list[str] | None = None, cur_ok: list[str] | None = None) -> dict:
    # Only compare providers that answered in BOTH snapshots. A provider outage
    # must never be reported as its models vanishing.
    if prev_ok is not None and cur_ok is not None:
        common = set(prev_ok) & set(cur_ok)
    else:
        common = {mid.split(":", 1)[0] for mid in set(previous) | set(current) if ":" in mid}
    prev_ids = {mid for mid in previous if mid.split(":", 1)[0] in common}
    cur_ids = {mid for mid in current if mid.split(":", 1)[0] in common}
    appeared = sorted(cur_ids - prev_ids)
    vanished = sorted(prev_ids - cur_ids)
    all_ids = {m.get("id") for m in models if m.get("id")}
    flipped_paid = sorted(mid for mid in vanished if mid in all_ids)
    gone_entirely = sorted(mid for mid in vanished if mid not in all_ids)
    return {
        "appeared": appeared,
        "vanished": vanished,
        "flipped_to_paid": flipped_paid,
        "removed_from_catalog": gone_entirely,
        "compared_providers": sorted(common),
        "fallback_suggestions": _fallbacks(vanished, current, models) if vanished else [],
    }


def _render_report(free: dict, diff: dict | None, history: list[dict], known: dict, status: dict) -> str:
    lines = ["# Free Model Radar", ""]
    lines.append(f"Checked: {_now()}")
    lines.append(f"Free models now: {len(free)}")
    per = {}
    for mid in free:
        p = mid.split(":", 1)[0]
        per[p] = per.get(p, 0) + 1
    lines.append("By provider: " + (", ".join(f"{k}={v}" for k, v in sorted(per.items())) or "none"))
    stat = []
    for name, s in status.items():
        if s["ok"]:
            stat.append(f"{name}: ok ({s['count']} models)")
        else:
            stat.append(f"{name}: UNAVAILABLE ({s.get('reason', 'unknown')})")
    lines.append("Sources: " + "; ".join(stat))
    lines.append("")
    for mid in sorted(free):
        info = free[mid]
        first = known.get(mid, {}).get("first_seen")
        age = f" (tracking since {first[:10]})" if first else ""
        win = known.get(mid, {}).get("window_end")
        wintxt = f" [announced free window ends {win}]" if win else ""
        exp = info.get("expiration_date")
        exptxt = f" [API retire date {exp}]" if exp else ""
        lines.append(f"- {mid}{age}{wintxt}{exptxt}")
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
        if diff.get("compared_providers"):
            lines.append("Compared providers: " + ", ".join(diff["compared_providers"]))
        if diff["fallback_suggestions"]:
            lines.append("Fallbacks: " + ", ".join(diff["fallback_suggestions"]))
    lines.append("")
    lines.append(f"Scans stored: {len(history)}")
    return "\n".join(lines)


def free_model_radar_status(force_refresh: bool = False) -> str:
    """Snapshot the free-model set across providers and report drift."""
    try:
        state = _load_state()
        models, status = _fetch_all()
        current = _free_map(models)

        known = state.setdefault("known", {})
        for mid, info in current.items():
            if mid not in known:
                known[mid] = {"first_seen": _now()}
            info["first_seen"] = known[mid].get("first_seen")

        history = state.setdefault("history", [])
        diff = None
        if history and not force_refresh:
            prev_snapshot = history[-1]
            previous_map = prev_snapshot.get("free", {})
            if not isinstance(previous_map, dict):
                previous_map = {}
            diff = _diff(
                previous_map, current, models,
                prev_ok=prev_snapshot.get("providers_ok"),
                cur_ok=[n for n, s in status.items() if s["ok"]],
            )

        snapshot = {
            "ts": _now(),
            "free": current,
            "providers_ok": [n for n, s in status.items() if s["ok"]],
        }
        history.append(snapshot)
        state["history"] = history[-MAX_HISTORY:]
        state["last_scan"] = snapshot["ts"]
        _save_state(state)

        return json.dumps({
            "report": _render_report(current, diff, history, known, status),
            "free_count": len(current),
            "drift": diff,
            "provider_status": status,
        })
    except Exception as exc:
        return json.dumps({"error": f"radar check failed: {exc!r}"})


def free_model_radar_track(model_id: str = "", window_end: str = "", note: str = "") -> str:
    """Register or list announced free windows (user-supplied, never API fact)."""
    try:
        state = _load_state()
        known = state.setdefault("known", {})
        if not model_id:
            tracked = {k: v for k, v in known.items() if v.get("window_end") or v.get("note")}
            if not tracked:
                return json.dumps({"report": "No announced windows tracked yet.", "tracked": {}})
            lines = ["# Tracked free windows (user-supplied, NOT from any provider API)", ""]
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

        mid = _namespaced(model_id)
        entry = known.setdefault(mid, {"first_seen": _now()})
        if window_end:
            datetime.fromisoformat(window_end)
            entry["window_end"] = window_end
            entry["window_source"] = "user-supplied announcement (not provider API)"
        if note:
            entry["note"] = note
        _save_state(state)
        return json.dumps({"report": f"Tracking {mid}: window_end={window_end or entry.get('window_end', 'unset')}", "entry": entry})
    except Exception as exc:
        return json.dumps({"error": f"track failed: {exc!r}"})


def _retire_report(models: list[dict], within_days: int = 90) -> dict:
    """Models with an expiration_date published BY a provider API."""
    today = datetime.now(timezone.utc).date()
    rows = []
    for m in models:
        exp = m.get("expiration_date")
        if not exp:
            continue
        try:
            end = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        days = (end - today).days
        if days > within_days:
            continue
        rows.append({
            "id": m.get("id"),
            "provider": m.get("provider", "nous"),
            "expiration_date": str(exp),
            "days_left": days,
            "free": _is_free(m),
            "prompt_price": (m.get("pricing") or {}).get("prompt"),
        })
    rows.sort(key=lambda r: (r["days_left"], r["id"]))
    return {"rows": rows, "source": "provider /v1/models expiration_date (API-published)"}


def free_model_radar_retire(within_days: int = 90) -> str:
    """List API-published expiration dates with a countdown."""
    try:
        within_days = int(within_days)
        models, status = _fetch_all()
        rep = _retire_report(models, within_days)
        lines = [
            "# API-published retire dates",
            "",
            f"Checked: {_now()}",
            "Source: provider /v1/models `expiration_date` — published by the APIs, "
            "NOT user-supplied. Today this is set on some paid models and at least "
            "one free OpenRouter model; no Nous free model has one.",
            "",
        ]
        if not rep["rows"]:
            lines.append(f"No expiration dates within {within_days} days.")
        else:
            for r in rep["rows"]:
                tag = "FREE" if r["free"] else "paid"
                status_txt = f"ENDED {abs(r['days_left'])}d ago" if r["days_left"] < 0 else (
                    "today" if r["days_left"] == 0 else f"in {r['days_left']}d"
                )
                lines.append(
                    f"- {r['id']} [{r['provider']}/{tag}] — expires {r['expiration_date']} ({status_txt})"
                )
        return json.dumps({
            "report": "\n".join(lines),
            "rows": rep["rows"],
            "source": rep["source"],
            "provider_status": status,
        })
    except Exception as exc:
        return json.dumps({"error": f"retire failed: {exc!r}"})


def _closing_soon(known: dict, within_days: int = 7) -> set[str]:
    """User-tracked windows ending within N days (or already ended)."""
    today = datetime.now(timezone.utc).date()
    out = set()
    for mid, info in known.items():
        end = info.get("window_end")
        if not end:
            continue
        try:
            days = (datetime.fromisoformat(end).date() - today).days
        except ValueError:
            continue
        if days <= within_days:
            out.add(mid)
    return out


def _ics_safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "-" for c in str(s))


def free_model_radar_calendar() -> str:
    """Write an iCalendar (.ics) of tracked windows + API retire dates."""
    try:
        state = _load_state()
        known = state.get("known", {})

        def esc(s: str) -> str:
            return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

        def stamp(dt: datetime) -> str:
            return dt.strftime("%Y%m%dT%H%M%SZ")

        now_utc = datetime.now(timezone.utc)
        events = []
        for mid, info in known.items():
            end_raw = info.get("window_end")
            if not end_raw:
                continue
            try:
                end = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            note = info.get("note", "")
            src = info.get("window_source", "user-supplied")
            events.append([
                f"UID:fmr-window-{_ics_safe(mid)}-{_ics_safe(end_raw)}@free-model-radar",
                f"DTSTAMP:{stamp(now_utc)}",
                f"DTSTART;VALUE=DATE:{end.strftime('%Y%m%d')}",
                f"SUMMARY:Free window ends: {esc(mid)}",
                f"DESCRIPTION:Source: {esc(src)}. Note: {esc(note)}",
            ])
        models, status = _fetch_all()
        for r in _retire_report(models, within_days=365)["rows"]:
            try:
                end = datetime.fromisoformat(str(r["expiration_date"]).replace("Z", "+00:00"))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            events.append([
                f"UID:fmr-retire-{_ics_safe(r['id'])}-{_ics_safe(r['expiration_date'])}@free-model-radar",
                f"DTSTAMP:{stamp(now_utc)}",
                f"DTSTART;VALUE=DATE:{end.strftime('%Y%m%d')}",
                f"SUMMARY:Model retires: {esc(r['id'])} ({r['free'] and 'FREE' or 'paid'})",
                f"DESCRIPTION:Source: {r['provider']} /v1/models expiration_date (API-published).",
            ])

        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//free-model-radar//EN", "CALSCALE:GREGORIAN"]
        for ev in events:
            lines.append("BEGIN:VEVENT")
            lines.extend(ev)
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        path = os.path.join(DATA_DIR, "windows.ics")
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\r\n".join(lines) + "\r\n")
        return json.dumps({
            "path": path,
            "events": len(events),
            "provider_status": status,
            "report": f"Wrote {len(events)} event(s) to {path}\n  Import into Apple Calendar / Google Calendar / Outlook.",
        })
    except Exception as exc:
        return json.dumps({"error": f"calendar failed: {exc!r}"})


def free_model_radar_recommend(within_days: int = 7) -> str:
    """Rank currently-free models by newest then context, excluding closing windows.
    Metadata only, NOT a quality benchmark.
    """
    try:
        within_days = int(within_days)
        state = _load_state()
        known = state.get("known", {})
        models, status = _fetch_all()
        free = _free_map(models)
        closing = _closing_soon(known, within_days)
        candidates = [(mid, info) for mid, info in free.items() if mid not in closing]
        candidates.sort(
            key=lambda pair: (
                pair[1].get("created") or 0,
                pair[1].get("context_length") or 0,
            ),
            reverse=True,
        )
        top = candidates[0][0] if candidates else None
        lines = [
            "# Free model switch recommendation",
            "",
            f"Checked: {_now()}",
            "Ranking: newest created, then largest context (metadata only, NOT a quality benchmark).",
            f"Excluded (tracked window within {within_days}d): "
            + (", ".join(sorted(closing)) if closing else "none"),
            "",
        ]
        if not candidates:
            lines.append("No free models outside a closing window — nothing safe to recommend.")
        else:
            for i, (mid, info) in enumerate(candidates, 1):
                ctx = info.get("context_length") or 0
                created = info.get("created")
                created_txt = (
                    datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%d")
                    if created else "unknown"
                )
                marker = " <-- best newest" if i == 1 else ""
                caps = []
                if info.get("supports_tools"):
                    caps.append("tools")
                if info.get("supports_reasoning"):
                    caps.append("reasoning")
                if info.get("synthesized_free_variant"):
                    caps.append("synthesized-free")
                cap_txt = "+".join(caps) if caps else "basic"
                lines.append(f"{i}. {mid} — ctx {ctx:,}, created {created_txt}, {cap_txt}{marker}")
        return json.dumps({
            "report": "\n".join(lines),
            "recommended": top,
            "candidates": [mid for mid, _ in candidates],
            "excluded_closing": sorted(closing),
            "provider_status": status,
        })
    except Exception as exc:
        return json.dumps({"error": f"recommend failed: {exc!r}"})


# Hermes plugin entry point — ctx provided by the loader.
def plugin_entry(ctx):
    ctx.register_tool(
        name="free_model_radar_status",
        schema={
            "name": "free_model_radar_status",
            "description": (
                "Snapshot the current free-tier model set across Nous, OpenRouter and "
                "(optionally) Groq, and report drift: new free models, models that "
                "vanished or flipped to paid, and fallbacks. Provider outages never "
                "count as drift. Keyless for Nous/OpenRouter; $0."
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
                "Register or list user-supplied free-window announcements (e.g. 'free for "
                "two weeks'). Providers do not publish these dates, so they are labelled "
                "user-supplied with countdowns. Call with no args to list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_id": {
                        "type": "string",
                        "description": "Model slug, e.g. tencent/hy4-flash:free or nous:tencent/hy4-flash:free",
                    },
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
    ctx.register_tool(
        name="free_model_radar_recommend",
        schema={
            "name": "free_model_radar_recommend",
            "description": (
                "Rank currently-free models by newest then largest context and recommend a "
                "switch target, excluding any model whose user-tracked window is closing. "
                "Metadata only — NOT a quality benchmark."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "within_days": {
                        "type": "integer",
                        "description": "Exclude windows ending within this many days (default 7).",
                    }
                },
                "required": [],
            },
        },
        handler=lambda args, **kw: free_model_radar_recommend(
            within_days=args.get("within_days", 7)
        ),
        check_fn=lambda: True,
    )
    ctx.register_tool(
        name="free_model_radar_retire",
        schema={
            "name": "free_model_radar_retire",
            "description": (
                "List models with an API-published expiration_date and a countdown. Dates "
                "come from provider /v1/models itself (NOT user-supplied). Covers Nous and "
                "OpenRouter retire dates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "within_days": {
                        "type": "integer",
                        "description": "Only show dates this many days out (default 90).",
                    }
                },
                "required": [],
            },
        },
        handler=lambda args, **kw: free_model_radar_retire(
            within_days=args.get("within_days", 90)
        ),
        check_fn=lambda: True,
    )
    ctx.register_tool(
        name="free_model_radar_calendar",
        schema={
            "name": "free_model_radar_calendar",
            "description": (
                "Write an .ics calendar of tracked free-window ends + API-published retire "
                "dates across providers. Import into Apple/Google/Outlook Calendar. Each "
                "event labels its source (user-supplied vs API)."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        handler=lambda args, **kw: free_model_radar_calendar(),
        check_fn=lambda: True,
    )
