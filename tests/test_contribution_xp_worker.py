import asyncio

from api import worker
from api.services import tracking_xp


def test_replay_is_detached_bounded_nonoverlapping_and_resumes_cursor(monkeypatch):
    async def scenario():
        clock = [1.0]
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
        monkeypatch.setitem(worker._CONTRIBUTION_XP_REPLAY_TASK, "task", None)
        release = asyncio.Event()
        calls = []

        async def background(function, store, **kwargs):
            assert function is tracking_xp.replay_contribution_xp
            calls.append(kwargs)
            await release.wait()
            return "next-exercise"

        monkeypatch.setattr(worker.asyncio, "to_thread", background)
        state = {"after_id": "previous-exercise"}
        stop = asyncio.Event()
        await asyncio.wait_for(worker._run_contribution_xp_replay_if_due(
            store=object(), state=state, shutdown_event=stop), timeout=0.1)
        await asyncio.sleep(0)
        assert len(calls) == 1
        assert calls[0]["after_id"] == "previous-exercise"
        assert calls[0]["should_stop"]() is False
        clock[0] = 120.0
        await worker._run_contribution_xp_replay_if_due(store=object(), state=state)
        assert len(calls) == 1, "a still-running sweep must not overlap"
        release.set()
        await worker._CONTRIBUTION_XP_REPLAY_TASK["task"]
        assert state["after_id"] == "next-exercise"
        await worker._run_contribution_xp_replay_if_due(store=object(), state=state)
        await worker._CONTRIBUTION_XP_REPLAY_TASK["task"]
        assert len(calls) == 2
        assert calls[1]["after_id"] == "next-exercise"
        clock[0] = 130.0
        await worker._run_contribution_xp_replay_if_due(store=object(), state=state)
        assert len(calls) == 2, "check the cadence after completion"
        clock[0] = 300.0
        stop.set()
        await worker._run_contribution_xp_replay_if_due(store=object(), state=state, shutdown_event=stop)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_background_replay_failure_preserves_cursor_and_allows_retry(monkeypatch):
    async def scenario():
        clock = [1.0]
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
        monkeypatch.setitem(worker._CONTRIBUTION_XP_REPLAY_TASK, "task", None)
        calls = []

        async def unavailable(function, store, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(worker.asyncio, "to_thread", unavailable)
        state = {"after_id": "previous"}
        await worker._run_contribution_xp_replay_if_due(store=object(), state=state)
        await worker._CONTRIBUTION_XP_REPLAY_TASK["task"]
        assert state["after_id"] == "previous"
        clock[0] = 62.0
        await worker._run_contribution_xp_replay_if_due(store=object(), state=state)
        await worker._CONTRIBUTION_XP_REPLAY_TASK["task"]
        assert len(calls) == 2

    asyncio.run(scenario())


def test_replay_waits_the_idle_interval_only_after_a_pass_finds_nothing(monkeypatch):
    async def scenario():
        clock = [1.0]
        monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
        monkeypatch.setitem(worker._CONTRIBUTION_XP_REPLAY_TASK, "task", None)
        cursors = iter(["page-1", None, "page-1"])
        calls = []

        async def background(function, store, **kwargs):
            calls.append(kwargs["after_id"])
            return next(cursors)

        monkeypatch.setattr(worker.asyncio, "to_thread", background)
        state = {}

        async def tick(at):
            clock[0] = at
            await worker._run_contribution_xp_replay_if_due(store=object(), state=state)
            task = worker._CONTRIBUTION_XP_REPLAY_TASK["task"]
            if task is not None:
                await task

        await tick(1.0)  # first pass starts at once; a full page leaves a cursor
        await tick(62.0)  # mid-walk: the short cadence drains the backlog
        assert calls == [None, "page-1"]
        assert state["after_id"] is None  # caught up

        await tick(123.0)
        await tick(900.0)
        assert len(calls) == 2, "a caught-up replay waits the idle interval"
        await tick(963.0)
        assert calls == [None, "page-1", None]

    asyncio.run(scenario())
