"""Tests for track_build.find_downstream_build — locating the downstream run a
build triggered, across the builds list and the queue.

The load-bearing behaviour is what `--trigger-timeout` bounds (R-052): DISCOVERY
of the downstream run, not its wait for an executor. A build that has been found
sitting in the queue has demonstrably been triggered, so the timeout must not kill
the tracker while it waits — but a queue item that is cancelled must still fail
rather than poll forever.

Run: `python3 -m pytest tools/ai_workflow/test_track_build.py`.
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "track_build", Path(__file__).resolve().parent / "track_build.py"
)
track_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(track_build)

UPSTREAM = "Org/Repo"
UPSTREAM_NR = 7
TRIGGER_TIMEOUT = 120.0
POLL = 5.0


class FakeClock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeJenkins:
    """Serves one scripted (builds, queue items) pair per poll round.

    The last round repeats forever, so a test states only the rounds that differ.
    """

    def __init__(self, rounds):
        self.rounds = rounds
        self.round = -1

    def _current(self):
        return self.rounds[min(self.round, len(self.rounds) - 1)]

    def get_json(self, path, tree=None):
        self.round += 1  # a round begins with the builds fetch
        return {"builds": self._current()[0]}

    def get_queue(self, tree):
        return {"items": self._current()[1]}


def _caused_by(upstream=UPSTREAM, upstream_nr=UPSTREAM_NR):
    return {"actions": [{"causes": [{"upstreamProject": upstream, "upstreamBuild": upstream_nr}]}]}


def _build(number, **cause):
    return {"number": number, **_caused_by(**cause)}


def _queue_item(executable=None, **cause):
    item = dict(_caused_by(**cause))
    if executable is not None:
        item["executable"] = {"number": executable}
    return item


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(track_build.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(track_build.time, "sleep", fake.sleep)
    return fake


@pytest.fixture
def find(clock):
    """Run find_downstream_build against scripted rounds; return (result, log lines)."""

    def _find(rounds):
        lines: list[str] = []
        number = track_build.find_downstream_build(
            FakeJenkins(rounds),
            "job/Org/job/Downstream",
            "Org/Downstream",
            UPSTREAM,
            UPSTREAM_NR,
            TRIGGER_TIMEOUT,
            POLL,
            lines.append,
        )
        return number, lines

    return _find


class TestFound:
    def test_matching_build_is_returned(self, find):
        number, _ = find([([_build(42)], [])])
        assert number == 42

    def test_queue_item_that_has_started_returns_its_build_number(self, find):
        number, _ = find([([], [_queue_item(executable=42)])])
        assert number == 42

    def test_a_build_from_a_different_upstream_is_not_ours(self, find):
        # Same downstream job, someone else's trigger — must not be claimed.
        rounds = [([_build(41, upstream="Org/Other")], []), ([_build(42)], [])]
        number, _ = find(rounds)
        assert number == 42

    def test_a_build_from_a_different_upstream_run_is_not_ours(self, find):
        rounds = [([_build(41, upstream_nr=UPSTREAM_NR - 1)], []), ([_build(42)], [])]
        number, _ = find(rounds)
        assert number == 42


class TestQueueWait:
    def test_pending_queue_item_outlives_the_trigger_timeout(self, find, clock):
        # R-052: 40 rounds x 5s poll = 200s pending, well past the 120s timeout. The
        # item is demonstrably triggered, so the tracker must still be there when it starts.
        rounds = [([], [_queue_item()])] * 40 + [([_build(42)], [])]
        number, _ = find(rounds)
        assert number == 42
        assert clock.now > TRIGGER_TIMEOUT

    def test_pending_is_announced_once(self, find):
        rounds = [([], [_queue_item()])] * 5 + [([_build(42)], [])]
        _, lines = find(rounds)
        assert len(lines) == 1
        assert "pending in queue" in lines[0]

    def test_a_queue_item_for_a_different_upstream_does_not_hold_the_deadline(self, find):
        # Someone else's queued build must not keep our discovery alive indefinitely.
        with pytest.raises(track_build.JenkinsError, match="appeared"):
            find([([], [_queue_item(upstream="Org/Other")])])


class TestGivingUp:
    def test_never_triggered_times_out(self, find, clock):
        with pytest.raises(track_build.JenkinsError, match="no build triggered by"):
            find([([], [])])
        assert clock.now >= TRIGGER_TIMEOUT

    def test_cancelled_queue_item_fails_instead_of_polling_forever(self, find):
        # Queued for a while, then gone without ever starting.
        rounds = [([], [_queue_item()])] * 10 + [([], [])]
        with pytest.raises(track_build.JenkinsError, match="left the queue without starting"):
            find(rounds)
