"""Tests for claude_status — the zero-dependency status.claude.com mirror.

The tool talks to the network and renders relative timestamps, so the two
things that make it awkward to test are (1) `urllib` calls and (2) "now". We
handle both with monkeypatching: a fake `urlopen` for the fetch path, and a
frozen `datetime` so relative-age strings are deterministic.

No third-party test deps — pytest + stdlib only, matching the tool's ethos.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import claude_status as cs

FIXTURES = Path(__file__).parent / "fixtures"

# A fixed "now" so rel_age() / the "fetched" line are deterministic.
FROZEN_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenDateTime(datetime):
    """A datetime whose .now() is pinned; everything else is inherited."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


@pytest.fixture
def frozen_time(monkeypatch):
    """Pin module-level `datetime` so time-dependent output is stable."""
    monkeypatch.setattr(cs, "datetime", _FrozenDateTime)


@pytest.fixture
def no_color(monkeypatch):
    """Render plain text so assertions match on content, not ANSI codes."""
    monkeypatch.setattr(cs, "_USE_COLOR", False)


def _ago(**kw) -> str:
    """ISO8601 (Z-suffixed) timestamp `kw` before FROZEN_NOW."""
    return (FROZEN_NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ── humanize ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("degraded_performance", "Degraded Performance"),
    ("operational", "Operational"),
    ("major_outage", "Major Outage"),
    ("", ""),
])
def test_humanize(raw, expected):
    assert cs.humanize(raw) == expected


# ── parse_ts ──────────────────────────────────────────────────────────────────
def test_parse_ts_valid_z_suffix():
    dt = cs.parse_ts("2026-06-24T11:30:00.000Z")
    assert dt == datetime(2026, 6, 24, 11, 30, tzinfo=timezone.utc)


def test_parse_ts_with_offset():
    dt = cs.parse_ts("2026-06-24T11:30:00+00:00")
    assert dt == datetime(2026, 6, 24, 11, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-99"])
def test_parse_ts_bad_returns_none(bad):
    assert cs.parse_ts(bad) is None


# ── rel_age ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("delta,expected", [
    ({"seconds": 5}, "5s ago"),
    ({"seconds": 90}, "1m ago"),
    ({"minutes": 45}, "45m ago"),
    ({"hours": 3}, "3h ago"),
    ({"hours": 25}, "1d ago"),
    ({"days": 11}, "11d ago"),
])
def test_rel_age_buckets(frozen_time, delta, expected):
    assert cs.rel_age(_ago(**delta)) == expected


