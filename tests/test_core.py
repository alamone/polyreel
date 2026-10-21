import itertools
import math

import pytest

from polyreel import layout as L
from polyreel.perf import (GREEN, IDLE, RED, YELLOW, BenchmarkPlan,
                                 PerfMonitor, Sample)
from polyreel.playlist import Item, Playlists
from polyreel.scheduler import plan

W, H = 1600, 900


def _no_overlap(rects):
    for (ka, a), (kb, b) in itertools.combinations(rects.items(), 2):
        ox = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
        oy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
        assert ox <= 1e-6 or oy <= 1e-6, (ka, kb)


@pytest.mark.parametrize("crop", [0.0, 0.15])
@pytest.mark.parametrize("aspects", [
    [16 / 9],
    [9 / 16] * 4,
    [16 / 9, 9 / 16, 4 / 3, 1.0, 21 / 9],
    [16 / 9, 9 / 16] * 6,
])
def test_layout_tiles_the_window(aspects, crop):
    items = list(enumerate(aspects))
    lay = L.solve(items, W, H, crop=crop)
    assert set(lay.rects) == {k for k, _ in items}
    for r in lay.rects.values():
        assert r.x >= -1e-6 and r.y >= -1e-6
        assert r.x + r.w <= W + 1e-6 and r.y + r.h <= H + 1e-6
    _no_overlap(lay.rects)
    # Cells cover the whole window; videos cover at most that.
    total = sum(r.area for r in lay.rects.values())
    assert math.isclose(total, W * H, rel_tol=1e-6)
    assert 0 < lay.fill <= 1 + 1e-9


def test_coverage_and_zoom():
    # Same shape: fills the cell, no zoom.
    assert L.coverage(16 / 9, 16 / 9, 0.15) == 1.0
    assert L.zoom(16 / 9, 16 / 9, 0.15) == 1.0
    # Slightly different shape: a small crop fills the cell completely.
    assert L.coverage(1.6, 16 / 9, 0.15) == 1.0
    assert math.isclose(L.zoom(1.6, 16 / 9, 0.15), (16 / 9) / 1.6)
    # Very different: crop to the limit, letterbox the rest.
    assert math.isclose(L.zoom(1.0, 16 / 9, 0.15), 1 / 0.85)
    assert math.isclose(L.coverage(1.0, 16 / 9, 0.15), 1 / ((16 / 9) * 0.85))
    assert math.isclose(L.coverage(1.0, 16 / 9, 0.0), 9 / 16)


def test_min_area_flag():
    items = list(enumerate([16 / 9] * 12))
    assert not L.solve(items, W, H, min_area=0.2).valid
    assert L.solve(items, W, H, min_area=0.03).valid


def test_adapt_swaps_similar_tile_in_place():
    items = list(enumerate([16 / 9, 16 / 9, 9 / 16, 4 / 3]))
    first = L.solve(items, W, H)
    # Video 0 ends; a new 16:9 video (99) takes its place.
    second = L.solve([(99, 16 / 9)] + items[1:], W, H, prev=first)
    for k in (1, 2, 3):
        a, b = first.rects[k], second.rects[k]
        assert abs(a.x - b.x) < 1 and abs(a.y - b.y) < 1 and abs(a.w - b.w) < 1


def test_tree_helpers():
    t = (L.H, 1, (L.V, 2, 3))
    assert L.leaves(L.remove_leaf(t, 2)) == [1, 3]
    assert L.leaves(L.replace_leaf(t, 3, 9)) == [1, 2, 9]
    assert L.leaves(L.chain(L.H, [1, 2, 3, 4])) == [1, 2, 3, 4]


def _items(*aspects):
    out = []
    for a in aspects:
        it = Item(f"/v/{len(out)}.mp4")
        it.aspect, it.probed = a, True
        out.append(it)
    return out


def test_plan_respects_max_streams():
    q = _items(*[16 / 9] * 10)
    p = plan([], q, W, H, max_streams=4, min_area=0.01)
    assert p.start == q[:4]
    assert len(p.layout.rects) == 4


def test_plan_respects_min_area():
    q = _items(*[16 / 9] * 10)
    p = plan([], q, W, H, max_streams=10, min_area=0.1)
    assert 1 <= len(p.start) < 10
    assert p.layout.valid and p.layout.min_area >= 0.1


def test_plan_demotes_newest_when_window_shrinks():
    playing = _items(*[16 / 9] * 6)
    p = plan(playing, [], W, H, max_streams=3, min_area=0.01)
    assert p.demote == playing[5:2:-1]


