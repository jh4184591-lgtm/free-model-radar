# Free Model Radar

**Watch Nous free-tier model windows and tell you before one closes.**

Nous publishes free models at `/v1/models`, but the `expiration_date` field is `null` on every free model — so there is no announced end date to read. This plugin uses **drift detection** instead: it snapshots the free set and reports when a model appears, vanishes, or flips from free to paid, with fallback suggestions.

No API key, no account, no payment. One anonymous HTTPS GET to the public Nous model list. $0 to run.

## Tools

| Tool | What it does |
|---|---|
| `free_model_radar_status(force_refresh=False)` | Scan + report drift since last scan |
| `free_model_radar_track(model_id, window_end, note)` | Register/list announced free windows with countdowns |
| `free_model_radar_recommend(within_days=7)` | Rank current free models by newest-then-context + capability flags and suggest a switch target, excluding any with a closing tracked window |
| `free_model_radar_retire(within_days=90)` | List models with an **API-published** `expiration_date` and a countdown (mostly paid models today) |
| `free_model_radar_calendar()` | Write `~/.hermes/free-model-radar/windows.ics` — tracked windows + retire dates as calendar events, each labelled by source. Import into Apple/Google/Outlook Calendar |

Announced windows are labelled **user-supplied, not Nous API** — the plugin never presents a tweet as an API fact. Retire dates from `free_model_radar_retire` are explicitly **API-published** — a different, verified source. Recommendations are ranked on Nous `/v1/models` metadata (release date + context size + tools/reasoning/synthesized-free flags) — **not a quality benchmark**.

Note: free pricing is parsed with `float()`, so both `"0"` and `"0.0000000000"` are correctly detected as free.

## Switch before a window closes

```python
free_model_radar_recommend(within_days=7)
# -> {"recommended": "meituan/longcat-2.5:free", "excluded_closing": ["tencent/hy4-flash:free"], ...}
```

The daily watchdog also prints the best newest free switch target when a tracked window comes within 3 days of ending.

## Install

```bash
hermes plugins install <repo-url>
hermes plugins enable free-model-radar
```

Or copy this directory into `~/.hermes/plugins/` and run `hermes plugins enable free-model-radar`.

## Optional: daily watchdog (silent unless action needed)

`scripts/free-model-radar-watchdog.py` emits output only on drift or a tracked window within 3 days. Schedule with `--no-agent` so it costs nothing:

```bash
hermes cron create "0 8 * * *" "Free-model radar" --name free-model-radar \
  --script free-model-radar-watchdog.py --no-agent --deliver local
```

## State

`~/.hermes/free-model-radar/state.json` — baseline snapshots (last 90) + tracked windows. No credentials, no secrets.

## Validation

```bash
hermes plugins validate . --install-deps
hermes plugins doctor .
```

## License

MIT
