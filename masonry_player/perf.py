"""Frame-drop monitoring for the traffic light, and the benchmark plan."""

from __future__ import annotations

from dataclasses import dataclass

GREEN, YELLOW, RED, IDLE = "green", "yellow", "red", "idle"

# Fraction of frames dropped (smoothed) at which the light changes colour.
YELLOW_AT = 0.01
RED_AT = 0.05
# Going back down needs a clear margin so the light doesn't flicker.
HYSTERESIS = 0.6


@dataclass
class Sample:
    """Cumulative counters read from one player."""
    dropped: int   # frames dropped by the decoder and the video output
    fps: float     # nominal frame rate of the file


class PerfMonitor:
    """Turns per-player frame-drop counters into a smoothed drop ratio."""

    def __init__(self, smoothing: float = 0.35) -> None:
        self.smoothing = smoothing
        self.ratio = 0.0          # smoothed fraction of frames dropped
        self.instant = 0.0        # last interval's fraction
        self.status = IDLE
        self.streams = 0
        self._last: dict[object, int] = {}

    def reset(self) -> None:
        self.ratio = self.instant = 0.0
        self.status = IDLE
        self.streams = 0
        self._last.clear()

    def update(self, samples: dict[object, Sample], dt: float) -> str:
        """Feed the counters of every *playing* player, ``dt`` seconds apart."""
        dropped = 0
        expected = 0.0
        for key, s in samples.items():
            prev = self._last.get(key)
            if prev is not None and s.dropped >= prev:
                dropped += s.dropped - prev
                expected += s.fps * dt
        self._last = {k: s.dropped for k, s in samples.items()}
        self.streams = len(samples)
        if not samples:
            self.reset()
            return self.status
        if expected <= 0:
            return self.status
        self.instant = min(1.0, dropped / expected)
        self.ratio += self.smoothing * (self.instant - self.ratio)
        self.status = classify(self.ratio, self.status)
        return self.status


def classify(ratio: float, current: str = IDLE) -> str:
    if ratio >= RED_AT:
        return RED
    if current == RED and ratio >= RED_AT * HYSTERESIS:
        return RED
    if ratio >= YELLOW_AT:
        return YELLOW
    if current in (RED, YELLOW) and ratio >= YELLOW_AT * HYSTERESIS:
        return YELLOW
    return GREEN


# Stream counts tried by the benchmark, in order.
BENCH_STEPS = [1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32]
BENCH_MAX_DROP = 0.02


class BenchmarkPlan:
    """Decides which stream count to try next and what to recommend."""

    def __init__(self, limit: int = 32) -> None:
        self.steps = [s for s in BENCH_STEPS if s <= limit] or [1]
        self.index = 0
        self.passed = 0
        self.results: list[tuple[int, float]] = []
        self.done = False

    @property
    def current(self) -> int:
        return self.steps[self.index]

    def record(self, drop_ratio: float) -> None:
        self.results.append((self.current, drop_ratio))
        if drop_ratio <= BENCH_MAX_DROP:
            self.passed = self.current
            self.index += 1
            if self.index >= len(self.steps):
                self.done = True
        else:
            self.done = True

    @property
    def recommended(self) -> int:
        return max(1, self.passed)
