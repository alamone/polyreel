"""The three playlists: already played, currently playing, and queued."""

from __future__ import annotations

import itertools
import os
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv", ".mpg",
    ".mpeg", ".m2ts", ".mts", ".ts", ".3gp", ".ogv", ".vob", ".divx", ".f4v",
    ".asf", ".rm", ".rmvb", ".y4m",
}

_ids = itertools.count(1)


@dataclass(eq=False)
class Item:
    path: str
    id: int = field(default_factory=lambda: next(_ids))
    aspect: float | None = None   # display aspect ratio (w / h), None until probed
    duration: float | None = None
    probed: bool = False
    error: str | None = None
    resume_pos: float = 0.0       # where to resume if it was bumped back to the queue

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def orientation(self) -> str:
        if not self.aspect:
            return "?"
        if self.aspect > 1.05:
            return "landscape"
        if self.aspect < 0.95:
            return "portrait"
        return "square"


def expand_paths(paths: Iterable[str]) -> list[str]:
    """Files are taken as-is; folders are scanned recursively for videos."""
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort(key=str.lower)
                for f in sorted(files, key=str.lower):
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
                        out.append(os.path.join(root, f))
        elif os.path.isfile(p):
            out.append(p)
    return out


class Playlists:
    """Owns every item and which of the three lists it is in.

    Listeners registered with ``subscribe`` are called after every change.
    """

    def __init__(self) -> None:
        self.played: list[Item] = []
        self.playing: list[Item] = []
        self.queued: list[Item] = []
        self._listeners: list[Callable[[], None]] = []

    # -- notifications ---------------------------------------------------
    def subscribe(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in self._listeners:
            fn()

    # -- lookup ----------------------------------------------------------
    def find(self, item_id: int) -> Item | None:
        for lst in (self.playing, self.queued, self.played):
            for it in lst:
                if it.id == item_id:
                    return it
        return None

    def list_of(self, item: Item) -> list[Item] | None:
        for lst in (self.playing, self.queued, self.played):
            if item in lst:
                return lst
        return None

    def __len__(self) -> int:
        return len(self.played) + len(self.playing) + len(self.queued)

    # -- mutations -------------------------------------------------------
    def add(self, paths: Iterable[str], shuffle: bool = False) -> list[Item]:
        items = [Item(p) for p in paths]
        if shuffle:
            random.shuffle(items)
        self.queued.extend(items)
        self._changed()
        return items

    def start(self, item: Item) -> None:
        """Queued -> playing."""
        self.queued.remove(item)
        self.playing.append(item)
        self._changed()

    def finish(self, item: Item, error: str | None = None) -> None:
        """Playing -> played."""
        if item in self.playing:
            self.playing.remove(item)
        item.error = error or item.error
        item.resume_pos = 0.0
        self.played.append(item)
        self._changed()

    def demote(self, item: Item, position: float = 0.0) -> None:
        """Playing -> front of the queue (it no longer fits on screen)."""
        self.playing.remove(item)
        item.resume_pos = position
        self.queued.insert(0, item)
        self._changed()

    def requeue(self, item: Item, front: bool = False) -> None:
        """Played (or queued) -> queue again."""
        lst = self.list_of(item)
        if lst is self.playing:
            return
        if lst is not None:
            lst.remove(item)
        item.error = None
        item.resume_pos = 0.0
        if front:
            self.queued.insert(0, item)
        else:
            self.queued.append(item)
        self._changed()

    def move_queued(self, order: list[int]) -> None:
        """Reorder the queue to match a list of item ids."""
        by_id = {it.id: it for it in self.queued}
        new = [by_id[i] for i in order if i in by_id]
        new += [it for it in self.queued if it not in new]
        self.queued = new
        self._changed()

    def remove(self, item: Item) -> None:
        lst = self.list_of(item)
        if lst is not None and lst is not self.playing:
            lst.remove(item)
            self._changed()

    def recycle_played(self) -> bool:
        """For repeat-all: move everything already played back to the queue."""
        if not self.played:
            return False
        for it in self.played:
            it.resume_pos = 0.0
        self.queued.extend(it for it in self.played if not it.error)
        self.played = [it for it in self.played if it.error]
        self._changed()
        return True

    def clear(self) -> None:
        self.played.clear()
        self.queued.clear()
        self._changed()

    def touch(self) -> None:
        """Signal a change to item metadata (e.g. probing finished)."""
        self._changed()
