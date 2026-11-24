"""free-model-radar: Nous free-tier window drift detection.

Registers two tools over a shared implementation in plugin.py. No keys, one
anonymous HTTPS GET to the public Nous /v1/models endpoint, $0.
"""

from __future__ import annotations

from typing import Any

from .plugin import (
    free_model_radar_status,
    free_model_radar_track,
    free_model_radar_recommend,
    free_model_radar_retire,
    free_model_radar_calendar,
    plugin_entry,
)

__version__ = "0.4.0"

_STATUS_SCHEMA = {
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
}

_TRACK_SCHEMA = {
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
}


_RECOMMEND_SCHEMA = {
    "name": "free_model_radar_recommend",
    "description": (
        "Rank currently-free Nous models by newest then largest context and recommend "
        "a switch target, excluding any model whose user-tracked free window is closing. "
        "Ranking uses Nous /v1/models metadata only — NOT a quality benchmark."
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
}


_RETIRE_SCHEMA = {
    "name": "free_model_radar_retire",
    "description": (
        "List Nous models with an API-published expiration_date and a countdown. "
        "Dates come from Nous /v1/models itself (NOT user-supplied)."
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
}


_CALENDAR_SCHEMA = {
    "name": "free_model_radar_calendar",
    "description": (
        "Write an .ics calendar of tracked free-window ends + API-published "
        "retire dates. Import into Apple/Google/Outlook Calendar."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="free_model_radar_status",
        toolset="free_model_radar",
        schema=_STATUS_SCHEMA,
        handler=lambda args, **kw: free_model_radar_status(
            force_refresh=bool(args.get("force_refresh", False))
        ),
    )
    ctx.register_tool(
        name="free_model_radar_track",
        toolset="free_model_radar",
        schema=_TRACK_SCHEMA,
        handler=lambda args, **kw: free_model_radar_track(
            model_id=args.get("model_id", ""),
            window_end=args.get("window_end", ""),
            note=args.get("note", ""),
        ),
    )
    ctx.register_tool(
        name="free_model_radar_recommend",
        toolset="free_model_radar",
        schema=_RECOMMEND_SCHEMA,
        handler=lambda args, **kw: free_model_radar_recommend(
            within_days=args.get("within_days", 7)
        ),
    )
    ctx.register_tool(
        name="free_model_radar_retire",
        toolset="free_model_radar",
        schema=_RETIRE_SCHEMA,
        handler=lambda args, **kw: free_model_radar_retire(
            within_days=args.get("within_days", 90)
        ),
    )
    ctx.register_tool(
        name="free_model_radar_calendar",
        toolset="free_model_radar",
        schema=_CALENDAR_SCHEMA,
        handler=lambda args, **kw: free_model_radar_calendar(),
    )
