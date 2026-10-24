"""Decide which videos play, which wait, and where each one goes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import layout
from .layout import Layout, solve
from .playlist import Item

DEFAULT_ASPECT = 16 / 9
# With more free slots than this, videos are taken in queue order and the
# number that fits is found by bisection, rather than trying each slot in turn.
BULK_FROM = 3
# Seconds to spend choosing among queued videos one slot at a time; once
# used up, the rest is left for the next pass (``Plan.more``).
FILL_BUDGET: float | None = 0.15


@dataclass
class Plan:
    start: list[Item] = field(default_factory=list)   # queued -> playing
    demote: list[Item] = field(default_factory=list)  # playing -> queue (no longer fit)
    layout: Layout = field(default_factory=lambda: Layout(tree=None))
    more: bool = False   # stopped early: plan again to fill the remaining slots


def _items(items: list[Item]) -> list[tuple[int, float]]:
    return [(it.id, it.aspect or DEFAULT_ASPECT) for it in items]


def plan(playing: list[Item], queued: list[Item], width: float, height: float,
         max_streams: int, min_area: float, lookahead: int = 1,
         prev: Layout | None = None, crop: float = 0.0) -> Plan:
    """Fill free slots from the queue without breaking the size constraint.

    ``playing`` is in start order; when something has to go (the window
    shrank or the limits were lowered) the most recently started video is
    bumped back to the front of the queue.  ``lookahead`` > 1 lets a later
    queued video jump ahead when it fits the free space better.
    """
    result = Plan()
    keep = list(playing)
    max_streams = max(1, max_streams)

    while len(keep) > max_streams:
        result.demote.append(keep.pop())
    while len(keep) > 1:
        lay = solve(_items(keep), width, height, min_area, prev, effort=0, crop=crop)
        if not lay.valid:
            lay = solve(_items(keep), width, height, min_area, prev, effort=0.5, budget=layout.QUICK_BUDGET, crop=crop)
        if lay.valid:
            break
        result.demote.append(keep.pop())

    if lookahead <= 1:
        # Strict queue order: wait for the head of the queue to be probed.
        pool = []
        for it in queued:
            if not it.probed:
                break
            if not it.error:
                pool.append(it)
    else:
        pool = [it for it in queued if it.probed and not it.error]
    pool = [it for it in pool if it not in result.demote]

    current = list(keep)
    free = min(max_streams - len(current), len(pool))
    if free > BULK_FROM:
        # Many free slots (starting up, or the limit was raised): filling
        # them one at a time would mean hundreds of layouts.
        n = _most_that_fit(current, pool[:free], width, height, min_area, prev, crop)
        current += pool[:n]
        result.start += pool[:n]
        pool = pool[n:]

    deadline = time.monotonic() + FILL_BUDGET if FILL_BUDGET else None
    while len(current) < max_streams and pool:
        if deadline and time.monotonic() > deadline:
            result.more = True
            break
        window = pool[:max(1, lookahead)]
        best: tuple[Item, Layout] | None = None
        for cand in window:
            if best is not None and deadline and time.monotonic() > deadline:
                break
            trial = current + [cand]
            lay = solve(_items(trial), width, height, min_area, prev, effort=0, crop=crop)
            if not lay.valid and cand is window[0]:
                lay = solve(_items(trial), width, height, min_area, prev, effort=0.4, budget=layout.QUICK_BUDGET, crop=crop)
            if (lay.valid or not current) and (best is None or lay.score > best[1].score):
                best = (cand, lay)
        if best is None:
            break
        current.append(best[0])
        result.start.append(best[0])
        pool.remove(best[0])

    if current:
        final = solve(_items(current), width, height, min_area, prev, effort=1.0, crop=crop)
        if not final.valid:
            # Annealing can wander; fall back to the quick search if it met the limit.
            quick = solve(_items(current), width, height, min_area, prev, effort=0, crop=crop)
            if quick.valid:
                final = quick
        result.layout = final
    return result


def _most_that_fit(current: list[Item], extra: list[Item], width: float, height: float,
                   min_area: float, prev: Layout | None, crop: float) -> int:
    """How many of ``extra`` (in order) can join ``current`` within the size
    limit; at least one if nothing plays yet."""
    lo, hi = 0, len(extra)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if solve(_items(current + extra[:mid]), width, height, min_area, prev, effort=0, crop=crop).valid:
            lo = mid
        else:
            hi = mid - 1
    return lo if lo or current else min(1, len(extra))
