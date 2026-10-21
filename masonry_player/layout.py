"""Aspect-preserving "masonry" packing of videos into a window.

Every layout is a guillotine (slicing) tree: each internal node either places
its two children side by side (``H``) or stacks them (``V``).  Because every
leaf keeps its own aspect ratio, a whole tree has a single composite aspect
ratio and packs with *no gaps inside it*; the only wasted space is the
letterbox between the composite and the window.  The solver therefore
searches for a tree whose composite shape matches the window while keeping
tiles reasonably even in size, and (optionally) close to the previous layout
so that tiles don't jump around when one video is swapped for another.

This module has no Qt dependency so it can be unit-tested on its own.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Hashable, Iterable, Sequence

H = "H"  # children side by side, sharing a height
V = "V"  # children stacked, sharing a width

# A tree is either a leaf key (any hashable that is not a tuple) or
# a tuple ``(op, left, right)``.
Tree = object

# Score weights.  Fill (fraction of window covered) dominates; balance
# keeps the smallest tile from being tiny compared to the average one.
W_BALANCE = 0.35
W_DEFICIT = 4.0
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
    min_area: float = 1.0  # smallest tile, as a fraction of the window area
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


def _place(node: Tree, aspects: dict, x: float, y: float, w: float, h: float,
           out: dict) -> float:
    """Assign rects top-down.  Returns the node's aspect (for reuse)."""
    if is_leaf(node):
        out[node] = Rect(x, y, w, h)
        return aspects[node]
    op, left, right = node
    la = composite_aspect(left, aspects)
    if op == H:
        lw = h * la
        _place(left, aspects, x, y, lw, h, out)
        _place(right, aspects, x + lw, y, w - lw, h, out)
    else:
        lh = w / la
        _place(left, aspects, x, y, w, lh, out)
        _place(right, aspects, x, y + lh, w, h - lh, out)
    return w / h


def place(tree: Tree, aspects: dict, width: float, height: float) -> tuple[dict, float]:
    """Fit ``tree`` centred inside ``width`` x ``height``.

    Returns ``(rects, fill)`` where fill is the covered fraction of the window.
    """
    a = composite_aspect(tree, aspects)
    if a > width / height:
        fw, fh = width, width / a
    else:
        fw, fh = height * a, height
    rects: dict = {}
    _place(tree, aspects, (width - fw) / 2, (height - fh) / 2, fw, fh, rects)
    return rects, (fw * fh) / (width * height)


# --------------------------------------------------------------------------
# Scoring


def _leaf_fractions(node: Tree, aspects: dict, frac: float, out: list) -> float:
    """Collect each leaf's share of the composite area.  Returns node aspect."""
    if is_leaf(node):
        out.append(frac)
        return aspects[node]
    op, left, right = node
    # Aspects of the children are needed before splitting the area.
    la = composite_aspect(left, aspects)
    ra = composite_aspect(right, aspects)
    if op == H:
        total = la + ra
        lf, rf = la / total, ra / total
    else:
        total = 1.0 / la + 1.0 / ra
        lf, rf = (1.0 / la) / total, (1.0 / ra) / total
    _leaf_fractions(left, aspects, frac * lf, out)
    _leaf_fractions(right, aspects, frac * rf, out)
    return la + ra if op == H else 1.0 / total


def evaluate(tree: Tree, aspects: dict, width: float, height: float,
             min_area: float, prev: dict | None = None) -> tuple[float, float, float]:
    """Return ``(score, fill, smallest_tile_fraction)`` for a tree."""
    a = composite_aspect(tree, aspects)
    r = width / height
    fill = a / r if a < r else r / a
    fracs: list[float] = []
    _leaf_fractions(tree, aspects, fill, fracs)
    smallest = min(fracs)
    mean = sum(fracs) / len(fracs)
    score = fill + W_BALANCE * (smallest / mean)
    if min_area > 0 and smallest < min_area:
        score -= W_DEFICIT * (min_area - smallest) / min_area
    if prev:
        score -= W_STABILITY * _displacement(tree, aspects, width, height, prev)
    return score, fill, smallest


def _displacement(tree: Tree, aspects: dict, width: float, height: float,
                  prev: dict) -> float:
    """Mean movement of tiles that were already on screen (0 = none)."""
    rects, _ = place(tree, aspects, width, height)
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


def adapt(prev_tree: Tree | None, keys: Sequence[Hashable], aspects: dict,
          width: float, height: float) -> Tree | None:
    """Turn the previous tree into one over ``keys`` with minimal disruption.

    Departed tiles are reused for newcomers of similar shape; leftover
    departures are removed and leftover newcomers split the biggest tiles.
    """
    if prev_tree is None:
        return None
    keyset = set(keys)
    old = [k for k in leaves(prev_tree) if k in aspects]
    if len(old) != len(leaves(prev_tree)):
        return None
    gone = [k for k in old if k not in keyset]
    new = [k for k in keys if k not in set(old)]
    tree = prev_tree
    # Swap departed tiles for newcomers with the closest aspect ratio.
    for g in list(gone):
        if not new:
            break
        best = min(new, key=lambda n: abs(math.log(aspects[n] / aspects[g])))
        tree = replace_leaf(tree, g, best)
        new.remove(best)
        gone.remove(g)
    for g in gone:
        tree = remove_leaf(tree, g)
    for n in new:
        if tree is None:
            tree = n
            continue
        rects, _ = place(tree, aspects, width, height)
        biggest = max(rects, key=lambda k: rects[k].area)
        tree = insert_beside(tree, biggest, n, aspects)
    return tree


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


def _mutate(tree: Tree, rng: random.Random) -> Tree:
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


def solve(items: Sequence[tuple[Hashable, float]], width: float, height: float,
          min_area: float = 0.0, prev: Layout | None = None,
          effort: float = 1.0, seed: int | None = None) -> Layout:
    """Find a good layout for ``items`` (``(key, aspect)`` pairs).

    ``min_area`` is the smallest allowed tile as a fraction of window area;
    the returned layout's ``valid`` flag says whether it was met.
    ``prev`` biases the result towards the previous arrangement.
    ``effort`` scales the number of annealing steps (0 = seeds only).
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

    def score_of(tree: Tree) -> float:
        return evaluate(tree, aspects, width, height, min_area, prev_rects)[0]

    candidates = list(seeds(keys, aspects))
    adapted = adapt(prev.tree if prev else None, keys, aspects, width, height)
    if adapted is not None:
        candidates.append(adapted)
    best = max(candidates, key=score_of)
    best_score = score_of(best)

    n = len(keys)
    steps = int(effort * min(2500, 120 + 90 * n)) if n >= 2 else 0
    if steps:
        rng = random.Random(seed if seed is not None else hash(tuple(keys)) & 0xFFFFFFFF)
        cur, cur_score = best, best_score
        t0, t1 = 0.06, 0.0005
        for i in range(steps):
            temp = t0 * (t1 / t0) ** (i / steps)
            cand = _mutate(cur, rng)
            s = score_of(cand)
            if s >= cur_score or rng.random() < math.exp((s - cur_score) / temp):
                cur, cur_score = cand, s
                if s > best_score:
                    best, best_score = cand, s

    rects, fill = place(best, aspects, width, height)
    smallest = min(r.area for r in rects.values()) / (width * height)
    return Layout(tree=best, rects=rects, fill=fill, min_area=smallest,
                  score=best_score, valid=smallest + 1e-9 >= min_area,
                  width=width, height=height)
