"""One build per distinct frame, however many connections want it.

A frame's panels take seconds to compute and every query in them goes through
the candle store's single DuckDB connection, so builds cannot overlap: 16 at
once take 16 times as long each and finish no sooner. Giving every connection
its own build meant a burst of reconnects queued a build per socket, each of
them still running long after its client had gone, and a new client waited
behind all of them.

So builds are shared. Connections wanting the same inputs join the same build,
and a finished frame is kept for whoever asks next. They run on one worker,
because a second one adds queueing at the store and no throughput. A build
nobody is waiting for any more is cancelled before it starts, so abandoned
work cannot pile up, and queued builds never outnumber connected clients. Once
started, a build cannot be stopped; it runs out and its result is kept.

Nothing here expires, because a key says exactly what its frame was computed
from: if any of that changes, the key changes. That is only safe for values
that are a function of the key, which is why a kept frame holds panels and no
labels. See `MarketXRayService.frame_labels`.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass


@dataclass(eq=False)
class Flight:
    """One build, and how many connections are waiting on it."""

    key: Hashable
    work: Future
    waiters: int = 0


class SharedFrames:
    def __init__(self, *, retained: int):
        self._retained_limit = retained
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-frame")
        self._lock = threading.Lock()
        self._flights: dict[Hashable, Flight] = {}
        self._retained: OrderedDict[Hashable, dict] = OrderedDict()

    def join(self, key: Hashable, build: Callable[[], dict]) -> Flight:
        """The build for `key`: a finished one, the one under way, or a new
        one. Every join must be matched by a `leave`."""
        with self._lock:
            if key in self._retained:
                self._retained.move_to_end(key)
                finished: Future = Future()
                finished.set_result(self._retained[key])
                return Flight(key, finished)
            flight = self._flights.get(key)
            if flight is None:
                flight = Flight(key, self._executor.submit(self._run, key, build))
                self._flights[key] = flight
            flight.waiters += 1
            return flight

    def leave(self, flight: Flight) -> None:
        """Stop waiting. The last to leave cancels a build not yet started."""
        with self._lock:
            flight.waiters -= 1
            if flight.waiters == 0 and flight.work.cancel():
                del self._flights[flight.key]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"flights": len(self._flights), "retained": len(self._retained)}

    def _run(self, key: Hashable, build: Callable[[], dict]) -> dict:
        try:
            frame = build()
        except BaseException:
            # Reported to everyone waiting, and not kept: the next request
            # gets a fresh attempt.
            with self._lock:
                del self._flights[key]
            raise
        with self._lock:
            del self._flights[key]
            self._retained[key] = frame
            while len(self._retained) > self._retained_limit:
                self._retained.popitem(last=False)
        return frame
