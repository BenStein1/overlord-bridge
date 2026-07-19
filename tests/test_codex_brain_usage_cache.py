import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import modules.codex_brain as cb  # noqa: E402


def test_codex_brain_records_rate_limits_cache(monkeypatch, tmp_path):
    cache = tmp_path / "codex_usage.json"
    monkeypatch.setattr(cb, "CODEX_USAGE_CACHE", cache)

    cb._record_codex_usage_cache_from_message(
        {
            "method": "event_msg",
            "params": {
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "plan_type": "plus",
                        "primary": {
                            "used_percent": 12.0,
                            "window_minutes": 300,
                            "resets_at": 9999992000,
                        },
                        "secondary": {
                            "used_percent": 34.0,
                            "window_minutes": 10080,
                            "resets_at": 9999993000,
                        },
                    },
                }
            },
        }
    )

    usage = json.loads(cache.read_text(encoding="utf-8"))
    assert usage["session"]["used"] == 12.0
    assert usage["session"]["gas"] == 88
    assert usage["weekly"]["used"] == 34.0
    assert usage["weekly"]["gas"] == 66
    assert usage["plan_type"] == "plus"
    assert usage["source"] == "codex_app_server"


def test_codex_brain_records_weekly_only_cache(monkeypatch, tmp_path):
    cache = tmp_path / "codex_usage.json"
    monkeypatch.setattr(cb, "CODEX_USAGE_CACHE", cache)

    cb._record_codex_usage_cache_from_message(
        {
            "payload": {
                "rate_limits": {
                    "primary": {
                        "used_percent": 6.0,
                        "window_minutes": 10080,
                        "resets_at": 9999993000,
                    },
                    "secondary": None,
                }
            }
        }
    )

    usage = json.loads(cache.read_text(encoding="utf-8"))
    assert usage["session"] is None
    assert usage["weekly"]["used"] == 6.0
