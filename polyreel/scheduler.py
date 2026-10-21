"""Decide which videos play, which wait, and where each one goes."""

from __future__ import annotations

from dataclasses import dataclass, field

from .layout import Layout, solve
from .playlist import Item

DEFAULT_ASPECT = 16 / 9


@dataclass
class Plan:
    start: list[Item] = field(default_factory=list)   # queued -> playing
    demote: list[Item] = field(default_factory=list)  # playing -> queue (no longer fit)
    layout: Layout = field(default_factory=lambda: Layout(tree=None))


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
            lay = solve(_items(keep), width, height, min_area, prev, effort=0.5, budget=0.08, crop=crop)
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
    while len(current) < max_streams and pool:
        window = pool[:max(1, lookahead)]
        best: tuple[Item, Layout] | None = None
        for cand in window:
            trial = current + [cand]
            lay = solve(_items(trial), width, height, min_area, prev, effort=0, crop=crop)
            if not lay.valid and cand is window[0]:
                lay = solve(_items(trial), width, height, min_area, prev, effort=0.4, budget=0.08, crop=crop)
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
