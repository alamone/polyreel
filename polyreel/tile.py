"""One on-screen video: a native child window that libmpv renders into."""

from __future__ import annotations

import math
import os
import threading

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from .mpvlib import mpv
from .perf import Sample

ANIM_MS = 260
# Starting a player (and its video output) can take a good fraction of a
# second, so players start on background threads, a few at a time.
_starting = threading.BoundedSemaphore(4)


class VideoTile(QWidget):
    # Signals are emitted from mpv's event thread; Qt queues them to the GUI thread.
    clicked = Signal(object)
    right_clicked = Signal(object)
    finished = Signal(object)
    failed = Signal(object, str)
    shape_changed = Signal(object, float)
    ready = Signal(object)          # the player has started
    retired = Signal(object)        # the player has been shut down
    _player_started = Signal()      # internal: from the starter thread

    def __init__(self, parent: QWidget, hwdec: str = "auto-safe") -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("black"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setMouseTracking(True)
        # mpv can leave its video window hidden if the parent window isn't
        # visible yet when the player starts, so show the tile first
        # (as a 1x1 speck until the layout gives it a real place).
        self.setGeometry(0, 0, 1, 1)
        self.show()

        self.item = None
        self._active = False     # true once the current file has loaded
        self._muted = True
        self._paused = False
        self.zoom = 1.0          # >1 enlarges the fitted video, cropping its edges
        self.user_muted = False  # muted with its own speaker button (or by the sound mode)
        self.keep_sound = False  # volume changed by hand: heard even when not hovered
        self._anim: QPropertyAnimation | None = None
        # Values mpv reports as they change, so the GUI never has to wait on
        # a busy player to read them.
        self._state = {"time-pos": 0.0, "duration": 0.0, "volume": 100.0,
                       "frame-drop-count": 0, "decoder-frame-drop-count": 0,
                       "container-fps": None}

        opts = dict(
            wid=str(int(self.winId())),
            hwdec=hwdec,
            keep_open="yes",          # hold the last frame; we notice eof-reached
            idle="yes",
            force_window="yes",
            mute="yes",
            osc="no",
            osd_level=0,
            input_default_bindings="no",
            input_vo_keyboard="no",    # let keyboard shortcuts reach the app
            cursor_autohide="no",
            ytdl="no",
            sub_auto="no",
            audio_display="no",
        )
        vo = os.environ.get("POLYREEL_VO")  # e.g. "x11" for headless testing
        if vo:
            opts["vo"] = vo
        self.player = None
        self._pending: list[tuple] = []   # commands sent before the player started
        # Stops asked for and stops carried out (each written by one thread).
        self._stops_sent = 0
        self._stops_done = 0
        self._started = threading.Event()
        self._player_started.connect(self._flush)
        threading.Thread(target=self._start_player, args=(opts,), daemon=True,
                         name="mpv-start").start()

    def _start_player(self, opts: dict) -> None:
        """Starter thread: create the player without holding up the GUI."""
        player = None
        with _starting:
            try:
                player = mpv.MPV(**opts)
                try:
                    player["drag-and-drop"] = "no"   # drops go to the app, not one tile
                except Exception:
                    pass
                for name in self._state:
                    player.observe_property(name, self._on_state)
                player.observe_property("eof-reached", self._on_eof)
                player.observe_property("video-params", self._on_video_params)
                player.register_event_callback(self._on_event)
                player.register_key_binding("MBTN_LEFT", self._on_left)
                player.register_key_binding("MBTN_RIGHT", self._on_right)
            except Exception:
                player = None
        self.player = player
        self._started.set()
        self._player_started.emit()

    @property
    def is_ready(self) -> bool:
        return self._started.is_set()

    def _flush(self) -> None:
        """GUI thread: send what was asked of the player while it started."""
        pending, self._pending = self._pending, []
        if self.player is None:
            self._stops_done = self._stops_sent
            if self.item is not None:
                self.failed.emit(self, "player could not start")
        else:
            for args, kwargs in pending:
                self._send(args, kwargs)
        self.ready.emit(self)

    # -- mpv callbacks (event thread) ----------------------------------------
    def _on_event(self, event) -> None:
        name = event.event_id.value
        if name == mpv.MpvEventID.FILE_LOADED:
            self._active = True
        elif name == mpv.MpvEventID.END_FILE:
            data = event.data
            if data is not None and data.reason == mpv.MpvEventEndFile.ERROR and self.item is not None:
                self._active = False
                self.failed.emit(self, "could not be played")

    def _on_state(self, name, value) -> None:
        if value is not None:
            self._state[name] = value

    def _on_eof(self, _name, value) -> None:
        if value and self._active and self.item is not None:
            self._active = False
            self.finished.emit(self)

    def _on_video_params(self, _name, params) -> None:
        if not params or not self._active:
            return
        try:
            w, h = float(params.get("dw") or 0), float(params.get("dh") or 0)
            if (params.get("rotate") or 0) in (90, 270):
                w, h = h, w
        except (TypeError, ValueError, AttributeError):
            return
        if w > 0 and h > 0:
            self.shape_changed.emit(self, w / h)

    def _on_left(self, state, *_):
        if state and state[0] == "d":
            self.clicked.emit(self)

    def _on_right(self, state, *_):
        if state and state[0] == "d":
            self.right_clicked.emit(self)

    # -- control (GUI thread) -------------------------------------------------
    def _command(self, *args, **kwargs) -> None:
        """Send a command without waiting for the player to carry it out
        (queued, in order, if the player is still starting)."""
        if self.player is None and self._started.is_set():
            callback = kwargs.get("callback")
            if callback:
                callback(None, None)        # no player: nothing to do
        elif self.player is None or self._pending:
            self._pending.append((args, kwargs))
        else:
            self._send(args, kwargs)

    def _send(self, args: tuple, kwargs: dict | None = None) -> None:
        kwargs = kwargs or {}
        try:
            self.player.command_async(*args, **kwargs)
        except Exception:
            callback = kwargs.get("callback")
            if callback:
                callback(None, None)

    def _set(self, name: str, value) -> None:
        """Change a property without waiting for the player to apply it."""
        if isinstance(value, bool):
            value = "yes" if value else "no"
        self._command("set", name, str(value))

    def load(self, item, start: float = 0.0, loop: bool = False, hwdec: str | None = None) -> None:
        self.item = item
        self._active = False
        self._paused = False
        if hwdec:
            self._set("hwdec", hwdec)
        self._set("start", f"{start:.3f}" if start > 0 else "none")
        self._set("loop-file", "inf" if loop else "no")
        self.set_volume(100)  # a reused tile starts at full volume again
        self.keep_sound = False
        self._set("pause", False)
        self._state.update({"time-pos": 0.0, "duration": item.duration or 0.0})
        self._command("loadfile", item.path)

    def stop(self) -> None:
        self.item = None
        self._active = False
        if self._anim:
            self._anim.stop()
        self._stops_sent += 1
        self._command("stop", callback=self._stop_done)
        self.hide()

    def _stop_done(self, *_):
        self._stops_done += 1     # event thread

    @property
    def is_stopped(self) -> bool:
        """True once every stop asked for has been carried out (a busy
        player can take a while to get to it)."""
        return self._stops_done >= self._stops_sent

    def retire(self) -> None:
        """Shut the player down in the background, then delete the tile."""
        self.retired.connect(self.deleteLater)   # queued to the GUI thread

        def run():
            self.shutdown()
            self.retired.emit(self)
        threading.Thread(target=run, daemon=True, name="mpv-retire").start()

    def shutdown(self, wait: float = 2.0) -> None:
        """Stop the player for good (any thread)."""
        self.item = None
        self._started.wait(wait)
        try:
            if self.player is not None:
                self.player.terminate()
        except Exception:
            pass

    def set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            self._set("pause", paused)

    @property
    def paused(self) -> bool:
        return self._paused

    def duration(self) -> float:
        return float(self._state["duration"] or 0.0)

    def position(self) -> float:
        return float(self._state["time-pos"] or 0.0)

    def seek(self, seconds: float, exact: bool = True) -> None:
        self._state["time-pos"] = seconds
        self._command("seek", str(max(0.0, seconds)), "absolute" if exact else "absolute+keyframes")

    @property
    def volume(self) -> int:
        return int(round(float(self._state["volume"])))

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        self._state["volume"] = volume
        self._set("volume", volume)

    def set_zoom(self, zoom: float) -> None:
        if abs(zoom - self.zoom) > 1e-3:
            self.zoom = zoom
            self._set("video-zoom", f"{math.log2(zoom):.4f}")

    def set_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self._set("mute", muted)

    def sample(self) -> Sample | None:
        """Cumulative dropped-frame counters, or None if nothing is playing."""
        if not self._active:
            return None
        st = self._state
        dropped = int(st["frame-drop-count"] or 0) + int(st["decoder-frame-drop-count"] or 0)
        return Sample(dropped, float(st["container-fps"] or 30.0))

    def move_to(self, rect: QRect, animate: bool) -> None:
        if self._anim:
            self._anim.stop()
            self._anim = None
        if not animate or not self.isVisible() or self.geometry() == rect:
            self.setGeometry(rect)
            self.show()
            return
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(ANIM_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(self.geometry())
        anim.setEndValue(rect)
        anim.start()
        self._anim = anim
