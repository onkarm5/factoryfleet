"""Fixed-interval ticker on its own thread.

Two of these drive the agent: one samples the machine, one drains the outbox to the broker.
They are deliberately separate, and set to different intervals, so that a broker outage
stalls publishing without also stopping the machine from being measured. Readings pile up in
the outbox and go out when the link returns.

Three properties matter more than the small amount of code here:

*Stopping is immediate.* The wait between ticks is an :class:`threading.Event` wait, not a
sleep, so shutdown does not have to sit through the remainder of an interval. A service
manager that asks the agent to stop and is ignored for thirty seconds eventually kills it.

*The interval does not drift.* Each wait is the interval minus however long the tick itself
took, so a sampling cycle that takes two seconds still produces a reading every ten rather
than every twelve. Timing uses the monotonic clock, so an NTP correction cannot make the
agent sleep for hours or spin.

*A failing tick never ends the thread.* The task is called inside a guard and failures are
reported to a callback. An agent whose sampler thread died silently looks exactly like a
healthy machine reporting nothing, which is the worst possible failure for a monitoring tool.
"""

from __future__ import annotations

import time
from threading import Event, Thread
from typing import Callable

#: Monotonic seconds. Injected only so tests can assert on interval arithmetic.
Ticker = Callable[[], float]

#: Waits up to ``timeout`` seconds, returning True if the agent was asked to stop meanwhile.
#: Defaults to the scheduler's own stop event; injectable so a test can observe how long each
#: tick asks to wait without measuring wall clock.
Waiter = Callable[[float], bool]

ErrorHandler = Callable[[BaseException], None]


class Scheduler:
    """Calls ``task`` every ``interval_seconds`` until stopped."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        task: Callable[[], None],
        name: str = "scheduler",
        run_immediately: bool = True,
        on_error: ErrorHandler | None = None,
        ticker: Ticker = time.monotonic,
        waiter: Waiter | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._interval = float(interval_seconds)
        self._task = task
        self._name = name
        # The agent should report something the moment it starts rather than after a first
        # silent interval, which on a slow schedule looks like a broken install.
        self._run_immediately = run_immediately
        self._on_error = on_error
        self._ticker = ticker
        self._stop = Event()
        self._wait = waiter if waiter is not None else self._stop.wait
        self._thread: Thread | None = None
        self._ticks = 0
        self._errors = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def ticks(self) -> int:
        """Completed tick attempts, successful or not."""
        return self._ticks

    @property
    def errors(self) -> int:
        return self._errors

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError(f"scheduler '{self._name}' is already running")
        self._stop.clear()
        # Daemon so a missed stop cannot wedge interpreter shutdown; the agent still joins
        # its threads explicitly on the way out.
        self._thread = Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Signals the thread and waits for it. ``True`` if it finished within ``timeout``."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def _loop(self) -> None:
        if not self._run_immediately and self._wait(self._interval):
            return
        while not self._stop.is_set():
            started = self._ticker()
            try:
                self._task()
            except BaseException as error:  # noqa: BLE001 — the thread must outlive the task
                self._errors += 1
                self._report(error)
            self._ticks += 1

            elapsed = self._ticker() - started
            # A tick that overran its interval gets no rest, but is not allowed to make the
            # next wait negative either.
            if self._wait(max(0.0, self._interval - elapsed)):
                return

    def _report(self, error: BaseException) -> None:
        if self._on_error is None:
            return
        try:
            self._on_error(error)
        except BaseException:  # noqa: BLE001 — a broken handler must not kill the thread
            pass
