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
    # Videos cover at most their cells, which cover at most the window.
    total = sum(r.area for r in lay.rects.values())
    assert total <= W * H * (1 + 1e-6)
    assert 0 < lay.fill <= total / (W * H) + 1e-9


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


def test_adapt_keeps_other_tiles_in_place():
    # Video 0 ends and a new video of the same shape (99) replaces it: the
    # adapted arrangement must leave every other tile exactly where it was.
    items = list(enumerate([16 / 9, 16 / 9, 9 / 16, 4 / 3]))
    first = L.solve(items, W, H, seed=1, budget=None)
    new = [(99, 16 / 9)] + items[1:]
    tree, stretch = L.adapt(first, [k for k, _ in new], dict(new), W, H)
    _, widen, _, _ = L._analyse(tree, dict(new), stretch, W, H, 0.0)
    rects = L.place(tree, L.cells(dict(new), stretch), W, H, 0.0, widen)
    for k in (1, 2, 3):
        a, b = first.rects[k], rects[k]
        assert abs(a.x - b.x) < 1 and abs(a.y - b.y) < 1 and abs(a.w - b.w) < 1
    assert abs(rects[99].area - first.rects[0].area) < 1


def test_solver_keeps_arrangement_when_nothing_is_better():
    # Four equal videos tile the window perfectly; replacing one with an
    # identical shape must not move anything.
    items = list(enumerate([16 / 9] * 4))
    first = L.solve(items, W, H, seed=1, budget=None)
    second = L.solve([(99, 16 / 9)] + items[1:], W, H, prev=first, seed=2, budget=None)
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


def _run_bench(capacity, limit=64):
    """Simulate a machine that plays up to ``capacity`` streams smoothly."""
    b = BenchmarkPlan(limit=limit)
    tried = []
    while not b.done:
        tried.append(b.current)
        b.record(0.0 if b.current <= capacity else 0.2)
    return b.recommended, tried


@pytest.mark.parametrize("capacity", [0, 1, 2, 3, 5, 8, 9, 11, 13, 20, 31, 32, 40, 50, 63, 64, 100])
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


def test_benchmark_accepts_up_to_five_percent_drops_by_default():
    from polyreel.perf import DEFAULT_MAX_DROP
    assert DEFAULT_MAX_DROP == 0.05
    b = BenchmarkPlan()
    b.record(0.049)                 # 8 streams: 4.9% dropped still passes
    assert b.passed_i >= 0 and b.recommended == 8
    b.record(0.051)                 # 16 streams: 5.1% fails
    assert b.failed_i < len(b.steps)


def test_benchmark_uses_the_chosen_drop_rate():
    b = BenchmarkPlan(max_drop=0.10)
    b.record(0.08)                  # 8 streams: within 10%
    assert b.recommended == 8


def test_light_thresholds_follow_the_acceptable_drop_rate():
    from polyreel.perf import classify
    m = PerfMonitor(max_drop=0.10)
    assert (m.yellow_at, m.red_at) == pytest.approx((0.02, 0.10))
    assert classify(0.06, GREEN, m.yellow_at, m.red_at) == YELLOW
    assert classify(0.11, GREEN, m.yellow_at, m.red_at) == RED
    m.set_max_drop(0.20)
    assert classify(0.11, GREEN, m.yellow_at, m.red_at) == YELLOW


def test_drop_curve_links_drop_rate_and_stream_count():
    from polyreel.perf import DropCurve
    c = DropCurve([(8, 0.02), (16, 0.12), (12, 0.04), (10, 0.05)])   # 10 is noisy
    assert c.points == [(8, 0.02), (10, 0.05), (12, 0.05), (16, 0.12)]   # made non-decreasing
    assert c.drop_at(4) == pytest.approx(0.01)       # below the first point: towards 0
    assert c.drop_at(14) == pytest.approx(0.085)     # interpolated
    assert c.drop_at(17) is None                     # never tried
    assert c.max_streams_for(0.05) == 12
    assert c.max_streams_for(0.085) == 14
    assert c.max_streams_for(1.0) == 16              # never beyond what was tried
    assert c.max_streams_for(0.0) == 1
    assert DropCurve.from_text(c.to_text()).points == c.points
    assert not DropCurve.from_text("") and not DropCurve.from_text("junk")


def test_limits_editor_keeps_the_two_values_linked():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from polyreel.perf import DropCurve
    from polyreel.settings import LimitsEditor
    app = QApplication.instance() or QApplication([])
    e = LimitsEditor(DropCurve([(4, 0.01), (8, 0.04), (16, 0.12)]), 8, 5.0)
    e.drop.setValue(10.0)                  # loosen the drop rate: more videos
    assert e.values == (14, 10.0)
    e.streams.setValue(4)                  # fewer videos: the expected drop rate
    assert e.values == (4, 1.0)
    e.streams.setValue(20)                 # untested: the drop rate is left alone
    assert e.values == (20, 1.0) and "untested" in e.note.text()
    free = LimitsEditor(DropCurve([]), 6, 5.0)    # no benchmark: independent
    free.drop.setValue(12.0)
    assert free.values == (6, 12.0)
    del app


def test_light_shows_the_dropped_frame_percentage():
    from polyreel.ui import drop_text
    assert drop_text(0.0) == "0.0%"
    assert drop_text(0.0123) == "1.2%"
    assert drop_text(0.256) == "26%"


def test_plan_fills_many_slots_in_queue_order():
    q = _items(*[16 / 9, 9 / 16, 4 / 3, 1.0] * 10)
    p = plan([], q, W, H, max_streams=24, min_area=0.01)
    assert p.start == q[:24]                    # bulk fill keeps queue order
    assert p.layout.valid and len(p.layout.rects) == 24


def test_plan_always_starts_one_video_even_if_too_big():
    q = _items(16 / 9, 16 / 9, 16 / 9, 16 / 9, 16 / 9)
    p = plan([], q, W, H, max_streams=5, min_area=0.9)
    assert p.start == q[:1]


def test_frames_lost_counts_slow_motion_as_lost():
    from polyreel.perf import frames_lost
    a = Sample(10, 30.0, at=100.0, pos=5.0)
    smooth = Sample(10, 30.0, at=102.0, pos=7.0)
    assert frames_lost(a, smooth) == (0.0, 60.0)
    dropping = Sample(16, 30.0, at=102.0, pos=7.0)
    assert frames_lost(a, dropping) == (6.0, 60.0)
    half_speed = Sample(10, 30.0, at=102.0, pos=6.0)      # fell a second behind
    assert frames_lost(a, half_speed) == pytest.approx((30.0, 60.0))
    looped = Sample(10, 30.0, at=102.0, pos=1.0)          # wrapped around: drops only
    assert frames_lost(a, looped) == (0.0, 60.0)
    assert frames_lost(a, Sample(3, 30.0, at=102.0, pos=7.0)) is None   # a new file


def test_perf_monitor_turns_red_for_slow_motion():
    m = PerfMonitor(smoothing=1.0)
    m.update({1: Sample(0, 30.0, at=1.0, pos=0.0)}, 1.0)
    status = m.update({1: Sample(0, 30.0, at=2.0, pos=0.5)}, 1.0)   # half speed, no drops
    assert status == RED and m.ratio == pytest.approx(0.5)
