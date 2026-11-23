"""free-model-radar: Nous free-tier window drift detection.

Registers two tools over a shared implementation in plugin.py. No keys, one
anonymous HTTPS GET to the public Nous /v1/models endpoint, $0.
"""

from __future__ import annotations

from typing import Any

from .plugin import (
    free_model_radar_status,
    free_model_radar_track,
    plugin_entry,
)

__version__ = "0.1.0"

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
