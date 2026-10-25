"""Frame-drop monitoring for the traffic light, and the benchmark plan."""

from __future__ import annotations

from dataclasses import dataclass

GREEN, YELLOW, RED, IDLE = "green", "yellow", "red", "idle"

# Default acceptable fraction of frames dropped (users can change it).  The
# traffic light turns red past it and yellow from a fifth of it.
DEFAULT_MAX_DROP = 0.05
YELLOW_FRACTION = 0.2
YELLOW_AT = DEFAULT_MAX_DROP * YELLOW_FRACTION
RED_AT = DEFAULT_MAX_DROP
# Going back down needs a clear margin so the light doesn't flicker.
HYSTERESIS = 0.6


@dataclass
class Sample:
    """Cumulative counters read from one player."""
    dropped: int     # frames dropped by the decoder and the video output
    fps: float       # nominal frame rate of the file
    at: float = 0.0  # when the counters were read (time.monotonic; 0 = unknown)
    pos: float = 0.0  # playback position then, in seconds


# A player whose position falls behind the clock by more than this share is
# playing in slow motion (as an overloaded one does instead of dropping frames).
SLOW_TOLERANCE = 0.04
# A video that has just started (or resumed) takes a moment to get going;
# the light ignores it for this many seconds.
STARTUP_GRACE = 2.0


def frames_lost(before: Sample, after: Sample, dt: float | None = None,
                tolerance: float = SLOW_TOLERANCE) -> tuple[float, float] | None:
    """Frames lost and frames expected between two readings of one player.

    Lost frames are the ones it dropped plus the ones it never got to by
    falling behind the clock.  None if the readings can't be compared (a new
    file, or no time between them)."""
    if after.dropped < before.dropped:
        return None
    if before.at and after.at:
        dt = after.at - before.at
    if not dt or dt <= 0:
        return None
    expected = after.fps * dt
    lost = float(after.dropped - before.dropped)
    if before.at and after.at:
        moved = after.pos - before.pos
        if moved >= 0:                     # not looped or sought backwards
            behind = 1.0 - moved / dt
            if behind > tolerance:
                lost += behind * expected
    return min(lost, expected), expected


class PerfMonitor:
    """Turns per-player frame-drop counters into a smoothed drop ratio."""

    def __init__(self, smoothing: float = 0.35, max_drop: float = DEFAULT_MAX_DROP,
                 grace: float = STARTUP_GRACE) -> None:
        self.smoothing = smoothing
        self.grace = grace
        self._since: dict[object, float] = {}   # when each player (re)started
        self.set_max_drop(max_drop)
        self.ratio = 0.0          # smoothed fraction of frames dropped
        self.instant = 0.0        # last interval's fraction
        self.status = IDLE
        self.streams = 0
        self._last: dict[object, Sample] = {}

    def set_max_drop(self, max_drop: float) -> None:
        """Red past the acceptable drop rate, yellow from a fifth of it."""
        self.red_at = max(1e-4, max_drop)
        self.yellow_at = self.red_at * YELLOW_FRACTION

    def reset(self) -> None:
        self.ratio = self.instant = 0.0
        self.status = IDLE
        self.streams = 0
        self._last.clear()
        self._since.clear()

    def update(self, samples: dict[object, Sample], dt: float) -> str:
        """Feed the counters of every *playing* player, ``dt`` seconds apart."""
        dropped = 0.0
        expected = 0.0
        for key, s in samples.items():
            prev = self._last.get(key)
            if s.at:
                if prev is None or s.dropped < prev.dropped or s.pos + 1.0 < prev.pos:
                    self._since[key] = s.at        # started, resumed or looped
                if s.at - self._since.get(key, s.at) < self.grace:
                    continue
            # A busy player may not have answered since last time; its
            # next fresh reading then spans the whole gap.
            if prev is not None and (s.at == 0 or s.at != prev.at):
                change = frames_lost(prev, s, dt)
                if change:
                    dropped += change[0]
                    expected += change[1]
        self._last = dict(samples)
        self._since = {k: v for k, v in self._since.items() if k in samples}
        self.streams = len(samples)
        if not samples:
            self.reset()
            return self.status
        if expected <= 0:
            return self.status
        self.instant = min(1.0, dropped / expected)
        self.ratio += self.smoothing * (self.instant - self.ratio)
        self.status = classify(self.ratio, self.status, self.yellow_at, self.red_at)
        return self.status


