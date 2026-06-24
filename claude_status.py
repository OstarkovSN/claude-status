#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Terminal mirror for https://status.claude.com/

Fetches the Atlassian Statuspage JSON API and renders the current Claude
service status in the terminal: overall indicator, per-component health,
active incidents (with the latest update), and scheduled maintenance.

Zero third-party dependencies (stdlib only) so it runs anywhere with no
install. Use --watch to keep a live, auto-refreshing mirror open.

Examples:
    claude_status.py                 # one snapshot
    claude_status.py --watch         # live mirror, refresh every 30s
    claude_status.py --watch 10      # refresh every 10s
    claude_status.py --json          # raw API JSON (for piping)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE = "https://status.claude.com"
SUMMARY_PATH = "/api/v2/summary.json"
USER_AGENT = "claude-status-mirror/1.0 (+terminal)"

# ── ANSI ────────────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
_COLORS = {
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
    "cyan": "\033[96m",
    "grey": "\033[90m",
}

# Overall-page indicator → colour.
_INDICATOR_COLOR = {
    "none": "green",
    "minor": "yellow",
    "major": "magenta",
    "critical": "red",
    "maintenance": "blue",
}
# Per-component status → (colour, symbol).
_COMPONENT_STYLE = {
    "operational": ("green", "●"),
    "degraded_performance": ("yellow", "●"),
    "partial_outage": ("magenta", "●"),
    "major_outage": ("red", "●"),
    "under_maintenance": ("blue", "●"),
}
# Incident impact → colour.
_IMPACT_COLOR = {
    "none": "grey",
    "maintenance": "blue",
    "minor": "yellow",
    "major": "magenta",
    "critical": "red",
}

_USE_COLOR = True


def paint(text: str, color: str, *, bold: bool = False, dim: bool = False) -> str:
    """Wrap text in ANSI colour codes unless colour is disabled."""
    if not _USE_COLOR:
        return text
    pre = _COLORS.get(color, "")
    if bold:
        pre = BOLD + pre
    if dim:
        pre = DIM + pre
    return f"{pre}{text}{RESET}" if pre else text


def humanize(label: str) -> str:
    """`degraded_performance` -> `Degraded Performance`."""
    return label.replace("_", " ").title()


def fetch_summary(base: str, timeout: float = 15.0, retries: int = 2,
                  backoff: float = 0.5) -> dict:
    """GET the statuspage summary JSON, retrying transient failures.

    Connection errors, timeouts, and 5xx responses are retried up to
    ``retries`` times with linear backoff. 4xx responses fail fast — they
    won't fix themselves on retry. Raises the last error if every attempt
    fails, or json.JSONDecodeError on a malformed body.
    """
    url = base.rstrip("/") + SUMMARY_PATH
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError is a URLError subclass — catch it first. Only server
            # errors (5xx) are worth retrying; client errors (4xx) are not.
            if exc.code < 500 or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError("unreachable: retry loop exhausted")  # pragma: no cover


