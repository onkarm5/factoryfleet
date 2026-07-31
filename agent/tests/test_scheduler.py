import threading
import time

import pytest

from factoryfleet_agent.scheduler import Scheduler

# Short enough to keep the suite fast, long enough that a loaded machine does not make the
# assertions racy.
TICK = 0.02
PATIENCE = 5.0


def counting_task(target_ticks):
    """A task that records calls and signals once it has been called ``target_ticks`` times."""
    reached = threading.Event()
    calls: list[float] = []

    def task():
        calls.append(time.monotonic())
        if len(calls) >= target_ticks:
            reached.set()

    return task, calls, reached


def test_the_task_runs_repeatedly_until_stopped():
    task, calls, reached = counting_task(3)
    scheduler = Scheduler(interval_seconds=TICK, task=task)

    scheduler.start()
    try:
        assert reached.wait(PATIENCE), f"only {len(calls)} ticks ran"
    finally:
        scheduler.stop()

    assert len(calls) >= 3


def test_the_first_tick_runs_immediately_by_default():
    """A silent first interval on a slow schedule looks like a broken install."""
    task, calls, reached = counting_task(1)
    scheduler = Scheduler(interval_seconds=60.0, task=task)

    scheduler.start()
    try:
        assert reached.wait(PATIENCE), "no tick ran without waiting a full interval"
    finally:
        scheduler.stop()


def test_the_first_tick_can_be_deferred_by_one_interval():
    task, calls, _ = counting_task(1)
    scheduler = Scheduler(interval_seconds=60.0, task=task, run_immediately=False)

    scheduler.start()
    try:
        time.sleep(TICK * 3)
        assert calls == []
    finally:
        scheduler.stop()


def test_stopping_does_not_wait_out_the_remaining_interval():
    """A service manager that is ignored for a whole interval eventually kills the process."""
    task, _, reached = counting_task(1)
    scheduler = Scheduler(interval_seconds=30.0, task=task)

    scheduler.start()
    assert reached.wait(PATIENCE)

    started = time.monotonic()
    stopped = scheduler.stop(timeout=PATIENCE)
    elapsed = time.monotonic() - started

    assert stopped
    assert elapsed < 1.0, f"stop took {elapsed:.2f}s of a 30s interval"
    assert not scheduler.is_running()


def test_a_failing_task_does_not_end_the_thread():
    """A dead sampler thread looks exactly like a healthy machine reporting nothing."""
    attempts: list[int] = []
    reported: list[BaseException] = []

    def task():
        attempts.append(1)
        raise RuntimeError("sensor bus timeout")

    scheduler = Scheduler(
        interval_seconds=TICK, task=task, on_error=reported.append
    )

    scheduler.start()
    try:
        deadline = time.monotonic() + PATIENCE
        while len(attempts) < 3 and time.monotonic() < deadline:
            time.sleep(TICK)
        assert scheduler.is_running(), "the thread died on the first failure"
    finally:
        scheduler.stop()

    assert len(attempts) >= 3
    assert all(isinstance(error, RuntimeError) for error in reported)
    assert scheduler.errors >= 3


def test_a_broken_error_handler_does_not_end_the_thread_either():
    def task():
        raise RuntimeError("sensor bus timeout")

    def bad_handler(_error):
        raise ValueError("logging is misconfigured")

    scheduler = Scheduler(interval_seconds=TICK, task=task, on_error=bad_handler)

    scheduler.start()
    try:
        time.sleep(TICK * 5)
        assert scheduler.is_running()
    finally:
        scheduler.stop()


def test_a_failure_without_a_handler_is_still_survivable():
    scheduler = Scheduler(
        interval_seconds=TICK, task=lambda: 1 / 0, name="no-handler"
    )

    scheduler.start()
    try:
        time.sleep(TICK * 5)
        assert scheduler.is_running()
    finally:
        scheduler.stop()

    assert scheduler.errors >= 1


def virtual_time(tick_cost, interval_seconds, stop_after_ticks=3):
    """Runs the loop on a simulated clock, returning what it asked to wait after each tick.

    Injecting both the clock and the wait keeps interval arithmetic assertable without
    measuring wall clock, which would be inexact and slow.
    """
    now = [0.0]
    waits: list[float] = []

    def task():
        now[0] += tick_cost

    def waiter(timeout):
        waits.append(timeout)
        now[0] += timeout
        return len(waits) >= stop_after_ticks  # True ends the loop, as a stop request would

    scheduler = Scheduler(
        interval_seconds=interval_seconds,
        task=task,
        ticker=lambda: now[0],
        waiter=waiter,
    )
    scheduler.start()
    assert scheduler.stop(timeout=PATIENCE)
    return waits


def test_a_slow_task_does_not_push_the_interval_out():
    """Sampling every ten seconds must not become every sixteen because a cycle took six."""
    waits = virtual_time(tick_cost=6.0, interval_seconds=10.0)

    # Ten second interval minus a six second tick leaves four, not a fresh ten.
    assert waits == [pytest.approx(4.0)] * 3


def test_a_task_that_overruns_its_interval_never_waits_a_negative_time():
    waits = virtual_time(tick_cost=25.0, interval_seconds=10.0, stop_after_ticks=1)

    assert waits == [0.0]


def test_a_quick_task_waits_almost_the_whole_interval():
    waits = virtual_time(tick_cost=0.0, interval_seconds=10.0)

    assert waits == [pytest.approx(10.0)] * 3


def test_ticks_are_counted_for_observability():
    task, _, reached = counting_task(2)
    scheduler = Scheduler(interval_seconds=TICK, task=task)

    scheduler.start()
    try:
        assert reached.wait(PATIENCE)
    finally:
        scheduler.stop()

    assert scheduler.ticks >= 2
    assert scheduler.errors == 0


def test_starting_twice_is_rejected():
    scheduler = Scheduler(interval_seconds=60.0, task=lambda: None, name="sampler")

    scheduler.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            scheduler.start()
    finally:
        scheduler.stop()


def test_stopping_a_scheduler_that_never_started_is_harmless():
    assert Scheduler(interval_seconds=1.0, task=lambda: None).stop() is True


def test_a_scheduler_can_be_restarted_after_stopping():
    task, calls, reached = counting_task(1)
    scheduler = Scheduler(interval_seconds=TICK, task=task)

    scheduler.start()
    assert reached.wait(PATIENCE)
    scheduler.stop()
    before = len(calls)

    scheduler.start()
    try:
        deadline = time.monotonic() + PATIENCE
        while len(calls) <= before and time.monotonic() < deadline:
            time.sleep(TICK)
    finally:
        scheduler.stop()

    assert len(calls) > before


@pytest.mark.parametrize("interval", [0, -1, -0.5])
def test_a_non_positive_interval_is_rejected(interval):
    """Zero would spin a thread at full speed for no benefit."""
    with pytest.raises(ValueError, match="must be positive"):
        Scheduler(interval_seconds=interval, task=lambda: None)