def test_rel_age_future(frozen_time):
    future = (FROZEN_NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert cs.rel_age(future) == "in the future"


def test_rel_age_empty_or_bad():
    assert cs.rel_age(None) == ""
    assert cs.rel_age("garbage") == ""


# ── render ────────────────────────────────────────────────────────────────────
def _component(name, status, group=False, group_id=None, id=None):
    return {"id": id, "name": name, "status": status,
            "group": group, "group_id": group_id}


def _base_summary(**over):
    summary = {
        "page": {"name": "Claude", "url": "https://status.claude.com",
                 "updated_at": _ago(minutes=2)},
        "status": {"indicator": "minor", "description": "Partially Degraded Service"},
        "components": [
            _component("claude.ai", "degraded_performance"),
            _component("Claude Code", "operational"),
        ],
        "incidents": [],
        "scheduled_maintenances": [],
    }
    summary.update(over)
    return summary


def test_render_header_and_components(frozen_time, no_color):
    out = cs.render(_base_summary())
    assert "Claude status" in out
    assert "Partially Degraded Service" in out
    assert "page updated 2m ago" in out
    assert "claude.ai" in out and "Degraded Performance" in out
    assert "Claude Code" in out and "Operational" in out
    # summary tally line
    assert "1 degraded performance" in out
    assert "1 operational" in out


def test_render_no_incidents(frozen_time, no_color):
    out = cs.render(_base_summary())
    assert "No active incidents." in out


def test_render_with_incident_and_update(frozen_time, no_color):
    summary = _base_summary(incidents=[{
        "name": "Elevated error rate on Claude Opus 4.8",
        "impact": "minor",
        "status": "investigating",
        "started_at": _ago(minutes=11),
        "shortlink": "https://stspg.io/abc123",
        "incident_updates": [
            {"body": "We are currently investigating this issue.",
             "created_at": _ago(minutes=11)},
        ],
    }])
    out = cs.render(summary)
    assert "Active incidents (1)" in out
    assert "MINOR" in out
    assert "Elevated error rate on Claude Opus 4.8" in out
    assert "Investigating" in out
    assert "We are currently investigating this issue." in out
    assert "https://stspg.io/abc123" in out


def test_render_multiple_incidents(frozen_time, no_color):
    summary = _base_summary(incidents=[
        {"name": "Incident A", "impact": "major", "status": "identified",
         "started_at": _ago(hours=2), "incident_updates": []},
        {"name": "Incident B", "impact": "critical", "status": "monitoring",
         "started_at": _ago(days=1), "incident_updates": []},
    ])
    out = cs.render(summary)
    assert "Active incidents (2)" in out
    assert "Incident A" in out and "Incident B" in out
    assert "MAJOR" in out and "CRITICAL" in out


def test_render_truncates_long_incident_body(frozen_time, no_color):
    long_body = "x" * 500
    summary = _base_summary(incidents=[{
        "name": "Big one", "impact": "minor", "status": "investigating",
        "started_at": _ago(minutes=1),
        "incident_updates": [{"body": long_body, "created_at": _ago(minutes=1)}],
    }])
    out = cs.render(summary)
    assert "..." in out
    # The 500-char body must not appear in full anywhere.
    assert long_body not in out


def test_render_collapses_whitespace_in_body(frozen_time, no_color):
    summary = _base_summary(incidents=[{
        "name": "Whitespace", "impact": "minor", "status": "investigating",
        "started_at": _ago(minutes=1),
        "incident_updates": [
            {"body": "line one\n\n   line   two", "created_at": _ago(minutes=1)},
        ],
    }])
    out = cs.render(summary)
    assert "line one line two" in out


def test_render_scheduled_maintenance_shown_and_completed_filtered(frozen_time, no_color):
    summary = _base_summary(scheduled_maintenances=[
        {"name": "DB upgrade", "status": "scheduled",
         "scheduled_for": "2026-06-25T00:00:00Z",
         "scheduled_until": "2026-06-25T02:00:00Z"},
        {"name": "Old finished work", "status": "completed",
         "scheduled_for": "2026-06-01T00:00:00Z",
         "scheduled_until": "2026-06-01T02:00:00Z"},
    ])
    out = cs.render(summary)
    assert "Scheduled maintenance (1)" in out
    assert "DB upgrade" in out
    assert "Old finished work" not in out


def test_render_empty_payload_does_not_crash(no_color, frozen_time):
    out = cs.render({})
    assert "Claude status" in out  # default page name
    assert "No active incidents." in out


def test_render_unknown_status_symbol(frozen_time, no_color):
    summary = _base_summary(components=[_component("Mystery", "teleporting")])
    out = cs.render(summary)
    # Unknown statuses fall back to the hollow marker rather than crashing.
    assert "Mystery" in out
    assert "Teleporting" in out
    assert "○" in out


def test_render_component_groups_nested_under_heading(frozen_time, no_color):
    """Group containers render as a heading with their leaves indented beneath;
    ungrouped leaves stay at the top level. (Tier-2 #3.)"""
    summary = _base_summary(components=[
        _component("API Group", "operational", group=True, id="grp1"),
        _component("Child One", "operational", group_id="grp1"),
        _component("Child Two", "degraded_performance", group_id="grp1"),
        _component("Standalone", "operational"),
    ])
    out = cs.render(summary)
    lines = out.splitlines()
    # Group heading now appears (was dropped before Tier-2 #3).
    assert any("API Group" in line for line in lines)

    gi = next(i for i, line in enumerate(lines) if "API Group" in line)
    c1 = next(i for i, line in enumerate(lines) if "Child One" in line)
    c2 = next(i for i, line in enumerate(lines) if "Child Two" in line)
    standalone = next(i for i, line in enumerate(lines) if "Standalone" in line)

    # Children follow the heading and are indented deeper than top-level leaves.
    assert c1 > gi and c2 > gi
    assert lines[c1].startswith("    ") and lines[c2].startswith("    ")
    assert lines[standalone].startswith("  ") and not lines[standalone].startswith("    ")

    # Tally counts leaves only — the group container is not a service.
    assert "2 operational" in out          # Child One + Standalone
    assert "1 degraded performance" in out  # Child Two


def test_render_empty_group_container_is_skipped(frozen_time, no_color):
    """A group with no leaves shouldn't print a dangling heading."""
    summary = _base_summary(components=[
        _component("Empty Group", "operational", group=True, id="grp9"),
        _component("Real Service", "operational"),
    ])
    out = cs.render(summary)
    assert "Empty Group" not in out
    assert "Real Service" in out


def test_render_orphan_child_falls_back_to_top_level(frozen_time, no_color):
    """A leaf whose group_id matches no group still renders (at top level)
    rather than vanishing."""
    summary = _base_summary(components=[
        _component("Lonely Child", "operational", group_id="missing-grp"),
    ])
    out = cs.render(summary)
    assert "Lonely Child" in out
    assert "1 operational" in out


# ── fetch_summary ─────────────────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_summary_builds_url_and_parses(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["ua"] = req.get_header("User-agent")
        return _FakeResponse(json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(cs.urllib.request, "urlopen", fake_urlopen)
    result = cs.fetch_summary("https://status.claude.com/")  # trailing slash
    assert result == {"ok": True}
    assert captured["url"] == "https://status.claude.com/api/v2/summary.json"
    assert captured["timeout"] == 15.0
    assert "claude-status" in (captured["ua"] or "")


def test_fetch_summary_custom_base(monkeypatch):
    def fake_urlopen(req, timeout=None):
        assert req.full_url == "https://example.statuspage.io/api/v2/summary.json"
        return _FakeResponse(b"{}")

    monkeypatch.setattr(cs.urllib.request, "urlopen", fake_urlopen)
    assert cs.fetch_summary("https://example.statuspage.io") == {}


# ── fetch_summary: bounded retry (Tier-2 #4) ──────────────────────────────────
@pytest.fixture
def no_backoff(monkeypatch):
    """Make retry backoff instant so retry tests don't actually sleep."""
    monkeypatch.setattr(cs.time, "sleep", lambda *_: None)


def test_fetch_summary_retries_then_succeeds(monkeypatch, no_backoff):
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise cs.urllib.error.URLError("temporary blip")
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(cs.urllib.request, "urlopen", flaky)
    assert cs.fetch_summary("https://x", retries=2) == {"ok": True}
    assert calls["n"] == 3  # failed twice, succeeded on the third


def test_fetch_summary_gives_up_after_retries(monkeypatch, no_backoff):
    calls = {"n": 0}

    def always_down(req, timeout=None):
        calls["n"] += 1
        raise cs.urllib.error.URLError("down")

    monkeypatch.setattr(cs.urllib.request, "urlopen", always_down)
    with pytest.raises(cs.urllib.error.URLError):
        cs.fetch_summary("https://x", retries=2)
    assert calls["n"] == 3  # initial attempt + 2 retries


def test_fetch_summary_4xx_fails_fast(monkeypatch, no_backoff):
    calls = {"n": 0}

    def not_found(req, timeout=None):
        calls["n"] += 1
        raise cs.urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)

    monkeypatch.setattr(cs.urllib.request, "urlopen", not_found)
    with pytest.raises(cs.urllib.error.HTTPError):
        cs.fetch_summary("https://x", retries=2)
    assert calls["n"] == 1  # 4xx is not retried


def test_fetch_summary_5xx_is_retried(monkeypatch, no_backoff):
    calls = {"n": 0}

    def server_error(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise cs.urllib.error.HTTPError("https://x", 503, "Unavailable", {}, None)
        return _FakeResponse(b"{}")

    monkeypatch.setattr(cs.urllib.request, "urlopen", server_error)
    assert cs.fetch_summary("https://x", retries=2) == {}
    assert calls["n"] == 2  # one 503, then success


# ── main / CLI ────────────────────────────────────────────────────────────────
def test_main_json_outputs_raw(monkeypatch, capsys):
    payload = {"page": {"name": "X"}, "status": {"indicator": "none"}}
    monkeypatch.setattr(cs, "fetch_summary", lambda *a, **k: payload)
    rc = cs.main(["--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == payload


def test_main_snapshot_renders(monkeypatch, capsys, frozen_time):
    monkeypatch.setattr(cs, "fetch_summary", lambda *a, **k: _base_summary())
    rc = cs.main(["--no-color"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Claude status" in out


def test_main_network_error_returns_1(monkeypatch, capsys):
    def boom(*a, **k):
        raise cs.urllib.error.URLError("no route")

    monkeypatch.setattr(cs, "fetch_summary", boom)
    rc = cs.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not reach" in err


def test_main_bad_json_returns_1(monkeypatch, capsys):
    def boom(*a, **k):
        raise json.JSONDecodeError("bad", "doc", 0)

    monkeypatch.setattr(cs, "fetch_summary", boom)
    rc = cs.main([])
    assert rc == 1
    err = capsys.readouterr().err
    assert "bad JSON" in err


# ── live fixture smoke test ───────────────────────────────────────────────────
def test_render_real_fixture_smoke(no_color):
    """A captured real summary.json must render without raising."""
    fixture = FIXTURES / "summary_live.json"
    if not fixture.exists():
        pytest.skip("no live fixture captured")
    data = json.loads(fixture.read_text())
    out = cs.render(data)
    assert isinstance(out, str) and out.strip()
    assert "status" in out.lower()


def test_fixture_is_valid_summary_shape():
    fixture = FIXTURES / "summary_live.json"
    if not fixture.exists():
        pytest.skip("no live fixture captured")
    data = json.loads(fixture.read_text())
    for key in ("page", "components", "status"):
        assert key in data
