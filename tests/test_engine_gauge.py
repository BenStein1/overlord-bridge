"""Engine gas gauge + brain preference router.

Regression cover for the 2026-07-14 mistake: a Codex Overlord dispatched a
Claude worker while Claude was out of gas (the very reason Ben was on Codex).
These tests monkeypatch every engine probe so nothing touches the network — they
exercise the routing/guard logic, not the live probes.
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "overlord" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import engine_gauge as eg  # noqa: E402
import overlord_mcp as om  # noqa: E402


def set_gauge(monkeypatch, **states):
    """Install a fake gauge. Each kwarg is engine -> (usable, state[, gas])."""
    def make(name, spec):
        usable, state = spec[0], spec[1]
        gas = spec[2] if len(spec) > 2 else None
        return eg._entry(name, usable=usable, gas=gas, state=state, reason=f"{name} {state}")
    probes = {}
    for name in eg.ENGINES:
        spec = states.get(name.replace("-", "_"), states.get(name))
        if spec is None:
            spec = (True, "ready")
        probes[name] = (lambda n=name, s=spec: make(n, s))
    monkeypatch.setattr(eg, "_PROBES", probes)
    monkeypatch.setattr(om, "_gauge_module", lambda: eg)


# ------------------------------------------------------------------ gauge probes
def test_gauge_shape(monkeypatch):
    set_gauge(monkeypatch, claude=(True, "ready", 40))
    g = eg.engine_gas_gauge()
    assert set(g["engines"]) == set(eg.ENGINES)
    assert g["engines"]["claude"]["gas"] == 40
    assert "checked_at" in g


def test_claude_out_of_gas_probe():
    entry = eg._entry("claude", usable=False, gas=2, state="out_of_gas", reason="2% left")
    assert entry["usable"] is False and entry["state"] == "out_of_gas"


# ------------------------------------------------------------------ router basics
def test_default_medium_work_prefers_free_nvidia(monkeypatch):
    set_gauge(monkeypatch)  # everything ready
    r = eg.recommend_engine()
    assert r["decision"] == "dispatch_now"
    assert r["recommended"] == {"brain": "nvidia", "model": "gpt-oss"}


def test_high_stakes_prefers_claude(monkeypatch):
    set_gauge(monkeypatch)
    r = eg.recommend_engine(stakes="high", multi_file=True)
    assert r["decision"] == "dispatch_now"
    assert r["recommended"]["brain"] == "claude"


def test_small_chore_floats_nvidia_and_picks_model(monkeypatch):
    set_gauge(monkeypatch)
    r = eg.recommend_engine(small_chore=True)
    assert r["decision"] == "dispatch_now"
    assert r["recommended"]["brain"] == "nvidia"
    assert r["recommended"]["model"] == "gpt-oss"


def test_writing_heavy_model_avoids_kimi(monkeypatch):
    set_gauge(monkeypatch)
    r = eg.recommend_engine(small_chore=True, writing_heavy=True)
    assert r["recommended"] == {"brain": "nvidia", "model": "mistral"}


def test_nvidia_probe_reports_model_level_gas(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERLORD_WORKER_NVIDIA_API_KEY", "fake-key")
    monkeypatch.setattr(eg, "_EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(eg, "NVIDIA_CATALOG_CACHE", tmp_path / "nvidia_models.json")
    monkeypatch.setattr(
        eg,
        "_curl_json",
        lambda *a, **kw: ({
            "data": [
                {"id": "deepseek-ai/deepseek-v4-pro"},
                {"id": "qwen/qwen3-next-80b-a3b-instruct"},
                {"id": "z-ai/glm5.1"},
            ]
        }, None),
    )

    entry = eg._probe_nvidia()

    assert entry["state"] == "ready"
    assert entry["gas"] == 100
    assert entry["detail"]["free_models_url"].endswith("orderBy=weightPopular%3ADESC")
    assert entry["detail"]["model_scope"] == "bounded popular-family watchlist, not full catalog output"
    assert entry["detail"]["active_probe"] == "not run during normal gas; use worker smoke/report history"
    models = {m["id"]: m for m in entry["detail"]["models"]}
    assert "z-ai/glm5.1" not in models
    assert models["deepseek-ai/deepseek-v4-pro"]["auto_candidate"] is False
    assert models["deepseek-ai/deepseek-v4-pro"]["gas"] == 100


def test_nvidia_recent_kimi_404_is_model_broken_not_whole_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERLORD_WORKER_NVIDIA_API_KEY", "fake-key")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(eg, "_EVENTS_PATH", events)
    monkeypatch.setattr(eg, "NVIDIA_CATALOG_CACHE", tmp_path / "nvidia_models.json")
    events.write_text(json.dumps({
        "brain": "nvidia",
        "status": "crashed",
        "name": "DataNvidiaSmoke",
        "effective_local_agent_model": "moonshotai/kimi-k2.6",
        "result_tail": "HTTP Error 404: Not Found",
        "finished_at": eg._now().isoformat().replace("+00:00", "Z"),
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        eg,
        "_curl_json",
        lambda *a, **kw: ({
            "data": [
                {"id": "moonshotai/kimi-k2.6"},
                {"id": "qwen/qwen3-next-80b-a3b-instruct"},
                {"id": "deepseek-ai/deepseek-v4-pro"},
            ]
        }, None),
    )
    nvidia = eg._probe_nvidia()
    monkeypatch.setattr(eg, "engine_gas_gauge", lambda: {
        "checked_at": eg._now().isoformat().replace("+00:00", "Z"),
        "engines": {
            "nvidia": nvidia,
            "claude": eg._entry("claude", usable=True, gas=50, state="ready", reason="ok"),
            "codex": eg._entry(
                "codex",
                usable=True,
                gas=50,
                weekly_gas=80,
                state="ready",
                reason="ok",
                detail={"plan_type": "plus"},
            ),
            "local-agent": eg._entry("local-agent", usable=True, gas=None, state="ready", reason="ok"),
        },
    })

    models = {m["id"]: m for m in nvidia["detail"]["models"]}
    assert nvidia["state"] == "ready"
    assert models["moonshotai/kimi-k2.6"]["state"] == "broken"
    assert models["moonshotai/kimi-k2.6"]["gas"] == 0
    r = eg.recommend_engine(small_chore=True, writing_heavy=True)
    assert r["recommended"] == {"brain": "nvidia", "model": "qwen3-next"}


def test_nvidia_probe_adds_recent_model_test_time(monkeypatch, tmp_path):
    monkeypatch.setenv("OVERLORD_WORKER_NVIDIA_API_KEY", "fake-key")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(eg, "_EVENTS_PATH", events)
    monkeypatch.setattr(eg, "NVIDIA_CATALOG_CACHE", tmp_path / "nvidia_models.json")
    events.write_text(json.dumps({
        "brain": "nvidia",
        "status": "finished",
        "name": "NvidiaLimerickQwen",
        "model": "qwen3-next",
        "effective_local_agent_model": "qwen/qwen3-next-80b-a3b-instruct",
        "duration_seconds": 501.019,
        "finished_at": eg._now().isoformat().replace("+00:00", "Z"),
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        eg,
        "_curl_json",
        lambda *a, **kw: ({
            "data": [
                {"id": "qwen/qwen3-next-80b-a3b-instruct"},
                {"id": "deepseek-ai/deepseek-v4-pro"},
            ]
        }, None),
    )

    nvidia = eg._probe_nvidia()

    models = {m["id"]: m for m in nvidia["detail"]["models"]}
    qwen = models["qwen/qwen3-next-80b-a3b-instruct"]
    assert qwen["test_seconds"] == 501.019
    assert qwen["test_status"] == "finished"
    assert qwen["test_worker"] == "NvidiaLimerickQwen"
    assert qwen["slow"] is True


def test_local_probe_adds_recent_test_time(monkeypatch, tmp_path):
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(eg, "_EVENTS_PATH", events)
    events.write_text(json.dumps({
        "brain": "local-agent",
        "status": "finished",
        "name": "LocalCleanLimerick",
        "model": "qwen3-coder-next",
        "effective_local_agent_model": "openai/Qwen3-Coder-Next",
        "duration_seconds": 37.254,
        "finished_at": eg._now().isoformat().replace("+00:00", "Z"),
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(eg, "_reachable", lambda *a, **kw: True)

    local = eg._probe_local_agent()

    assert local["state"] == "ready"
    assert local["detail"]["test_seconds"] == 37.254
    assert local["detail"]["test_model"] == "qwen3-coder-next"
    assert local["detail"]["slow"] is False


def test_gauge_cli_prioritizes_caps_and_model_rows():
    gauge = {
        "checked_at": "2026-07-16T00:00:00Z",
        "engines": {
            "claude": eg._entry("claude", usable=True, gas=50, state="ready", reason="ok"),
            "nvidia": eg._entry(
                "nvidia",
                usable=True,
                gas=100,
                state="ready",
                reason="ok",
                detail={
                    "models": [
                        {
                            "alias": "qwen3-next",
                            "id": "qwen/qwen3-next-80b-a3b-instruct",
                            "auto_candidate": True,
                            "gas": 100,
                            "state": "ready",
                            "test_seconds": 501.019,
                            "slow": True,
                        },
                        {
                            "alias": "kimi",
                            "id": "moonshotai/kimi-k2.6",
                            "auto_candidate": False,
                            "gas": 0,
                            "state": "broken",
                            "reason": "recent worker call returned not-found/404",
                        },
                    ]
                },
            ),
            "codex": eg._entry(
                "codex",
                usable=True,
                gas=50,
                weekly_gas=80,
                state="ready",
                reason="ok",
                detail={"plan_type": "plus"},
            ),
            "local-agent": eg._entry(
                "local-agent",
                usable=True,
                gas=None,
                state="ready",
                reason="ok",
                detail={"test_seconds": 37.254, "slow": False},
            ),
        },
    }

    out = eg.format_gauge_cli(gauge)

    assert out.startswith("Engine gas gauge")
    assert out.index("Cloud caps:") < out.index("Local:")
    assert out.index("Local:") < out.index("NVIDIA models:")
    assert out.index("  codex") < out.index("  nvidia")
    assert out.index("  local") < out.index("  nvidia")
    assert "plan plus" in out
    assert "5h 50% used" in out
    assert "wk 20% used" in out
    assert "5h reset:" in out
    assert "wk reset:" in out
    assert "qwen3-next" in out and "8m21s" in out and "slow" in out
    assert "kimi" in out and "broken" in out and "not-found/404" in out
    assert "local   ✅ UP" in out
    assert "test 37s" in out
    assert "manual-only" not in out
    assert "gas 100%" not in out


def test_mcp_gas_dashboard_returns_plain_text(monkeypatch):
    gauge = {
        "checked_at": "2026-07-16T00:00:00Z",
        "engines": {
            "claude": eg._entry("claude", usable=False, gas=0, state="out_of_gas", reason="cap"),
            "codex": eg._entry(
                "codex",
                usable=True,
                gas=None,
                weekly_gas=56,
                state="ready",
                reason="ok",
                detail={"plan_type": "plus"},
            ),
            "local-agent": eg._entry("local-agent", usable=True, gas=None, state="ready", reason="ok"),
            "nvidia": eg._entry("nvidia", usable=True, gas=100, state="ready", reason="ok"),
        },
    }
    monkeypatch.setattr(om, "_gauge_module", lambda: eg)
    monkeypatch.setattr(eg, "engine_gas_gauge", lambda: gauge)

    result = om._call_tool("engine_gas_dashboard", {})
    text = result["content"][0]["text"]

    assert result["isError"] is False
    assert text.startswith("Engine gas gauge")
    assert "Cloud caps:" in text
    assert "  claude  ❌ OUT" in text
    assert "  codex" in text
    assert not text.lstrip().startswith("{")


def test_nvidia_models_catalog_is_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(eg, "NVIDIA_CATALOG_CACHE", tmp_path / "nvidia_models.json")
    calls = []
    monkeypatch.setattr(
        eg,
        "_curl_json",
        lambda *a, **kw: calls.append(1) or ({"data": [{"id": "deepseek-ai/deepseek-v4-pro"}]}, None),
    )

    first, first_err, first_source = eg._nvidia_models_payload(
        "https://integrate.api.nvidia.com/v1/models", headers={}
    )
    second, second_err, second_source = eg._nvidia_models_payload(
        "https://integrate.api.nvidia.com/v1/models", headers={}
    )

    assert first_err is None and second_err is None
    assert first == second
    assert first_source == "live"
    assert second_source == "cache"
    assert len(calls) == 1


def test_review_model_is_gpt_oss(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0), codex=(False, "offline"))
    r = eg.recommend_engine(review_or_analysis=True, small_chore=True)
    assert r["recommended"]["brain"] == "nvidia"
    assert r["recommended"]["model"] == "gpt-oss"


def test_qwen3_next_is_preferred_over_qwen35(monkeypatch):
    nvidia = eg._entry("nvidia", usable=True, gas=100, state="ready", reason="ok", detail={
        "models": [
            {"id": "qwen/qwen3-next-80b-a3b-instruct", "state": "ready"},
            {"id": "qwen/qwen3.5-122b-a10b", "state": "ready"},
        ]
    })
    monkeypatch.setattr(eg, "engine_gas_gauge", lambda: {
        "checked_at": eg._now().isoformat().replace("+00:00", "Z"),
        "engines": {
            "nvidia": nvidia,
            "claude": eg._entry("claude", usable=True, gas=50, state="ready", reason="ok"),
            "codex": eg._entry("codex", usable=True, gas=50, state="ready", reason="ok"),
            "local-agent": eg._entry("local-agent", usable=True, gas=None, state="ready", reason="ok"),
        },
    })

    r = eg.recommend_engine(small_chore=True)

    assert r["recommended"] == {"brain": "nvidia", "model": "qwen3-next"}


def test_must_stay_local_pins_local(monkeypatch):
    set_gauge(monkeypatch)
    r = eg.recommend_engine(must_stay_local=True)
    assert r["recommended"]["brain"] == "local-agent"
    assert all(e["engine"] == "local-agent" for e in r["ranking"])


def test_needs_resume_restricts_to_claude_codex(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0))
    r = eg.recommend_engine(needs_resume=True)
    assert r["recommended"]["brain"] == "codex"
    assert {e["engine"] for e in r["ranking"]} == {"claude", "codex"}


# ------------------------------------------------------------------ 2026-07-14 regression
def test_claude_out_of_gas_falls_through_and_names_the_skip(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 1))
    r = eg.recommend_engine(stakes="high", needs_judgment=True)
    assert r["recommended"]["brain"] == "codex"
    assert any(s["engine"] == "claude" for s in r["skipped"])
    assert "skipped claude:" in r["rationale"]


def test_dispatch_blocks_out_of_gas_claude(monkeypatch, tmp_path):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 1))
    monkeypatch.setattr(om, "DISPATCH_DIR", tmp_path)
    r = om.dispatch_overlord_worker(
        "T", str(Path.home() / "Projects" / "overlord-bridge"), "echo", brain="claude")
    assert r["ok"] is False and r["blocked"] is True
    assert r["recommended"]["brain"] == "nvidia"
    assert not list(tmp_path.glob("*.json"))  # nothing written


def test_dispatch_force_overrides_and_records_routing(monkeypatch, tmp_path):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 1))
    monkeypatch.setattr(om, "DISPATCH_DIR", tmp_path)
    r = om.dispatch_overlord_worker(
        "T", str(Path.home() / "Projects" / "overlord-bridge"), "echo",
        brain="claude", force=True)
    assert r["ok"] is True
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["routing"]["forced"] is True
    assert payload["routing"]["brain"] == "claude"


def test_dispatch_usable_brain_writes_normally(monkeypatch, tmp_path):
    set_gauge(monkeypatch)  # all ready
    monkeypatch.setattr(om, "DISPATCH_DIR", tmp_path)
    r = om.dispatch_overlord_worker(
        "T", str(Path.home() / "Projects" / "overlord-bridge"), "echo", brain="nvidia")
    assert r["ok"] is True
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert r["routing"]["brain"] == "nvidia"


def test_auto_route_dispatches_recommended_nvidia(monkeypatch, tmp_path):
    set_gauge(monkeypatch)
    monkeypatch.setattr(om, "DISPATCH_DIR", tmp_path / "dispatch")
    r = om.dispatch_overlord_worker(
        "T", str(tmp_path / "project"), "write a small script",
        brain="auto", small_chore=True,
    )
    assert r["ok"] is True
    assert r["decision"] == "dispatch_now"
    assert r["payload"]["brain"] == "nvidia"
    assert r["payload"]["model"] == "gpt-oss"


def test_auto_route_defer_schedules_resume_without_dispatch(monkeypatch, tmp_path):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0), codex=(False, "offline"),
              nvidia=(True, "ready"), local_agent=(True, "ready"))
    monkeypatch.setattr(om, "DISPATCH_DIR", tmp_path / "dispatch")
    calls = []
    monkeypatch.setattr(
        om,
        "schedule_resume_after_cap",
        lambda **kw: calls.append(kw) or {"ok": True, "unit": "resume.timer"},
    )
    r = om.dispatch_overlord_worker(
        "T", str(tmp_path / "project"), "hard ambiguous thing",
        brain="auto", stakes="high", ambiguous=True,
    )
    assert r["ok"] is True
    assert r["scheduled"] is True
    assert r["dispatched"] is False
    assert r["decision"] == "defer_until_reset"
    assert calls[0]["brain"] == "claude"
    assert not (tmp_path / "dispatch").exists()


def test_auto_route_reserve_claude_writes_handoff_and_dispatches(monkeypatch, tmp_path):
    set_gauge(monkeypatch, claude=(True, "ready", 15), codex=(True, "ready"))
    monkeypatch.setattr(om, "DISPATCH_DIR", tmp_path / "dispatch")
    monkeypatch.setattr(
        om,
        "schedule_resume_after_cap",
        lambda **kw: {"ok": True, "unit": "resume.timer", "template": "resume.json"},
    )
    project = tmp_path / "project"
    r = om.dispatch_overlord_worker(
        "T", str(project), "hard ambiguous thing",
        brain="auto", stakes="high", ambiguous=True,
    )
    assert r["ok"] is True
    assert r["decision"] == "dispatch_with_handoff"
    assert r["payload"]["brain"] == "claude"
    assert (project / "HANDOFF.md").exists()
    assert r["routing"]["handoff"]["ok"] is True
    assert r["routing"]["scheduled_resume"]["ok"] is True


# ------------------------------------------------------------------ codex tiering
def test_codex_ready_is_a_fallback_when_cloud_down(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0), nvidia=(False, "offline"),
              codex=(True, "ready"))
    r = eg.recommend_engine()
    assert r["recommended"]["brain"] == "codex"


def test_codex_offline_is_never_recommended(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0), nvidia=(False, "offline"),
              codex=(False, "offline"))
    r = eg.recommend_engine()
    assert r["decision"] == "defer_until_reset"
    assert r["recommended"] is None


def test_no_usable_engine_returns_none_with_rationale(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "offline", None), nvidia=(False, "offline"),
              codex=(False, "offline"), local_agent=(False, "offline"))
    r = eg.recommend_engine()
    assert r["recommended"] is None
    assert r["decision"] == "blocked"
    assert "No dispatch-ready engine" in r["rationale"]


def test_complex_job_uses_reserve_claude_with_handoff(monkeypatch):
    set_gauge(monkeypatch, claude=(True, "ready", 15), codex=(True, "ready"))
    r = eg.recommend_engine(stakes="high", ambiguous=True)
    assert r["decision"] == "dispatch_with_handoff"
    assert r["recommended"]["brain"] == "claude"
    assert r["handoff"]["required"] is True


def test_complex_job_uses_codex_when_claude_capped(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0), codex=(True, "ready"),
              nvidia=(True, "ready"), local_agent=(True, "ready"))
    r = eg.recommend_engine(stakes="high", ambiguous=True)
    assert r["decision"] == "dispatch_now"
    assert r["recommended"]["brain"] == "codex"


def test_complex_job_defers_instead_of_overreaching_nvidia_or_local(monkeypatch):
    set_gauge(monkeypatch, claude=(False, "out_of_gas", 0), codex=(False, "offline"),
              nvidia=(True, "ready"), local_agent=(True, "ready"))
    r = eg.recommend_engine(stakes="high", ambiguous=True)
    assert r["decision"] == "defer_until_reset"
    assert r["recommended"] is None
    assert all(e["engine"] not in {"nvidia", "local-agent"} for e in r["ranking"])


def test_medium_vague_architecture_stays_on_claude_or_codex(monkeypatch):
    set_gauge(monkeypatch, claude=(True, "ready", 80), codex=(True, "ready"),
              nvidia=(True, "ready"))
    r = eg.recommend_engine(stakes="medium", needs_judgment=True)
    assert r["recommended"]["brain"] == "claude"


def test_large_single_file_narrow_task_can_use_local_last_resort(monkeypatch):
    set_gauge(monkeypatch, nvidia=(False, "offline"), codex=(False, "offline"),
              local_agent=(True, "ready"), claude=(True, "ready", 10))
    r = eg.recommend_engine(context_size="large")
    assert r["recommended"] == {"brain": "local-agent", "model": "qwen3-coder-next"}


def test_basic_task_uses_local_when_cloud_unavailable_and_claude_reserve(monkeypatch):
    set_gauge(monkeypatch, nvidia=(False, "offline"), codex=(False, "offline"),
              local_agent=(True, "ready"), claude=(True, "ready", 10))
    r = eg.recommend_engine(small_chore=True)
    assert r["recommended"]["brain"] == "local-agent"


def test_degraded_and_cold_states_are_not_selected(monkeypatch):
    set_gauge(monkeypatch, nvidia=(True, "degraded"), codex=(True, "degraded"),
              local_agent=(True, "cold"), claude=(True, "ready", 10))
    r = eg.recommend_engine(small_chore=True)
    assert r["decision"] == "blocked"
    assert r["recommended"] is None


def test_codex_disabled_env_marks_unavailable(monkeypatch):
    monkeypatch.setenv("OVERLORD_ENGINE_CODEX_DISABLED", "1")
    entry = eg._probe_codex()
    assert entry["usable"] is False and entry["state"] == "unavailable"


# ------------------------------------------------------------------ _codex_usage
def _write_rollout(path, rate_limits, *, extra_lines=0):
    """Write a rollout-*.jsonl with a rate_limits line last, matching the real
    shape: {"payload": {"type": "token_count", "rate_limits": {...}}}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"timestamp": "2026-07-15T00:00:00Z", "type": "event_msg",
                          "payload": {"type": "task_started"}})] * extra_lines
    lines.append(json.dumps({
        "timestamp": "2026-07-15T17:08:30Z", "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": rate_limits},
    }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _isolate_codex_usage(monkeypatch, tmp_path, sessions_dir):
    monkeypatch.setattr(eg, "CODEX_SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(eg, "CODEX_USAGE_CACHE", tmp_path / "cache" / "codex_usage.json")


def test_codex_usage_fresh_window(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    now = eg._now().timestamp()
    _write_rollout(sessions / "2026" / "07" / "15" / "rollout-a.jsonl", {
        "plan_type": "plus",
        "primary": {"used_percent": 7.0, "window_minutes": 300, "resets_at": now + 3600},
        "secondary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": now + 86400},
    })
    usage = eg._codex_usage()
    assert usage["session"] == {"used": 7.0, "gas": 93, "resets_at": now + 3600, "reset_passed": False}
    assert usage["weekly"] == {"used": 12.0, "gas": 88, "resets_at": now + 86400, "reset_passed": False}
    assert usage["plan_type"] == "plus"


def test_codex_usage_weekly_only_primary(monkeypatch, tmp_path):
    """As of 2026-07-13 Codex reports only the weekly window, as `primary`,
    with `secondary: null` -- windows must be matched by window_minutes, not
    by primary/secondary position."""
    sessions = tmp_path / "sessions"
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    now = eg._now().timestamp()
    _write_rollout(sessions / "2026" / "07" / "15" / "rollout-a.jsonl", {
        "primary": {"used_percent": 2.0, "window_minutes": 10080, "resets_at": now + 86400},
        "secondary": None,
    })
    usage = eg._codex_usage()
    assert usage["session"] is None
    assert usage["weekly"]["used"] == 2.0


def test_codex_usage_rolled_over_window_reads_zero(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    now = eg._now().timestamp()
    _write_rollout(sessions / "2026" / "07" / "15" / "rollout-a.jsonl", {
        "primary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": now - 60},
        "secondary": {"used_percent": 50.0, "window_minutes": 10080, "resets_at": now + 86400},
    })
    usage = eg._codex_usage()
    assert usage["session"]["used"] == 0.0
    assert usage["session"]["gas"] == 100
    assert usage["session"]["reset_passed"] is True


def test_codex_usage_missing_log_returns_none(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"  # doesn't exist -> rglob finds nothing
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    assert eg._codex_usage() is None


def test_codex_usage_is_cached(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    now = eg._now().timestamp()
    rollout = sessions / "2026" / "07" / "15" / "rollout-a.jsonl"
    _write_rollout(rollout, {
        "primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": now + 3600},
        "secondary": None,
    })
    first = eg._codex_usage()
    assert first["session"]["used"] == 1.0
    # Change the underlying data without touching the cache -- a call inside
    # the TTL window must keep returning the cached value.
    _write_rollout(rollout, {
        "primary": {"used_percent": 50.0, "window_minutes": 300, "resets_at": now + 3600},
        "secondary": None,
    })
    second = eg._codex_usage()
    assert second["session"]["used"] == 1.0


def test_probe_codex_falls_back_to_reachable_when_no_usage_log(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    monkeypatch.setattr(eg, "CODEX_AUTH", tmp_path / "auth.json")
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "x"}}))
    monkeypatch.setattr(eg, "_reachable", lambda *a, **k: True)
    monkeypatch.setattr(eg, "_recent_rate_limit_hits", lambda brain: 0)
    entry = eg._probe_codex()
    assert entry["usable"] is True and entry["state"] == "ready"
    assert entry["gas"] is None and entry["weekly_gas"] is None
    assert "no usage log yet" in entry["reason"]


def test_probe_codex_reports_weekly_when_session_window_absent(monkeypatch, tmp_path):
    sessions = tmp_path / "sessions"
    _isolate_codex_usage(monkeypatch, tmp_path, sessions)
    now = eg._now().timestamp()
    _write_rollout(sessions / "2026" / "07" / "15" / "rollout-a.jsonl", {
        "primary": {"used_percent": 2.0, "window_minutes": 10080, "resets_at": now + 86400},
        "secondary": None,
    })
    monkeypatch.setattr(eg, "CODEX_AUTH", tmp_path / "auth.json")
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "x"}}))
    monkeypatch.setattr(eg, "_reachable", lambda *a, **k: True)
    entry = eg._probe_codex()
    assert entry["gas"] is None
    assert entry["weekly_gas"] == 98
    assert entry["state"] == "ready"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
