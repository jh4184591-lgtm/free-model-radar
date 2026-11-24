# Free Model Radar

**Watch free-tier model windows across providers — and automatically switch when the one you're using stops being free.**

Public `/v1/models` endpoints rarely publish end dates for free models, so this plugin uses **drift detection**: it snapshots the free set and reports when a model appears, vanishes, or flips from free to paid. It also surfaces `expiration_date` values when a provider *does* publish them — including on free models.

Providers:
- **Nous** — keyless
- **OpenRouter** — keyless
- **Groq** — optional, set `GROQ_API_KEY` (Groq's model endpoint requires auth)

No key or account needed for the default path. $0 to run. **A provider outage never counts as drift** — only providers that answered in both snapshots are compared.

## Tools

| Tool | What it does |
|---|---|
| `free_model_radar_status(force_refresh=False)` | Scan all providers + report drift since last scan |
| `free_model_radar_track(model_id, window_end, note)` | Register/list announced free windows with countdowns |
| `free_model_radar_recommend(within_days=7)` | Rank current free models across providers by newest-then-context + capability flags; exclude any with a closing tracked window |
| `free_model_radar_retire(within_days=90)` | List **API-published** `expiration_date`s and countdowns, from every provider |
| `free_model_radar_calendar()` | Write `~/.hermes/free-model-radar/windows.ics` — tracked windows + retire dates as calendar events, each labelled by source |
| `free_model_radar_auto_switch(dry_run=False)` | **Automatically** switch the configured default to the best free same-provider model when the current one leaves the free list. Keeps using the last of the free usage first. Never switches on an outage or to a closing window. `dry_run=True` previews. |

Model IDs are namespaced (`nous:tencent/hy4-flash:free`, `openrouter:inclusionai/ling-3.2-flash:free`) so the same upstream model on two providers is never confused. v1 state files migrate automatically on first load.

Announced windows are labelled **user-supplied, not API** — the plugin never presents a tweet as API fact. Retire dates are explicitly **API-published** — a different, verified source. Recommendations rank on provider metadata (release date + context size + tools/reasoning/synthesized-free flags) — **not a quality benchmark**.

Free pricing uses `float()`, so `"0"`, `"0.0000000000"` and `0` all detect as free. A model with *no* pricing fields at all is treated as unknown, never as free.

## Automatic switching

The watchdog checks the **configured default model** on every run. While it is still in the free list, nothing happens — you keep using the last of its allowance. The moment it leaves the free list, the watchdog switches `model.default` to the best free model on the **same provider** (metadata ranking only), via the supported `hermes config set` CLI, and pings you with what it did. It refuses to switch on a provider outage, when no safe target exists, or to a window closing within 7 days. Every switch is recorded in `state.json` under `switch_log`.

The switch applies to the **next session** — a session already running keeps its current model until restarted; no external script can change a live session's model safely.

Set the cron job's delivery to `bot-chat` (or another real channel) if you want the switch alert delivered as a chat message; `--deliver local` keeps output in the local job log only.

## Watchdog

`scripts/free-model-radar-watchdog.py` is silent unless:
- a free model drifted (vanished / flipped to paid),
- an **API-published retire date is within 14 days** (including free models),
- a user-tracked window is within 3 days (it then suggests a newest-free switch target),
- an automatic switch happened or was needed but unsafe,
- a provider is unexpectedly unavailable.

Recommended schedule: hourly, `--no-agent`, so it costs nothing.

```bash
hermes cron create "0 * * * *" "Free-model radar" --name free-model-radar \
  --script free-model-radar-watchdog.py --no-agent --deliver bot-chat
```

## Tests

23 dependency-free unit tests cover free-price parsing, provider namespacing, v1→v2 migration, partial-outage handling (no false drift), drift classification, `.ics` validity/escaping, tracking and recommendation exclusions, and automatic switching (still-free, gone-model, closing-window exclusion, outage, no-target, dry-run):

```bash
python3 -m unittest discover -s tests -v
```

## Install

```bash
hermes plugins install <repo-url>
hermes plugins enable free-model-radar
```

Or copy this directory into `~/.hermes/plugins/` and enable it.

## State

`~/.hermes/free-model-radar/state.json` — baseline snapshots (last 90) + tracked windows. No credentials, no secrets.

Set `GROQ_API_KEY` only if you want Groq included; it is never required or stored by this plugin.

## Validation

```bash
hermes plugins validate .
hermes plugins doctor .
python3 -m unittest discover -s tests -v
```

## License

MIT
