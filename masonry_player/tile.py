"""One on-screen video: a native child window that libmpv renders into."""

from __future__ import annotations

import os

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from .mpvlib import mpv
from .perf import Sample

ANIM_MS = 260


class VideoTile(QWidget):
    # Signals are emitted from mpv's event thread; Qt queues them to the GUI thread.
    clicked = Signal(object)
    right_clicked = Signal(object)
    finished = Signal(object)
    failed = Signal(object, str)
    shape_changed = Signal(object, float)

    def __init__(self, parent: QWidget, hwdec: str = "auto-safe") -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WA_NativeWindow)
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("black"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)
        self.setMouseTracking(True)

        self.item = None
        self._active = False     # true once the current file has loaded
        self._muted = True
        self._paused = False
        self._anim: QPropertyAnimation | None = None

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
        vo = os.environ.get("MASONRY_VO")  # e.g. "x11" for headless testing
        if vo:
            opts["vo"] = vo
        self.player = mpv.MPV(**opts)
        try:
            self.player["drag-and-drop"] = "no"   # drops go to the app, not one tile
        except Exception:
            pass

        self.player.observe_property("eof-reached", self._on_eof)
        self.player.observe_property("video-params", self._on_video_params)
        self.player.register_event_callback(self._on_event)
        self.player.register_key_binding("MBTN_LEFT", self._on_left)
        self.player.register_key_binding("MBTN_RIGHT", self._on_right)

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
    def load(self, item, start: float = 0.0, loop: bool = False, hwdec: str | None = None) -> None:
        self.item = item
        self._active = False
        self._paused = False
        p = self.player
        if hwdec:
            p["hwdec"] = hwdec
        p["start"] = f"{start:.3f}" if start > 0 else "none"
        p["loop-file"] = "inf" if loop else "no"
        p.pause = False
        p.loadfile(item.path)

    def stop(self) -> None:
        self.item = None
        self._active = False
        if self._anim:
            self._anim.stop()
        try:
            self.player.command("stop")
        except Exception:
            pass
        self.hide()

    def shutdown(self) -> None:
        self.item = None
        try:
            self.player.terminate()
        except Exception:
            pass

    def set_paused(self, paused: bool) -> None:
        if paused != self._paused:
            self._paused = paused
            self.player.pause = paused

    @property
    def paused(self) -> bool:
        return self._paused

    def set_muted(self, muted: bool) -> None:
        if muted != self._muted:
            self._muted = muted
            self.player.mute = muted

    def position(self) -> float:
        try:
            return float(self.player.time_pos or 0.0)
        except Exception:
            return 0.0

    def sample(self) -> Sample | None:
        """Cumulative dropped-frame counters, or None if nothing is playing."""
        if not self._active:
            return None
        p = self.player
        try:
            dropped = int(p.frame_drop_count or 0) + int(p.decoder_frame_drop_count or 0)
            fps = float(p.container_fps or p.estimated_vf_fps or 30.0)
        except Exception:
            return None
        return Sample(dropped, fps)

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
