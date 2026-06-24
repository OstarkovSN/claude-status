# claude-status

A zero-dependency terminal mirror for [status.claude.com](https://status.claude.com).

Fetches the Atlassian Statuspage JSON API and renders Claude's current service
health in your terminal: overall indicator, per-component status, active
incidents (with the latest update), and scheduled maintenance. Use `--watch`
to keep a live, auto-refreshing view open.

```
● Claude status — Partially Degraded Service
  page updated 2m ago · fetched 13:27:48 UTC · https://status.claude.com

Components
  ● claude.ai                             Degraded Performance
  ● Claude API (api.anthropic.com)        Degraded Performance
  ● Claude Code                           Degraded Performance
  ● Claude for Government                 Operational
  ── 5 degraded performance, 1 operational

Active incidents (1)
   MINOR  Elevated error rate on Claude Opus 4.8
     Investigating  started 11m ago
     └ We are currently investigating this issue. (11m ago)
     https://stspg.io/wnw8wdn9sfwm
```

## Install

It's a single file with no dependencies (Python 3.9+, stdlib only):

```sh
curl -O https://raw.githubusercontent.com/OstarkovSN/claude-status/main/claude_status.py
chmod +x claude_status.py
./claude_status.py
```

Or drop it on your `PATH`:

```sh
install -m 755 claude_status.py ~/.local/bin/claude-status
claude-status
```

## Usage

```
claude-status              # one snapshot
claude-status --watch      # live mirror, refresh every 30s
claude-status --watch 10   # refresh every 10s (5s floor)
claude-status --json       # raw API JSON, pipeable
claude-status --no-color   # plain text (auto-disabled when not a TTY)
claude-status --url URL    # mirror any other Atlassian Statuspage
```

## Notes

- Pure standard library — nothing to install, runs anywhere Python does.
- Behaves like a proper Unix filter: colour auto-disables when piped, `| head`
  exits cleanly, network/JSON errors are reported on stderr with a non-zero exit.
- `--url` works against any Atlassian Statuspage (they all expose
  `/api/v2/summary.json`), so it doubles as a generic status-page mirror.

## License

MIT — see [LICENSE](LICENSE).