def test_plan_waits_for_unprobed_head_in_strict_mode():
    q = _items(16 / 9, 16 / 9)
    q[0].probed = False
    assert plan([], q, W, H, 4, 0.01, lookahead=1).start == []
    assert plan([], q, W, H, 4, 0.01, lookahead=3).start == [q[1]]


def test_lookahead_picks_better_fit():
    # Three landscape videos side by side leave room for a portrait one.
    playing = _items(16 / 9, 16 / 9, 16 / 9)
    q = _items(21 / 9, 9 / 16)
    strict = plan(playing, q, W, H, 4, 0.01, lookahead=1)
    loose = plan(playing, q, W, H, 4, 0.01, lookahead=2)
    assert strict.start == [q[0]]
    assert loose.layout.fill >= strict.layout.fill - 1e-9


def test_playlists_flow():
    pl = Playlists()
    calls = []
    pl.subscribe(lambda: calls.append(1))
    a, b = pl.add(["/a.mp4", "/b.mp4"])
    pl.start(a)
    assert pl.playing == [a] and pl.queued == [b]
    pl.demote(a, 12.5)
    assert pl.queued == [a, b] and a.resume_pos == 12.5
    pl.start(a)
    pl.finish(a)
    assert pl.played == [a] and a.resume_pos == 0
    pl.requeue(a, front=True)
    assert pl.queued == [a, b]
    pl.move_queued([b.id, a.id])
    assert pl.queued == [b, a]
    assert calls


def test_perf_monitor_levels():
    m = PerfMonitor(smoothing=1.0)
    assert m.update({}, 1.0) == IDLE
    m.update({1: Sample(0, 30)}, 1.0)
    assert m.update({1: Sample(0, 30)}, 1.0) == GREEN
    assert m.update({1: Sample(1, 30)}, 1.0) == YELLOW   # 1/30 dropped
    assert m.update({1: Sample(5, 30)}, 1.0) == RED      # 4/30 dropped
    assert m.update({1: Sample(5, 30)}, 1.0) == GREEN


def _run_bench(capacity, limit=32):
    """Simulate a machine that plays up to ``capacity`` streams smoothly."""
    b = BenchmarkPlan(limit=limit)
    tried = []
    while not b.done:
        tried.append(b.current)
        b.record(0.0 if b.current <= capacity else 0.2)
    return b.recommended, tried


@pytest.mark.parametrize("capacity", [0, 1, 2, 3, 5, 8, 9, 11, 13, 20, 31, 32, 50])
def test_benchmark_finds_capacity_quickly(capacity):
    from polyreel.perf import BENCH_STEPS
    recommended, tried = _run_bench(capacity)
    expected = max([s for s in BENCH_STEPS if s <= capacity] or [1])
    assert recommended == expected
    assert len(tried) <= 5, tried
    assert len(set(tried)) == len(tried)    # never measures the same count twice


def test_benchmark_plan_respects_limit():
    recommended, tried = _run_bench(capacity=100, limit=6)
    assert max(tried) <= 6 and recommended == 6


def test_recycle_played_for_endless_mode():
    pl = Playlists()
    a, b, c = pl.add(["/a.mp4", "/b.mp4", "/c.mp4"])
    for it in (a, b, c):
        pl.start(it)
    pl.finish(b)
    pl.finish(a)
    pl.finish(c, error="could not be played")
    assert pl.recycle_played()
    assert pl.queued == [b, a]          # replayed in the order they finished
    assert pl.played == [c]             # broken files stay out of rotation
    assert not pl.recycle_played()      # nothing playable left to move


def test_expand_paths_keeps_only_video_candidates(tmp_path):
    from polyreel.playlist import expand_paths
    for name in ("a.mp4", "b.MKV", "c.gif", "d.png", "e.jpg", "f.mp3", "g.txt"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "h.webm").write_bytes(b"x")
    found = sorted(p.replace(str(tmp_path), "").lstrip("/\\") for p in expand_paths([str(tmp_path)]))
    # Videos and possibly-animated images are probed; JPEGs, audio and others never.
    assert found == ["a.mp4", "b.MKV", "c.gif", "d.png", "sub/h.webm".replace("/", __import__("os").sep)]
    assert expand_paths([str(tmp_path / "e.jpg"), str(tmp_path / "a.mp4")]) == [str(tmp_path / "a.mp4")]
