"""Behavioral coverage for the near-cap reset-request flag honored in
ClaudeBrain.query() (modules/claude_brain.py).

Contract: ~/.claude/hooks/near-limit-handoff.sh drops
``.reset-requested.claude`` next to the session pin when Ben's usage is near
cap. The bridge -- not the hook -- must consume it at the top of the next
turn, because query() re-pins the session on every ResultMessage, which would
silently overwrite a hook-side delete of the pin seconds later.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

from modules import shared_job  # noqa: E402
from modules.claude_brain import ClaudeBrain  # noqa: E402


class FakeClient:
    """Stands in for ClaudeSDKClient: no real subprocess, no real session."""

    def __init__(self, new_session_id: str):
        self.new_session_id = new_session_id
        self.queried_text: str | None = None
        self.disconnected = False

    async def query(self, text: str) -> None:
        self.queried_text = text

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="hi")], model="claude")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self.new_session_id,
            result="hi",
        )

    async def disconnect(self) -> None:
        self.disconnected = True


def make_brain(tmp_path: Path) -> ClaudeBrain:
    return ClaudeBrain(
        gate=None,  # not exercised: fake client never calls can_use_tool
        cwd=str(tmp_path),
        model="test-model",
        session_file=tmp_path / ".session.claude",
    )


async def drain(brain: ClaudeBrain, text: str) -> list[str]:
    return [chunk async for chunk in brain.query(text)]


def reset_flag_for(brain: ClaudeBrain) -> Path:
    return brain.session_file.parent / f".reset-requested{brain.session_file.suffix}"


def test_reset_flag_with_no_live_job_wipes_pin_and_starts_fresh(tmp_path, monkeypatch):
    asyncio.run(_reset_flag_with_no_live_job_wipes_pin_and_starts_fresh(tmp_path, monkeypatch))


async def _reset_flag_with_no_live_job_wipes_pin_and_starts_fresh(tmp_path, monkeypatch):
    brain = make_brain(tmp_path)
    brain.session_file.write_text("old-sid")
    reset_flag_for(brain).write_text("")

    monkeypatch.setattr(shared_job, "live_job", lambda session_file: None)

    opened_with: list[str | None] = []
    fake_client = FakeClient(new_session_id="new-sid")

    async def fake_open(self, resume):
        opened_with.append(resume)
        return fake_client

    monkeypatch.setattr(ClaudeBrain, "_open", fake_open)

    chunks = await drain(brain, "hello")

    assert not reset_flag_for(brain).exists(), "flag must be consumed"
    assert opened_with == [None], "reset must wipe the pin before the resume is read"
    assert brain.session_file.read_text().strip() == "new-sid"
    assert brain.session_id == "new-sid"
    assert chunks == ["hi"]
    assert fake_client.disconnected


def test_reset_flag_with_live_shared_job_is_skipped_but_consumed(tmp_path, monkeypatch):
    asyncio.run(_reset_flag_with_live_shared_job_is_skipped_but_consumed(tmp_path, monkeypatch))


async def _reset_flag_with_live_shared_job_is_skipped_but_consumed(tmp_path, monkeypatch):
    brain = make_brain(tmp_path)
    brain.session_file.write_text("live-sid")
    reset_flag_for(brain).write_text("")

    job = shared_job.SharedJob(job_id="42", session_id="live-sid", cwd=str(tmp_path))
    monkeypatch.setattr(shared_job, "live_job", lambda session_file: job)
    monkeypatch.setattr(shared_job, "inject", lambda job, text: "reply from shared job")

    def fail_open(self, resume):
        raise AssertionError("must not open a new SDK client while a shared job is live")

    monkeypatch.setattr(ClaudeBrain, "_open", fail_open)

    chunks = await drain(brain, "hello")

    assert not reset_flag_for(brain).exists(), "flag must still be consumed"
    assert brain.session_file.read_text().strip() == "live-sid", "pin must survive untouched"
    assert chunks == ["reply from shared job"]


def test_no_flag_behaves_exactly_as_before(tmp_path, monkeypatch):
    asyncio.run(_no_flag_behaves_exactly_as_before(tmp_path, monkeypatch))


async def _no_flag_behaves_exactly_as_before(tmp_path, monkeypatch):
    brain = make_brain(tmp_path)
    brain.session_file.write_text("old-sid")
    # No reset flag written for this test.

    monkeypatch.setattr(shared_job, "live_job", lambda session_file: None)

    opened_with: list[str | None] = []
    fake_client = FakeClient(new_session_id="old-sid")

    async def fake_open(self, resume):
        opened_with.append(resume)
        return fake_client

    monkeypatch.setattr(ClaudeBrain, "_open", fake_open)

    chunks = await drain(brain, "hello")

    assert opened_with == ["old-sid"], "unchanged pin must be resumed, not reset"
    assert brain.session_file.read_text().strip() == "old-sid"
    assert chunks == ["hi"]