def classify(ratio: float, current: str = IDLE, yellow_at: float = YELLOW_AT,
             red_at: float = RED_AT) -> str:
    if ratio >= red_at:
        return RED
    if current == RED and ratio >= red_at * HYSTERESIS:
        return RED
    if ratio >= yellow_at:
        return YELLOW
    if current in (RED, YELLOW) and ratio >= yellow_at * HYSTERESIS:
        return YELLOW
    return GREEN


# Stream counts the benchmark can try.
BENCH_STEPS = [1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 48, 64]
BENCH_START = 8


class BenchmarkPlan:
    """Finds the highest stream count that plays acceptably (at most
    ``max_drop`` of frames dropped) in few measurements.

    If N streams play acceptably, fewer will too, so the plan starts in the
    middle (8), doubles while it keeps passing, and once something fails
    bisects between the highest pass and the lowest failure.  That is 3-5
    measurements instead of trying every count in turn.
    """

    def __init__(self, limit: int = 32, max_drop: float = DEFAULT_MAX_DROP) -> None:
        self.steps = [s for s in BENCH_STEPS if s <= limit] or [1]
        self.max_drop = max_drop
        self.passed_i = -1                 # highest index known to pass
        self.failed_i = len(self.steps)    # lowest index known to fail
        start = [i for i, s in enumerate(self.steps) if s <= BENCH_START]
        self.index = start[-1] if start else 0
        self.results: list[tuple[int, float]] = []
        self.done = False

    @property
    def current(self) -> int:
        return self.steps[self.index]

    def record(self, drop_ratio: float) -> None:
        self.results.append((self.current, drop_ratio))
        if drop_ratio <= self.max_drop:
            self.passed_i = self.index
        else:
            self.failed_i = self.index
        if self.failed_i - self.passed_i <= 1:
            self.done = True
        elif self.failed_i == len(self.steps):
            # Nothing has failed yet: double the stream count.
            target = self.current * 2
            bigger = [i for i, s in enumerate(self.steps) if s >= target]
            self.index = bigger[0] if bigger else len(self.steps) - 1
        else:
            self.index = (self.passed_i + self.failed_i) // 2

    @property
    def recommended(self) -> int:
        return self.steps[self.passed_i] if self.passed_i >= 0 else 1


class DropCurve:
    """What the benchmark measured: the fraction of frames dropped at each
    stream count it tried, with estimates in between.

    Links the two user settings: the most streams that stay within an
    acceptable drop rate, and the drop rate expected at a stream count.
    Measurements are noisy, so the curve is made non-decreasing (more streams
    never drop fewer frames).  Beyond the largest count tried there is no
    data, and ``drop_at`` returns None.
    """

    def __init__(self, points) -> None:
        pts = sorted((int(n), float(r)) for n, r in points if int(n) >= 1)
        merged: dict[int, float] = {}
        for n, r in pts:
            merged[n] = max(r, merged.get(n, 0.0))
        self.points: list[tuple[int, float]] = []
        worst = 0.0
        for n in sorted(merged):
            worst = max(worst, merged[n])
            self.points.append((n, worst))

    def __bool__(self) -> bool:
        return bool(self.points)

    @property
    def max_tested(self) -> int:
        return self.points[-1][0] if self.points else 0

    def drop_at(self, streams: int) -> float | None:
        """Expected fraction of frames dropped at ``streams`` streams."""
        if not self.points or streams > self.max_tested:
            return None
        n0, r0 = self.points[0]
        if streams <= n0:
            return r0 * streams / n0          # scale down towards 0 at 0 streams
        for (na, ra), (nb, rb) in zip(self.points, self.points[1:]):
            if na <= streams <= nb:
                return ra + (rb - ra) * (streams - na) / (nb - na)
        return self.points[-1][1]

    def max_streams_for(self, max_drop: float) -> int:
        """The most streams expected to drop at most ``max_drop`` (at least 1)."""
        best = 1
        for n in range(1, self.max_tested + 1):
            r = self.drop_at(n)
            if r is not None and r <= max_drop + 1e-12:
                best = n
        return best

    def to_text(self) -> str:
        return ",".join(f"{n}:{r:.5f}" for n, r in self.points)

    @classmethod
    def from_text(cls, text: str) -> "DropCurve":
        points = []
        for part in (text or "").split(","):
            try:
                n, r = part.split(":")
                points.append((int(n), float(r)))
            except ValueError:
                continue
        return cls(points)
