"""Background probing of each file's shape and length with a headless mpv."""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QObject, Signal

from .mpvlib import mpv

TIMEOUT = 8.0


class Prober(QObject):
    # item id, aspect (float or None), duration (float or None), error (str or None)
    probed = Signal(int, object, object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="prober", daemon=True)
        self._thread.start()

    def submit(self, item_id: int, path: str) -> None:
        self._jobs.put((item_id, path))

    def close(self) -> None:
        self._jobs.put(None)

    def _run(self) -> None:
        player = mpv.MPV(vo="null", ao="null", pause="yes", idle="yes", ytdl="no",
                         aid="no", sid="no", sub_auto="no", audio_display="no")
        try:
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                item_id, path = job
                try:
                    aspect, duration, error = self._probe(player, path)
                except Exception as e:  # noqa: BLE001 - report, keep going
                    aspect, duration, error = None, None, str(e) or "could not be read"
                self.probed.emit(item_id, aspect, duration, error)
        finally:
            player.terminate()

    @staticmethod
    def _probe(player, path: str):
        def cond(evt):
            if evt.event_id.value == mpv.MpvEventID.FILE_LOADED:
                return "loaded"
            if evt.event_id.value == mpv.MpvEventID.END_FILE and evt.data.reason == mpv.MpvEventEndFile.ERROR:
                return "error"
            return None

        try:
            with player.prepare_and_wait_for_event("file-loaded", "end-file", cond=cond,
                                                   timeout=TIMEOUT) as result:
                player.loadfile(path)
            outcome = result.result()
        except TimeoutError:
            player.command("stop")
            return None, None, None  # unknown shape; play it anyway with a default
        if outcome == "error":
            return None, None, "could not be opened"

        aspect = None
        for track in player.track_list or []:
            if track.get("type") != "video" or track.get("albumart") or track.get("image"):
                continue
            w, h = track.get("demux-w"), track.get("demux-h")
            if not w or not h:
                continue
            aspect = float(w) / float(h) * float(track.get("demux-par") or 1.0)
            if (track.get("demux-rotation") or 0) in (90, 270):
                aspect = 1.0 / aspect
            break
        duration = player.duration
        player.command("stop")
        if aspect is None:
            return None, duration, "no video track"
        return aspect, duration, None
