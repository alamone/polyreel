"""Aspect-preserving "masonry" packing of videos into a window.

Every layout is a guillotine (slicing) tree: each internal node either places
its two children side by side (``H``) or stacks them (``V``).  Each leaf is a
cell whose shape is the video's aspect ratio, optionally *stretched* a little
wider or taller.  With exact shapes, sizes are rigidly coupled (a portrait
video beside a landscape one in a row must be ~3x smaller); the stretch gives
the solver enough slack to keep every video roughly the same size.

The finished layout always spans the whole window.  Wherever a cell ends up
a different shape from its video, the video is enlarged to fill it, cropping
up to ``crop`` of its width or height, and letterboxed only beyond that, so
leftover space becomes thin gutters between videos rather than big bars.

The solver searches for a tree (and stretches) that covers as much of the
window as possible while keeping video sizes even, and (optionally) stays
close to the previous layout so tiles don't jump around when one video is
swapped for another.

This module has no Qt dependency so it can be unit-tested on its own.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Hashable, Iterable, Sequence

H = "H"  # children side by side, sharing a height
V = "V"  # children stacked, sharing a width

# A tree is either a leaf key (any hashable that is not a tuple) or
# a tuple ``(op, left, right)``.
Tree = object

# How much a cell may differ in shape from its video (the video is
# letterboxed inside); 1.5 means at most a third of a cell is black bars.
STRETCHES = (1 / 1.5, 1 / 1.25, 1 / 1.12, 1.0, 1.12, 1.25, 1.5)
# Videos may differ in size by up to SIZE_TOLERANCE x without penalty;
# beyond that the score falls off steeply (power SIZE_PENALTY).
SIZE_TOLERANCE = 1.5
SIZE_PENALTY = 2.0
W_STRETCH = 0.05   # mild preference for cells that match their video exactly
W_DEFICIT = 4.0
RESTARTS = 4
MAX_RUNS = 40
# Keep the current arrangement unless another scores clearly better, so the
# wall doesn't reshuffle when a video is replaced by one of similar shape.
STICKY = 0.06
STICKY_FILL = 0.03
W_STABILITY = 0.25


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)


@dataclass
class Layout:
    tree: Tree | None
    rects: dict[Hashable, Rect] = field(default_factory=dict)
    fill: float = 0.0
    min_area: float = 1.0  # smallest video, as a fraction of the window area
    stretch: dict = field(default_factory=dict)  # cell shape / video shape
    aspects: dict = field(default_factory=dict)  # video shapes
    crop: float = 0.0
    widen: float = 1.0
    score: float = 0.0
    valid: bool = True
    width: float = 0.0
    height: float = 0.0

    @property
    def keys(self) -> list[Hashable]:
        return list(self.rects)


def is_leaf(node: Tree) -> bool:
    return not isinstance(node, tuple)


def leaves(node: Tree) -> list[Hashable]:
    if is_leaf(node):
        return [node]
    return leaves(node[1]) + leaves(node[2])


def composite_aspect(node: Tree, aspects: dict) -> float:
    if is_leaf(node):
        return aspects[node]
    a = composite_aspect(node[1], aspects)
    b = composite_aspect(node[2], aspects)
    return a + b if node[0] == H else 1.0 / (1.0 / a + 1.0 / b)


def _place(node: Tree, memo: dict, x: float, y: float, w: float, h: float,
           out: dict) -> None:
    """Assign rects top-down using precomputed subtree aspects."""
    if is_leaf(node):
        out[node] = Rect(x, y, w, h)
        return
    op, left, right = node
    la, ra = memo[id(left)], memo[id(right)]
    # Split in the same proportions as an exact fit would, so the layout can
    # be stretched to any window shape.
    if op == H:
        lw = w * la / (la + ra)
        _place(left, memo, x, y, lw, h, out)
        _place(right, memo, x + lw, y, w - lw, h, out)
    else:
        lh = h * ra / (la + ra)
        _place(left, memo, x, y, w, lh, out)
        _place(right, memo, x, y + lh, w, h - lh, out)


def widen_factor(natural: float, window: float, crop: float) -> float:
    """How far to stretch a layout of aspect ``natural`` towards the window's
    shape: only as far as cropping the videos can absorb, so every video
    still fills its cell edge to edge and neighbours stay lined up."""
    want = window / natural
    limit = 1.0 / (1.0 - crop)
    return min(want, limit) if want >= 1.0 else max(want, 1.0 / limit)


def place(tree: Tree, aspects: dict, width: float, height: float,
          crop: float = 0.0, widen: float | None = None) -> dict:
    """Cells for ``tree`` (with cell shapes ``aspects``), centred in the window.

    The layout is stretched by ``widen`` (by default, as far towards the
    window's shape as ``crop`` allows); whatever is left over becomes an even
    border around the wall.
    """
    memo: dict = {}
    natural = _shapes(tree, aspects, memo)
    a = natural * (widen if widen is not None else widen_factor(natural, width / height, crop))
    if a > width / height:
        fw, fh = width, width / a
    else:
        fw, fh = height * a, height
    rects: dict = {}
    _place(tree, memo, (width - fw) / 2, (height - fh) / 2, fw, fh, rects)
    return rects


def coverage(cell_aspect: float, video_aspect: float, crop: float) -> float:
    """Fraction of a cell the video covers when it may crop up to ``crop``
    of its width or height to fill the cell, and is letterboxed beyond that."""
    r = cell_aspect / video_aspect
    r = max(r, 1.0 / r)
    return min(1.0, 1.0 / (r * (1.0 - crop)))


def zoom(cell_aspect: float, video_aspect: float, crop: float) -> float:
    """How much to enlarge a fitted video so it fills its cell, cropping at
    most ``crop`` of it (1.0 = plain fit)."""
    r = cell_aspect / video_aspect
    return min(max(r, 1.0 / r), 1.0 / (1.0 - crop))


# --------------------------------------------------------------------------
# Scoring


def cells(aspects: dict, stretch: dict) -> dict:
    """Cell aspect ratios: each video's shape times its stretch."""
    return {k: a * stretch.get(k, 1.0) for k, a in aspects.items()}


def _shapes(node: Tree, shapes: dict, memo: dict) -> float:
    """Composite aspect of every subtree, computed once (keyed by id)."""
    if is_leaf(node):
        a = shapes[node]
    else:
        l, r = _shapes(node[1], shapes, memo), _shapes(node[2], shapes, memo)
        a = l + r if node[0] == H else l * r / (l + r)
    memo[id(node)] = a
    return a


def _shares(node: Tree, memo: dict, frac: float, out: dict) -> None:
    if is_leaf(node):
        out[node] = frac
        return
    l, r = memo[id(node[1])], memo[id(node[2])]
    # Side by side, areas split like the aspects; stacked, like their inverses.
    lf = l / (l + r) if node[0] == H else r / (l + r)
    _shares(node[1], memo, frac * lf, out)
    _shares(node[2], memo, frac * (1.0 - lf), out)


def _analyse(tree: Tree, aspects: dict, stretch: dict, width: float, height: float,
             crop: float, widen: float | None = None, fill_window: bool = False) -> tuple:
    """Score how a tree fills the window.  Returns ``(score, widen, shares,
    covers)``: the chosen stretch towards the window's shape, each cell's
    share of the window and how much of each cell its video covers.

    Unless ``widen`` is given, it is chosen from: only as far as cropping
    can absorb (every video fills its cell, so rows and columns line up and
    any slack becomes a border around the wall), all the way to the window
    (slack becomes bars inside cells), or halfway.  Bars inside a cell count
    double against the score (coverage is squared), so they only win when
    they buy a lot of extra picture.
    """
    memo: dict = {}
    natural = _shapes(tree, cells(aspects, stretch), memo)
    r = width / height
    base: dict = {}
    _shares(tree, memo, 1.0, base)
    if fill_window:
        options = (r / natural,)
    elif widen is None:
        aligned, full = widen_factor(natural, r, crop), r / natural
        options = (aligned,) if abs(math.log(full / aligned)) < 1e-9 else \
            (aligned, math.sqrt(aligned * full), full)
    else:
        options = (widen,)
    keys = list(base)
    share0 = [base[k] for k in keys]
    # How far each cell already is from its video's shape before widening.
    dev0 = [stretch.get(k, 1.0) for k in keys]
    fill_limit = 1.0 / (1.0 - crop)
    best = None
    for w in options:
        a = natural * w
        used = a / r if a < r else r / a      # the rest is the border around the wall
        covers = []
        for d in dev0:
            q = d * w
            q = q if q >= 1.0 else 1.0 / q
            covers.append(1.0 if q <= fill_limit else fill_limit / q)
        fracs = [used * f * c for f, c in zip(share0, covers)]
        evenness = min(1.0, SIZE_TOLERANCE * min(fracs) / max(fracs))
        # Squaring coverage makes one heavily letterboxed tile cost more than
        # the same empty space spread thinly.
        quality = used * sum(f * c * c for f, c in zip(share0, covers))
        score = quality * evenness ** SIZE_PENALTY
        if best is None or score > best[0]:
            best = (score, w, used, covers)
    score, w, used, covers = best
    shares = {k: used * f for k, f in zip(keys, share0)}
    return score, w, shares, dict(zip(keys, covers))


def video_fractions(tree: Tree, aspects: dict, stretch: dict, width: float,
                    height: float, crop: float = 0.0, widen: float | None = None) -> dict:
    """Each video's visible area as a fraction of the window."""
    _, _, shares, covers = _analyse(tree, aspects, stretch, width, height, crop, widen)
    return {k: shares[k] * covers[k] for k in shares}


def evaluate(tree: Tree, aspects: dict, width: float, height: float,
             min_area: float, prev: dict | None = None,
             stretch: dict | None = None, crop: float = 0.0,
             fill_window: bool = False) -> tuple[float, float, float]:
    """Return ``(score, fill, smallest_video_fraction)``.

    The score is the covered fraction of the window (weighted against big
    bars in any one tile), scaled down sharply once the largest video is
    more than SIZE_TOLERANCE x the smallest.
    """
    stretch = stretch or {}
    score, _, shares, covers = _analyse(tree, aspects, stretch, width, height, crop,
                                        fill_window=fill_window)
    fracs = {k: shares[k] * covers[k] for k in shares}
    fill = sum(fracs.values())
    smallest = min(fracs.values())
    if stretch:
        score -= W_STRETCH * sum(abs(math.log(v)) for v in stretch.values()) / len(fracs)
    if min_area > 0 and smallest < min_area:
        score -= W_DEFICIT * (min_area - smallest) / min_area
    if prev:
        score -= W_STABILITY * _displacement(tree, cells(aspects, stretch), width, height, prev)
    return score, fill, smallest


def _displacement(tree: Tree, aspects: dict, width: float, height: float,
                  prev: dict) -> float:
    """Mean movement of tiles that were already on screen (0 = none)."""
    rects = place(tree, aspects, width, height)
    diag = math.hypot(width, height)
    total, n = 0.0, 0
    for key, old in prev.items():
        new = rects.get(key)
        if new is None:
            continue
        (ox, oy), (nx, ny) = old.center, new.center
        moved = math.hypot(nx - ox, ny - oy) / diag
        resized = abs(math.sqrt(new.area) - math.sqrt(old.area)) / diag
        total += moved + resized
        n += 1
    return total / n if n else 0.0


# --------------------------------------------------------------------------
# Tree construction helpers


def chain(op: str, nodes: Sequence[Tree]) -> Tree:
    """Combine nodes with a single operator as a balanced binary tree."""
    if len(nodes) == 1:
        return nodes[0]
    mid = len(nodes) // 2
    return (op, chain(op, nodes[:mid]), chain(op, nodes[mid:]))


def _partition(weights: Sequence[float], groups: int) -> list[list[int]]:
    """Greedily split indices into ``groups`` contiguous runs of similar weight."""
    target = sum(weights) / groups
    runs: list[list[int]] = [[]]
    acc = 0.0
    for i, w in enumerate(weights):
        remaining_items = len(weights) - i
        remaining_groups = groups - len(runs)
        if runs[-1] and remaining_groups > 0 and (
                remaining_items <= remaining_groups
                or abs(acc + w - target) > abs(acc - target)):
            runs.append([])
            acc = 0.0
        runs[-1].append(i)
        acc += w
    return runs


def justified(keys: Sequence[Hashable], aspects: dict, groups: int, rows: bool) -> Tree:
    """Justified rows (``rows=True``) or columns of the keys in order."""
    if rows:
        weights = [aspects[k] for k in keys]
        inner, outer = H, V
    else:
        weights = [1.0 / aspects[k] for k in keys]
        inner, outer = V, H
    runs = _partition(weights, groups)
    return chain(outer, [chain(inner, [keys[i] for i in run]) for run in runs if run])


def seeds(keys: Sequence[Hashable], aspects: dict) -> Iterable[Tree]:
    orders = [list(keys), sorted(keys, key=lambda k: aspects[k])]
    for order in orders:
        for g in range(1, len(order) + 1):
            yield justified(order, aspects, g, rows=True)
            yield justified(order, aspects, g, rows=False)


def remove_leaf(tree: Tree, key: Hashable) -> Tree | None:
    if is_leaf(tree):
        return None if tree == key else tree
    op, left, right = tree
    nl, nr = remove_leaf(left, key), remove_leaf(right, key)
    if nl is None:
        return nr
    if nr is None:
        return nl
    return (op, nl, nr)


def replace_leaf(tree: Tree, old: Hashable, new: Hashable) -> Tree:
    if is_leaf(tree):
        return new if tree == old else tree
    return (tree[0], replace_leaf(tree[1], old, new), replace_leaf(tree[2], old, new))


def insert_beside(tree: Tree, target: Hashable, key: Hashable, aspects: dict) -> Tree:
    """Split the tile ``target`` in two, putting ``key`` next to it.

    The split direction is the one that keeps the pair closest to the shape
    of the tile being split, so neighbours barely move.
    """
    if is_leaf(tree):
        if tree != target:
            return tree
        ta, ka = aspects[target], aspects[key]
        h_err = abs(math.log((ta + ka) / ta))
        v_err = abs(math.log((1.0 / (1.0 / ta + 1.0 / ka)) / ta))
        return (H, tree, key) if h_err <= v_err else (V, tree, key)
    return (tree[0], insert_beside(tree[1], target, key, aspects),
            insert_beside(tree[2], target, key, aspects))


def adapt(prev: "Layout", keys: Sequence[Hashable], aspects: dict,
          width: float, height: float) -> tuple[Tree, dict] | None:
    """Turn the previous layout into one over ``keys`` with minimal disruption.

    Each departed tile is taken over by the newcomer closest in shape, with
    its cell stretched to match the old cell so neighbours stay put.  Any
    remaining departures are removed and remaining newcomers split the
    biggest tiles.  Returns ``(tree, stretch)``.
    """
    if prev is None or prev.tree is None:
        return None
    old = leaves(prev.tree)
    gone = [k for k in old if k not in set(keys)]
    new = [k for k in keys if k not in set(old)]
    stretch = {k: v for k, v in prev.stretch.items() if k in aspects}
    tree = prev.tree
    for g in list(gone):
        if not new:
            break
        old_cell = prev.aspects.get(g, 1.0) * prev.stretch.get(g, 1.0)
        best = min(new, key=lambda n: abs(math.log(aspects[n] / old_cell)))
        tree = replace_leaf(tree, g, best)
        stretch[best] = min(STRETCHES, key=lambda f: abs(math.log(aspects[best] * f / old_cell)))
        new.remove(best)
        gone.remove(g)
    for g in gone:
        tree = remove_leaf(tree, g)
    for n in new:
        if tree is None:
            tree = n
            continue
        shapes = cells(aspects, stretch)
        rects = place(tree, shapes, width, height)
        biggest = max(rects, key=lambda k: rects[k].area)
        tree = insert_beside(tree, biggest, n, shapes)
    return tree, {k: v for k, v in stretch.items() if v != 1.0}


# --------------------------------------------------------------------------
# Search


def _internal_paths(node: Tree, path: tuple = ()) -> list[tuple]:
    if is_leaf(node):
        return []
    return [path] + _internal_paths(node[1], path + (1,)) + _internal_paths(node[2], path + (2,))


def _all_paths(node: Tree, path: tuple = ()) -> list[tuple]:
    if is_leaf(node):
        return [path]
    return [path] + _all_paths(node[1], path + (1,)) + _all_paths(node[2], path + (2,))


def _get(node: Tree, path: tuple) -> Tree:
    for p in path:
        node = node[p]
    return node


def _set(node: Tree, path: tuple, value: Tree) -> Tree:
    if not path:
        return value
    parts = list(node)
    parts[path[0]] = _set(node[path[0]], path[1:], value)
    return tuple(parts)


def _mutate_tree(tree: Tree, rng: random.Random) -> Tree:
    keys = leaves(tree)
    move = rng.random()
    if move < 0.35 and len(keys) >= 2:
        # Swap two tiles.
        a, b = rng.sample(keys, 2)
        marker = object()
        tree = replace_leaf(tree, a, marker)
        tree = replace_leaf(tree, b, a)
        return replace_leaf(tree, marker, b)
    if move < 0.65:
        # Flip a split direction.
        path = rng.choice(_internal_paths(tree))
        op, left, right = _get(tree, path)
        return _set(tree, path, (V if op == H else H, left, right))
    # Pull a tile out and re-insert it somewhere else.
    key = rng.choice(keys)
    rest = remove_leaf(tree, key)
    path = rng.choice(_all_paths(rest))
    target = _get(rest, path)
    op = rng.choice((H, V))
    pair = (op, target, key) if rng.random() < 0.5 else (op, key, target)
    return _set(rest, path, pair)


def _mutate(state: tuple[Tree, dict], rng: random.Random) -> tuple[Tree, dict]:
    tree, stretch = state
    if rng.random() < 0.3:
        # Make one cell a bit wider or taller than its video.
        key = rng.choice(leaves(tree))
        i = STRETCHES.index(stretch.get(key, 1.0))
        j = min(len(STRETCHES) - 1, max(0, i + rng.choice((-1, 1))))
        return tree, {**stretch, key: STRETCHES[j]}
    return _mutate_tree(tree, rng), stretch


def _towards_square(aspects: dict) -> dict:
    """Seed stretch: widen portrait cells and narrow wide ones, which lets
    mixed orientations share rows at more similar sizes."""
    out = {}
    for k, a in aspects.items():
        out[k] = STRETCHES[-1] if a < 0.9 else STRETCHES[0] if a > 1.5 else 1.0
    return out


def solve(items: Sequence[tuple[Hashable, float]], width: float, height: float,
          min_area: float = 0.0, prev: Layout | None = None,
          effort: float = 1.0, seed: int | None = None,
          budget: float | None = 0.25, crop: float = 0.0) -> Layout:
    """Find a good layout for ``items`` (``(key, aspect)`` pairs).

    ``min_area`` is the smallest allowed video as a fraction of window area;
    the returned layout's ``valid`` flag says whether it was met.
    ``prev`` biases the result towards the previous arrangement.
    ``effort`` scales the number of annealing steps (0 = seeds only);
    ``budget`` caps the search time in seconds so the UI never stalls.
    ``crop`` is how much of a video's width or height may be cut off to
    fill its cell before black bars are used instead.
    """
    if not items or width <= 0 or height <= 0:
        return Layout(tree=None)
    aspects = {k: max(0.05, min(20.0, float(a))) for k, a in items}
    keys = [k for k, _ in items]
    prev_rects = None
    if prev is not None and prev.rects and prev.width > 0 and prev.height > 0:
        # Scale the old rects to the current window size.
        sx, sy = width / prev.width, height / prev.height
        prev_rects = {k: Rect(r.x * sx, r.y * sy, r.w * sx, r.h * sy)
                      for k, r in prev.rects.items()}

    def score_of(state: tuple[Tree, dict], fill_window: bool = False) -> float:
        return evaluate(state[0], aspects, width, height, min_area, prev_rects, state[1], crop,
                        fill_window)[0]

    plain, squarish = {}, _towards_square(aspects)
    candidates = []
    for tree in seeds(keys, aspects):
        candidates += [(tree, plain), (tree, squarish)]
    for tree in seeds(keys, cells(aspects, squarish)):
        candidates.append((tree, squarish))
    adapted = adapt(prev, keys, aspects, width, height)
    if adapted is not None:
        candidates.append(adapted)
    candidates.sort(key=score_of, reverse=True)
    best = candidates[0]
    best_score = score_of(best)

    # Anneal repeatedly: first from the best distinct starting points, then
    # (while the time budget lasts) from the best result so far or a shuffled
    # seed.  The landscape is rugged enough that a single run often gets stuck.
    n = len(keys)
    steps = int(effort * min(1500, 80 + 60 * n)) if n >= 2 else 0
    starts: list = []
    for c in candidates:
        if len(starts) >= RESTARTS:
            break
        if all(c[0] != s[0] for s in starts):
            starts.append(c)
    rng = random.Random(seed if seed is not None else hash(tuple(keys)) & 0xFFFFFFFF)
    deadline = time.monotonic() + budget if budget else None
    run = 0
    while steps:
        if run < len(starts):
            start = starts[run]
        elif deadline is None or time.monotonic() > deadline - 0.02 or run >= MAX_RUNS:
            break
        elif run % 2:
            start = best
        else:
            order = list(keys)
            rng.shuffle(order)
            start = (justified(order, aspects, rng.randint(1, n), rows=rng.random() < 0.5),
                     rng.choice((plain, squarish)))
        run += 1
        # Every other run aims purely at filling the window.  That steers the
        # search towards arrangements whose overall shape matches the window,
        # which the aligned option alone tends to miss; whatever each run
        # finds is then judged by the real score.
        fill_window = run % 2 == 0
        cur = run_best = start
        cur_score = run_best_score = score_of(start, fill_window)
        t0, t1 = 0.06, 0.0005
        for i in range(steps):
            if deadline and i % 32 == 0 and time.monotonic() > deadline:
                break
            temp = t0 * (t1 / t0) ** (i / steps)
            cand = _mutate(cur, rng)
            sc = score_of(cand, fill_window)
            if sc >= cur_score or rng.random() < math.exp((sc - cur_score) / temp):
                cur, cur_score = cand, sc
                if sc > run_best_score:
                    run_best, run_best_score = cand, sc
        true_score = score_of(run_best)
        if true_score > best_score:
            best, best_score = run_best, true_score

    if adapted is not None and best is not adapted:
        a_score, a_fill, a_small = evaluate(adapted[0], aspects, width, height, min_area,
                                            prev_rects, adapted[1], crop)
        b_fill = evaluate(best[0], aspects, width, height, min_area, None, best[1], crop)[1]
        # Only stay put if that doesn't leave noticeably more empty space (a
        # newcomer of a very different shape in an old slot would).
        if (a_score >= best_score - STICKY and a_fill >= b_fill - STICKY_FILL
                and a_small + 1e-9 >= min_area):
            best, best_score = adapted, a_score

    tree, stretch = best
    _, widen, _, _ = _analyse(tree, aspects, stretch, width, height, crop)
    rects = place(tree, cells(aspects, stretch), width, height, crop, widen)
    fracs = video_fractions(tree, aspects, stretch, width, height, crop, widen)
    smallest = min(fracs.values())
    return Layout(tree=tree, rects=rects, fill=sum(fracs.values()), min_area=smallest,
                  stretch={k: v for k, v in stretch.items() if v != 1.0}, aspects=aspects,
                  score=best_score, valid=smallest + 1e-9 >= min_area,
                  width=width, height=height, crop=crop, widen=widen)
