"""A worker killed by a signal is not a worker that failed.

Regression cover for the 2026-07-11 false alarm: worker "Chorus" restarted
overlord-bridge.service to apply its own fix, systemd SIGTERMed its whole cgroup
(including Chorus), and the bridge pushed Ben a scary "⚠️ failed" on Telegram —
even though the work was already committed and the fix was live.
"""

import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.workers import (  # noqa: E402
    _compact_llamacpp_slots,
    _is_answer_only_worker_task,
    _llamacpp_slots_url,
    _llamacpp_slots_indicate_idle_stall,
    classify_worker_exit,
)


def test_clean_exit_is_finished():
    v = classify_worker_exit(0, shutting_down=False)
    assert v["status"] == "finished"
    assert v["failed"] is False


def test_nonzero_exit_is_a_real_failure():
    v = classify_worker_exit(1, shutting_down=False)
    assert v["status"] == "failed"
    assert v["failed"] is True


def test_sigterm_during_bridge_restart_is_interrupted_not_failed():
    # the Chorus case: worker restarts the bridge, bridge SIGTERMs its own cgroup
    v = classify_worker_exit(-int(signal.SIGTERM), shutting_down=True)
    assert v["status"] == "interrupted"
    assert v["failed"] is False
    assert v["signal"] == "SIGTERM"
    assert "restarting" in v["cause"]


def test_sigterm_without_restart_is_interrupted_but_blamed_externally():
    # e.g. the unexplained SIGTERM that killed worker Barker mid-run
    v = classify_worker_exit(-int(signal.SIGTERM), shutting_down=False)
    assert v["status"] == "interrupted"
    assert v["failed"] is False
    assert v["signal"] == "SIGTERM"
    assert "external" in v["cause"]


def test_sigkill_is_also_interrupted():
    v = classify_worker_exit(-int(signal.SIGKILL), shutting_down=False)
    assert v["status"] == "interrupted"
    assert v["failed"] is False
    assert v["signal"] == "SIGKILL"


def test_unknown_signal_number_does_not_crash():
    v = classify_worker_exit(-99, shutting_down=False)
    assert v["status"] == "interrupted"
    assert v["failed"] is False
    assert "99" in v["signal"]


def test_none_returncode_is_not_silently_a_success():
    # a process we never got a code for must not be reported as finished
    v = classify_worker_exit(None, shutting_down=False)
    assert v["status"] == "failed"


def test_llamacpp_idle_slot_after_decode_is_backend_stall():
    slots = [
        {"id": 0, "n_ctx": 256000, "is_processing": False},
        {
            "id": 3,
            "is_processing": False,
            "id_task": 36,
            "n_prompt_tokens": 5817,
            "n_prompt_tokens_processed": 4935,
            "n_decoded": 54,
            "n_remain": 4042,
            "next_token": [{"has_next_token": False}],
        },
    ]

    assert _llamacpp_slots_indicate_idle_stall(slots) is True
    compact = _compact_llamacpp_slots(slots)
    assert compact[1]["id_task"] == 36
    assert compact[1]["has_next_token"] is False


def test_llamacpp_processing_slot_is_not_stalled():
    slots = [
        {
            "id": 3,
            "is_processing": True,
            "id_task": 36,
            "n_prompt_tokens_processed": 4935,
            "n_decoded": 54,
        },
    ]

    assert _llamacpp_slots_indicate_idle_stall(slots) is False


def test_llamacpp_slots_url_uses_server_root_not_openai_v1_path():
    assert _llamacpp_slots_url("http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/slots"
    assert _llamacpp_slots_url("http://127.0.0.1:1234") == "http://127.0.0.1:1234/slots"


def test_creative_smoke_prompt_is_answer_only():
    assert _is_answer_only_worker_task("Write me a dirty limerick like Data from Star Trek.", []) is True
    assert _is_answer_only_worker_task("Answer-only smoke test. Write a joke.", []) is True
    assert _is_answer_only_worker_task("Write a limerick into limerick.txt", []) is False
    assert _is_answer_only_worker_task("Fix the repo test for poem generation", []) is False
    assert _is_answer_only_worker_task("Write me a dirty limerick", ["README.md"]) is False