def parse_ts(value: str | None):
    """Parse an ISO8601 timestamp (statuspage uses trailing Z)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def rel_age(value: str | None) -> str:
    """Human relative age, e.g. `12m ago`, `3h ago`."""
    dt = parse_ts(value)
    if dt is None:
        return ""
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        return "in the future"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def render(summary: dict) -> str:
    """Build the full terminal view from a summary.json payload."""
    lines: list[str] = []
    page = summary.get("page", {})
    status = summary.get("status", {})
    indicator = status.get("indicator", "unknown")
    color = _INDICATOR_COLOR.get(indicator, "cyan")
    name = page.get("name", "Claude")
    desc = status.get("description", "Unknown")

    dot = paint("●", color, bold=True)
    lines.append(f"{dot} {paint(name + ' status', 'cyan', bold=True)} — "
                 f"{paint(desc, color, bold=True)}")
    updated = page.get("updated_at")
    fetched = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    meta = f"  page updated {rel_age(updated)} · fetched {fetched} · {page.get('url', '')}"
    lines.append(paint(meta, "grey", dim=True))
    lines.append("")

    # ── Components ───────────────────────────────────────────────────────────
    # Statuspage splits components into group containers (``group: true``, with
    # a ``components`` id-list) and leaf services (which carry ``group_id``).
    # Render groups as headings with their leaves indented beneath, ungrouped
    # leaves at the top level, and tally only the leaves.
    all_components = summary.get("components", [])
    group_ids = {c.get("id") for c in all_components if c.get("group")}
    leaves = [c for c in all_components if not c.get("group")]
    if leaves:
        width = max((len(c.get("name", "")) for c in leaves), default=0)

        def leaf_line(comp: dict, indent: str) -> str:
            st = comp.get("status", "unknown")
            col, sym = _COMPONENT_STYLE.get(st, ("cyan", "○"))
            nm = comp.get("name", "").ljust(width)
            return f"{indent}{paint(sym, col)} {nm}  {paint(humanize(st), col)}"

        # Walk in array order: collect each group's leaves, keep ungrouped
        # leaves inline. Orphan leaves (a group_id with no matching group)
        # fall through to the top level rather than vanishing.
        children: dict[str, list[dict]] = {}
        plan: list[tuple[str, dict]] = []
        for c in all_components:
            if c.get("group"):
                plan.append(("group", c))
            elif c.get("group_id") in group_ids:
                children.setdefault(c.get("group_id"), []).append(c)
            else:
                plan.append(("leaf", c))

        lines.append(paint("Components", "cyan", bold=True))
        for kind, c in plan:
            if kind == "group":
                kids = children.get(c.get("id"), [])
                if not kids:
                    continue  # empty container — nothing to show
                lines.append(f"  {paint(c.get('name', ''), 'cyan', bold=True)}")
                for ch in kids:
                    lines.append(leaf_line(ch, "    "))
            else:
                lines.append(leaf_line(c, "  "))

        counts: dict[str, int] = {}
        for c in leaves:
            st = c.get("status", "unknown")
            counts[st] = counts.get(st, 0) + 1
        summary_bits = [paint(f"{n} {humanize(s).lower()}",
                              _COMPONENT_STYLE.get(s, ("cyan", ""))[0])
                        for s, n in sorted(counts.items())]
        lines.append(paint("  ── ", "grey", dim=True) + ", ".join(summary_bits))
    lines.append("")

    # ── Active incidents ─────────────────────────────────────────────────────
    incidents = summary.get("incidents", [])
    if incidents:
        lines.append(paint(f"Active incidents ({len(incidents)})", "red", bold=True))
        for inc in incidents:
            impact = inc.get("impact", "none")
            icol = _IMPACT_COLOR.get(impact, "yellow")
            badge = paint(f" {impact.upper()} ", icol, bold=True)
            lines.append(f"  {badge} {paint(inc.get('name',''), 'cyan', bold=True)}")
            meta = (f"     {humanize(inc.get('status','')):14} "
                    f"started {rel_age(inc.get('started_at'))}")
            lines.append(paint(meta, "grey"))
            updates = inc.get("incident_updates", [])
            if updates:
                latest = updates[0]
                body = " ".join((latest.get("body") or "").split())
                if len(body) > 200:
                    body = body[:197] + "..."
                age = paint(f"({rel_age(latest.get('created_at'))})", "grey", dim=True)
                lines.append(f"     {paint('└', 'grey')} {body} {age}")
            if inc.get("shortlink"):
                lines.append(paint(f"     {inc['shortlink']}", "blue", dim=True))
            lines.append("")
    else:
        lines.append(paint("No active incidents.", "green"))
        lines.append("")

    # ── Scheduled maintenance ────────────────────────────────────────────────
    maints = summary.get("scheduled_maintenances", [])
    active_maints = [m for m in maints if m.get("status") not in ("completed",)]
    if active_maints:
        lines.append(paint(f"Scheduled maintenance ({len(active_maints)})", "blue", bold=True))
        for m in active_maints:
            lines.append(f"  {paint('◆', 'blue')} {paint(m.get('name',''), 'cyan')}")
            sched = (f"     {humanize(m.get('status',''))} · "
                     f"{m.get('scheduled_for','?')} → {m.get('scheduled_until','?')}")
            lines.append(paint(sched, "grey"))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Terminal mirror for status.claude.com",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--watch", nargs="?", const=30, type=int, default=None,
                        metavar="SECONDS",
                        help="live mirror: refresh every SECONDS (default 30)")
    parser.add_argument("--json", action="store_true",
                        help="print raw API JSON and exit")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    parser.add_argument("--url", default=DEFAULT_BASE,
                        help=f"statuspage base URL (default {DEFAULT_BASE})")
    args = parser.parse_args(argv)

    # Behave like a normal Unix filter: a closed downstream pipe (`| head`)
    # terminates us via SIGPIPE instead of raising BrokenPipeError + traceback.
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):
        pass  # non-Unix or no SIGPIPE — the __main__ guard still covers it

    global _USE_COLOR
    _USE_COLOR = (not args.no_color) and sys.stdout.isatty()

    def snapshot() -> int:
        try:
            data = fetch_summary(args.url)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            print(paint(f"✗ could not reach {args.url}: {exc}", "red"), file=sys.stderr)
            return 1
        except json.JSONDecodeError as exc:
            print(paint(f"✗ bad JSON from status API: {exc}", "red"), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(render(data), end="")
        return 0

    if args.watch is None:
        return snapshot()

    interval = max(5, args.watch)  # be polite to the API
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")  # clear screen, home cursor
            rc = snapshot()
            if not args.json:
                print(paint(f"\n↻ refreshing every {interval}s — Ctrl-C to exit", "grey", dim=True))
            sys.stdout.flush()
            if rc != 0:
                time.sleep(min(interval, 15))
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # Downstream pipe (e.g. `| head`) closed early — exit quietly without
        # the interpreter-shutdown flush dumping a traceback.
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0) from None
