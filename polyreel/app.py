"""Main window: owns the tiles and wires layout, playlists, focus and perf."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QCursor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QLabel, QMainWindow,
                               QMenu, QMessageBox, QSystemTrayIcon, QWidget)

from . import layout as L
from .perf import IDLE, BenchmarkPlan, PerfMonitor
from .playlist import VIDEO_EXTENSIONS, Item, Playlists, expand_paths
from .probe import Prober
from .scheduler import DEFAULT_ASPECT, plan
from .settings import Settings, SettingsDialog
from .tile import VideoTile
from .ui import BAR_HEIGHT, CloseButton, Drawer, TrafficLight, light_icon

HOVER_ZONE = 56        # px from the bottom edge that reveals the drawer
HIDE_DELAY_MS = 700
EMPTY_TEXT = ("Drop videos or folders here\n\n"
              "O  add files     Ctrl+O  add a folder     Ctrl+,  settings")


@dataclass
class BenchState:
    plan: BenchmarkPlan
    sources: list[Item]
    tiles: list[VideoTile] = field(default_factory=list)
    layout: L.Layout | None = None
    t0: float = 0.0
    c0: dict = field(default_factory=dict)
    timer: QTimer | None = None


class Stage(QWidget):
    """Black area the tiles live in."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window_ = window
        pal = self.palette()
        pal.setColor(QPalette.Window, QColor("black"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

    def resizeEvent(self, e) -> None:
        self.window_.stage_resized()

    def mouseDoubleClickEvent(self, e) -> None:
        self.window_.toggle_fullscreen()

    def contextMenuEvent(self, e) -> None:
        self.window_.show_menu(e.globalPos(), None)


class MainWindow(QMainWindow):
    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self.setWindowTitle("Polyreel")
        self.resize(1280, 760)
        self.setAcceptDrops(True)

        self.settings = Settings.load()
        self.pl = Playlists()
        self.pl.subscribe(self._lists_changed)

        self.stage = Stage(self)
        self.setCentralWidget(self.stage)

        self.tiles: dict[int, VideoTile] = {}   # item id -> tile
        self.pool: list[VideoTile] = []         # idle tiles ready for reuse
        self.all_tiles: list[VideoTile] = []
        self.layout_: L.Layout | None = None
        self.focus_tile: VideoTile | None = None
        self.paused_all = False
        self.bench: BenchState | None = None

        self.message = QLabel(self.stage)
        self.message.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.message.setAttribute(Qt.WA_NativeWindow)
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setStyleSheet("QLabel { background: #000; color: #8a8a90; font-size: 15px; padding: 18px; }")

        self.drawer = Drawer(self.stage)
        self.drawer.hide()
        self.drawer.requeue.connect(self._requeue)
        self.drawer.play_now.connect(self._play_now)
        self.drawer.focus.connect(lambda i: self.tiles.get(i) and self.enter_focus(self.tiles[i]))
        self.drawer.skip.connect(self._skip_id)
        self.drawer.remove.connect(self._remove_ids)
        self.drawer.reorder.connect(self.pl.move_queued)
        self.drawer.add_files.connect(self.add_files_dialog)
        self.drawer.add_folder.connect(self.add_folder_dialog)
        self.drawer.open_settings.connect(self.open_settings)
        self.drawer.toggle_pause.connect(self.toggle_pause)
        self.drawer.layout_changed.connect(self._drawer_changed)

        self.light = TrafficLight(self.stage)
        self.close_btn = CloseButton(self.stage)
        self.close_btn.hide()
        self.close_btn.clicked.connect(self._close_clicked)
        self._close_target: VideoTile | None = None
        self.light.clicked.connect(self.open_settings)
        self.perf = PerfMonitor()

        self.tray: QSystemTrayIcon | None = None
        self._setup_tray()

        self.prober = Prober(self)
        self.prober.probed.connect(self._probed)

        self._schedule_timer = QTimer(self, singleShot=True, interval=120)
        self._schedule_timer.timeout.connect(self.schedule)
        self._lists_timer = QTimer(self, singleShot=True, interval=150)
        self._lists_timer.timeout.connect(self._refresh_lists)
        self._hide_timer = QTimer(self, singleShot=True, interval=HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.drawer.hide)
        self._cursor_timer = QTimer(self, interval=60)
        self._cursor_timer.timeout.connect(self._cursor_tick)
        self._cursor_timer.start()
        self._perf_timer = QTimer(self, interval=1000)
        self._perf_timer.timeout.connect(self._perf_tick)
        self._perf_timer.start()
        self._last_perf = time.monotonic()

        self._shortcuts()
        self._place_overlays()
        self._update_message()
        if paths:
            QTimer.singleShot(0, lambda: self.add_paths(paths))

    # ------------------------------------------------------------------ setup
    def _shortcuts(self) -> None:
        for keys, fn in ((("Space",), self.toggle_pause),
                         (("F", "F11"), self.toggle_fullscreen),
                         (("Escape",), self.escape),
                         (("O",), self.add_files_dialog),
                         (("Ctrl+O",), self.add_folder_dialog),
                         (("Ctrl+,",), self.open_settings),
                         (("N",), self.skip_under_cursor),
                         (("Ctrl+Q",), self.close)):
            for k in keys:
                QShortcut(QKeySequence(k), self, activated=fn)

    def _setup_tray(self) -> None:
        if self.tray:
            self.tray.setVisible(self.settings.tray_icon)
            return
        if not self.settings.tray_icon or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(light_icon(IDLE), self)
        menu = QMenu()
        menu.addAction("Show / hide window", lambda: self.setVisible(not self.isVisible()))
        menu.addAction("Pause / resume all", self.toggle_pause)
        menu.addSeparator()
        menu.addAction("Quit", self.close)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.setToolTip("Polyreel")
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.showNormal() if self.isMinimized() else None
            self.show()
            self.raise_()
            self.activateWindow()

    # ---------------------------------------------------------------- adding
    def add_paths(self, paths: list[str]) -> None:
        files = expand_paths(paths)
        if not files:
            return
        items = self.pl.add(files, shuffle=self.settings.shuffle_on_add)
        for it in items:
            self.prober.submit(it.id, it.path)
        self._update_message()
        if not self.settings.benchmark_asked:
            self.settings.benchmark_asked = True
            self.settings.save()
            QTimer.singleShot(1500, self._offer_benchmark)

    def add_files_dialog(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        files, _ = QFileDialog.getOpenFileNames(self, "Add videos", "", f"Videos ({exts});;All files (*)")
        self.add_paths(files)

    def add_folder_dialog(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Add a folder of videos")
        if d:
            self.add_paths([d])

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def _probed(self, item_id: int, aspect, duration, error) -> None:
        it = self.pl.find(item_id)
        if it is None:
            return
        it.aspect, it.duration, it.probed = aspect, duration, True
        if error:
            it.error = error
            if it in self.pl.queued:
                self.pl.queued.remove(it)
                self.pl.played.append(it)
        self.pl.touch()
        self.schedule_soon()

    # ------------------------------------------------------------- scheduling
    def schedule_soon(self) -> None:
        self._schedule_timer.start()

    def full_size(self) -> tuple[int, int]:
        return max(1, self.stage.width()), max(1, self.stage.height())

    def stage_size(self) -> tuple[int, int]:
        """The area videos may use: everything above the drawer when it is
        docked open, otherwise the whole window."""
        w, h = self.full_size()
        if self.drawer.expanded:
            h -= self.drawer.preferred_height(h)
        return w, max(1, h)

    def video_area(self) -> QRect:
        return QRect(0, 0, *self.stage_size())

    def schedule(self) -> None:
        """Bump what no longer fits, start what does, and lay everything out."""
        if self.bench or self.focus_tile:
            return
        s = self.settings
        w, h = self.stage_size()
        p = plan(self.pl.playing, self.pl.queued, w, h, s.max_streams, s.min_area,
                 s.lookahead, prev=self.layout_)
        for it in p.demote:
            tile = self.tiles.pop(it.id)
            pos = tile.position()
            self._release(tile)
            self.pl.demote(it, pos)
        for it in p.start:
            self.pl.start(it)
            tile = self._acquire()
            self.tiles[it.id] = tile
            tile.load(it, start=it.resume_pos, hwdec=s.hwdec)
            tile.set_paused(self.paused_all)
        self.layout_ = p.layout
        self._apply_layout(animate=True)

        # Endless playback: once nothing is left to start, replay what has played.
        if (s.endless and self.pl.played
                and not any(it.probed and not it.error for it in self.pl.queued)):
            if self.pl.recycle_played():
                self.schedule_soon()
        self._update_message()

    def _acquire(self) -> VideoTile:
        if self.pool:
            return self.pool.pop()
        tile = VideoTile(self.stage, hwdec=self.settings.hwdec)
        tile.clicked.connect(self._tile_clicked)
        tile.right_clicked.connect(lambda t: self.show_menu(QCursor.pos(), t))
        tile.finished.connect(self._tile_finished)
        tile.failed.connect(self._tile_failed)
        tile.shape_changed.connect(self._tile_shape)
        self.all_tiles.append(tile)
        return tile

    def _release(self, tile: VideoTile) -> None:
        tile.stop()
        tile.set_muted(True)
        self.pool.append(tile)

    def _rect_for(self, r: L.Rect, sx: float = 1.0, sy: float = 1.0) -> QRect:
        g = self.settings.gap / 2
        x0, y0 = round(r.x * sx + g), round(r.y * sy + g)
        x1, y1 = round((r.x + r.w) * sx - g), round((r.y + r.h) * sy - g)
        return QRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0))

    def _apply_layout(self, animate: bool) -> None:
        lay = self.layout_
        if lay is None or not lay.rects:
            self._place_overlays()
            return
        w, h = self.stage_size()
        sx = w / lay.width if lay.width else 1.0
        sy = h / lay.height if lay.height else 1.0
        for item_id, r in lay.rects.items():
            tile = self.tiles.get(item_id)
            if tile is not None and tile is not self.focus_tile:
                tile.move_to(self._rect_for(r, sx, sy), animate)
        self._place_overlays()

    def stage_resized(self) -> None:
        if self.focus_tile:
            self.focus_tile.move_to(self.video_area(), False)
        elif self.bench and self.bench.layout:
            self._bench_layout()
        else:
            self._apply_layout(animate=False)   # stretch now, re-solve shortly
            self.schedule_soon()
        self._place_overlays()

    # ------------------------------------------------------------- tile events
    def _tile_finished(self, tile: VideoTile) -> None:
        if self.bench or tile.item is None:
            return
        item = tile.item
        if tile is self.focus_tile:
            self.exit_focus(finished=True)
            return
        self.tiles.pop(item.id, None)
        self._release(tile)
        self.pl.finish(item)
        self.schedule_soon()

    def _tile_failed(self, tile: VideoTile, message: str) -> None:
        if self.bench or tile.item is None:
            return
        item = tile.item
        if tile is self.focus_tile:
            self.focus_tile = None
            self._resume_others()
        self.tiles.pop(item.id, None)
        self._release(tile)
        self.pl.finish(item, error=message)
        self.schedule_soon()

    def _tile_shape(self, tile: VideoTile, aspect: float) -> None:
        it = tile.item
        if it is None or self.bench:
            return
        if not it.aspect or abs(aspect - it.aspect) / it.aspect > 0.02:
            it.aspect = aspect
            self.pl.touch()
            self.schedule_soon()

    def _tile_clicked(self, tile: VideoTile) -> None:
        if self.bench or tile.item is None:
            return
        if self.focus_tile is None:
            self.enter_focus(tile)
        elif tile is self.focus_tile:
            self.exit_focus(finished=False)

    # ------------------------------------------------------------------ focus
    def enter_focus(self, tile: VideoTile) -> None:
        if self.bench or self.focus_tile is tile:
            return
        if self.focus_tile is not None:
            self.exit_focus(finished=False)
        self.focus_tile = tile
        for t in self.tiles.values():
            if t is not tile:
                t.set_paused(True)
        tile.set_paused(False)
        tile.raise_()
        tile.move_to(self.video_area(), animate=True)
        self._place_overlays()
        self._cursor_tick()

    def exit_focus(self, finished: bool) -> None:
        tile = self.focus_tile
        if tile is None:
            return
        self.focus_tile = None
        if finished:
            item = tile.item
            self.tiles.pop(item.id, None)
            self._release(tile)
            self.pl.finish(item)
        self._resume_others()
        self._apply_layout(animate=True)
        self.schedule_soon()

    def _resume_others(self) -> None:
        for t in self.tiles.values():
            t.set_paused(self.paused_all)

    # -------------------------------------------------------------- commands
    def toggle_pause(self) -> None:
        if self.bench:
            return
        if self.focus_tile:
            self.focus_tile.set_paused(not self.focus_tile.paused)
            return
        self.paused_all = not self.paused_all
        for t in self.tiles.values():
            t.set_paused(self.paused_all)

    def toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def escape(self) -> None:
        if self.bench:
            self._bench_finish(cancelled=True)
        elif self.focus_tile:
            self.exit_focus(finished=False)
        elif self.isFullScreen():
            self.showNormal()

    def tile_under_cursor(self) -> VideoTile | None:
        if self.focus_tile:
            return self.focus_tile
        pos = self.stage.mapFromGlobal(QCursor.pos())
        for t in self.tiles.values():
            if t.isVisible() and t.geometry().contains(pos):
                return t
        return None

    def skip_under_cursor(self) -> None:
        t = self.tile_under_cursor()
        if t and t.item:
            self._skip_id(t.item.id)

    def _skip_id(self, item_id: int) -> None:
        tile = self.tiles.get(item_id)
        if tile is None:
            return
        if tile is self.focus_tile:
            self.exit_focus(finished=True)
        else:
            self._tile_finished(tile)

    def _requeue(self, item_id: int, front: bool) -> None:
        it = self.pl.find(item_id)
        if it:
            self.pl.requeue(it, front=front)
            self.schedule_soon()

    def _play_now(self, item_id: int) -> None:
        it = self.pl.find(item_id)
        if it and it in self.pl.queued:
            self.pl.requeue(it, front=True)
            self.schedule_soon()

    def _remove_ids(self, ids: list[int]) -> None:
        for i in ids:
            it = self.pl.find(i)
            if it:
                self.pl.remove(it)
        self._update_message()

    def show_menu(self, global_pos, tile: VideoTile | None) -> None:
        m = QMenu(self)
        if tile is not None and tile.item is not None:
            title = m.addAction(tile.item.name)
            title.setEnabled(False)
            if tile is self.focus_tile:
                m.addAction("Back to the wall", lambda: self.exit_focus(False))
            else:
                m.addAction("Focus (fill window)", lambda: self.enter_focus(tile))
            m.addAction("Skip", lambda: self._skip_id(tile.item.id))
            m.addSeparator()
        m.addAction("Add files…", self.add_files_dialog)
        m.addAction("Add folder…", self.add_folder_dialog)
        m.addAction("Pause / resume all", self.toggle_pause)
        m.addAction("Fullscreen", self.toggle_fullscreen)
        m.addAction("Settings…", self.open_settings)
        m.addAction("Run benchmark", self.start_benchmark)
        m.addSeparator()
        m.addAction("Clear played and queued", self._clear)
        m.exec(global_pos)

    def _clear(self) -> None:
        self.pl.clear()
        self._update_message()

    def open_settings(self) -> None:
        if self.bench:
            return
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            dlg.apply_to(self.settings)
            self.settings.save()
            self._setup_tray()
            self._place_overlays()
            self.schedule_soon()
            if dlg.benchmark_requested:
                QTimer.singleShot(200, self.start_benchmark)

    # ------------------------------------------------------- overlays & mouse
    def _place_overlays(self) -> None:
        w, h = self.full_size()
        dh = self.drawer.preferred_height(h)
        self.drawer.setGeometry(0, h - dh, w, dh)
        lw, lh = self.light.width(), self.light.height()
        corner = self.settings.light_corner
        x = 10 if "left" in corner else w - lw - 10
        y = 10 if "top" in corner else h - lh - 10 - (BAR_HEIGHT if self.drawer.isVisible() else 0)
        self.light.move(x, y)
        self.light.setVisible(self.settings.show_light)
        if self.message.isVisible():
            self.message.adjustSize()
            self.message.move((w - self.message.width()) // 2, (h - self.message.height()) // 2)
        for widget in (self.message, self.drawer, self.light):
            if widget.isVisible():
                widget.raise_()

    def _drawer_changed(self) -> None:
        """A list was opened or closed: dock or undock the drawer and let the
        wall reflow into the space that is left."""
        self._place_overlays()
        if self.focus_tile:
            self.focus_tile.move_to(self.video_area(), animate=True)
        elif self.bench:
            self._bench_layout()
        else:
            self._apply_layout(animate=True)
            self.schedule_soon()

    def _update_message(self) -> None:
        if self.bench:
            text = f"Benchmarking: {self.bench.plan.current} videos at once…  (Esc to cancel)"
        elif not len(self.pl):
            text = EMPTY_TEXT
        elif not self.pl.playing and any(not it.probed for it in self.pl.queued):
            text = "Reading videos…"
        elif not self.pl.playing and not self.pl.queued:
            text = "All done.  Double-click an item in Played to watch it again, or add more videos."
        else:
            text = ""
        self.message.setText(text)
        self.message.setVisible(bool(text))
        self._place_overlays()

    def _cursor_tick(self) -> None:
        pos = self.stage.mapFromGlobal(QCursor.pos())
        inside = self.stage.rect().contains(pos) and self.isVisible()
        near_bottom = inside and pos.y() >= self.stage.height() - HOVER_ZONE
        over_drawer = self.drawer.isVisible() and self.drawer.geometry().contains(pos)
        busy = QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None
        if near_bottom or over_drawer or self.drawer.expanded or (busy and self.drawer.isVisible()):
            self._hide_timer.stop()
            if not self.drawer.isVisible():
                self.drawer.show()
                self._refresh_lists()
                self._place_overlays()
        elif self.drawer.isVisible() and not self._hide_timer.isActive():
            self._hide_timer.start()

        if self.bench:
            self.close_btn.hide()
            return
        hovered = self.tile_under_cursor() if inside and not over_drawer else None
        self._show_close(hovered)

        # Audio follows the mouse (or the focused video).
        target = self.focus_tile
        if target is None and self.settings.hover_audio:
            target = hovered
        for t in self.tiles.values():
            t.set_muted(t is not target)

    def _show_close(self, tile: VideoTile | None) -> None:
        """Put the "X" in the top-right corner of the video under the mouse."""
        btn = self.close_btn
        if tile is None or tile.item is None:
            if not btn.underMouse():
                btn.hide()
                self._close_target = None
            return
        g = self._video_rect(tile)
        x, y = g.right() - btn.width() - 8, g.top() + 8
        if self.light.isVisible() and self.light.geometry().intersects(
                btn.geometry().translated(x - btn.x(), y - btn.y())):
            y = self.light.geometry().bottom() + 6   # step below the traffic light
        btn.move(x, y)
        self._close_target = tile
        if not btn.isVisible():
            btn.show()
        btn.raise_()

    @staticmethod
    def _video_rect(tile: VideoTile) -> QRect:
        """The part of a tile the picture covers (cells may be letterboxed)."""
        g = tile.geometry()
        a = tile.item.aspect if tile.item and tile.item.aspect else g.width() / max(1, g.height())
        if g.width() / max(1, g.height()) > a:
            w = round(g.height() * a)
            return QRect(g.x() + (g.width() - w) // 2, g.y(), w, g.height())
        h = round(g.width() / a)
        return QRect(g.x(), g.y() + (g.height() - h) // 2, g.width(), h)

    def _close_clicked(self) -> None:
        tile = self._close_target
        self.close_btn.hide()
        self._close_target = None
        if tile is not None and tile.item is not None:
            self._skip_id(tile.item.id)

    def _lists_changed(self) -> None:
        if not self._lists_timer.isActive():
            self._lists_timer.start()

    def _refresh_lists(self) -> None:
        self.drawer.refresh(self.pl.played, self.pl.playing, self.pl.queued)

    # ---------------------------------------------------------- performance
    def _perf_tick(self) -> None:
        now = time.monotonic()
        dt, self._last_perf = now - self._last_perf, now
        if self.bench:
            tiles = {id(t): t for t in self.bench.tiles}
        else:
            tiles = {i: t for i, t in self.tiles.items() if not t.paused}
        samples = {}
        for key, t in tiles.items():
            s = t.sample()
            if s is not None:
                samples[key] = s
        status = self.perf.update(samples, dt)
        if status == IDLE:
            tip = "No videos playing"
        else:
            label = {"green": "Smooth", "yellow": "Some frames dropped", "red": "Struggling"}[status]
            tip = (f"{label}: {self.perf.streams} video{'s' * (self.perf.streams != 1)} playing, "
                   f"{self.perf.ratio * 100:.1f}% of frames dropped\n"
                   f"Max simultaneous videos: {self.settings.max_streams} (click to change)")
        self.light.set_status(status, tip)
        if self.tray:
            if self.tray.property("status") != status:
                self.tray.setIcon(light_icon(status))
                self.tray.setProperty("status", status)
            self.tray.setToolTip("Polyreel — " + tip.split("\n")[0])

    # ------------------------------------------------------------- benchmark
    def _offer_benchmark(self) -> None:
        if self.bench or self.focus_tile:
            return
        r = QMessageBox.question(
            self, "Quick benchmark?",
            "Measure how many of these videos your computer can play at once?\n\n"
            "This takes about 30–60 seconds and sets “Max simultaneous videos”.\n"
            "You can run it any time from Settings.")
        if r == QMessageBox.Yes:
            self.start_benchmark()

    def start_benchmark(self) -> None:
        if self.bench:
            return
        everything = self.pl.playing + self.pl.queued + self.pl.played
        sources = [it for it in everything if it.probed and not it.error][:4]
        if not sources:
            QMessageBox.information(self, "Benchmark", "Add some videos first; the benchmark plays your own files.")
            return
        if self.focus_tile:
            self.exit_focus(finished=False)
        for t in self.tiles.values():
            t.set_paused(True)
            t.hide()
        self.bench = BenchState(plan=BenchmarkPlan(limit=32), sources=sources)
        self.perf.reset()
        self._bench_step()

    def _bench_layout(self) -> None:
        b = self.bench
        w, h = self.stage_size()
        items = [(i, t.item.aspect or DEFAULT_ASPECT) for i, t in enumerate(b.tiles)]
        b.layout = L.solve(items, w, h, effort=0.3)
        for i, t in enumerate(b.tiles):
            t.move_to(self._rect_for(b.layout.rects[i]), animate=False)

    def _bench_step(self) -> None:
        b = self.bench
        k = b.plan.current
        while len(b.tiles) < k:
            tile = self._acquire()
            src = b.sources[len(b.tiles) % len(b.sources)]
            tile.load(src, loop=True, hwdec=self.settings.hwdec)
            tile.set_muted(True)
            b.tiles.append(tile)
        self._bench_layout()
        self._update_message()
        self._bench_after(2000, self._bench_measure_start)   # warm up

    def _bench_after(self, ms: int, fn) -> None:
        b = self.bench
        b.timer = QTimer(self, singleShot=True, interval=ms)
        b.timer.timeout.connect(fn)
        b.timer.start()

    def _bench_counters(self) -> dict:
        out = {}
        for t in self.bench.tiles:
            s = t.sample()
            if s is not None:
                out[id(t)] = s
        return out

    def _bench_measure_start(self) -> None:
        self.bench.t0 = time.monotonic()
        self.bench.c0 = self._bench_counters()
        self._bench_after(4000, self._bench_measure_end)

    def _bench_measure_end(self) -> None:
        b = self.bench
        elapsed = time.monotonic() - b.t0
        c1 = self._bench_counters()
        dropped = expected = 0.0
        for key, s0 in b.c0.items():
            s1 = c1.get(key)
            if s1 is not None and s1.dropped >= s0.dropped:
                dropped += s1.dropped - s0.dropped
                expected += s1.fps * elapsed
        # A player that never got going counts as fully dropped.
        missing = len(b.tiles) - len(b.c0)
        expected += missing * 30 * elapsed
        dropped += missing * 30 * elapsed
        ratio = dropped / expected if expected else 1.0
        b.plan.record(ratio)
        if b.plan.done:
            self._bench_finish()
        else:
            self._bench_step()

    def _bench_finish(self, cancelled: bool = False) -> None:
        b = self.bench
        if b is None:
            return
        if b.timer:
            b.timer.stop()
        for t in b.tiles:
            self._release(t)
        self.bench = None
        self.perf.reset()
        for t in self.tiles.values():
            t.show()
        self._resume_others()
        self._apply_layout(animate=False)
        self._update_message()
        if not cancelled:
            n = b.plan.recommended
            detail = "\n".join(f"  {k:>2} videos: {r * 100:5.1f}% frames dropped" for k, r in b.plan.results)
            r = QMessageBox.question(
                self, "Benchmark finished",
                f"This computer played {n} of your videos at once smoothly.\n\n{detail}\n\n"
                f"Set “Max simultaneous videos” to {n}?")
            if r == QMessageBox.Yes:
                self.settings.max_streams = n
                self.settings.save()
        self.schedule_soon()

    # -------------------------------------------------------------- shutdown
    def closeEvent(self, e) -> None:
        self._cursor_timer.stop()
        self._perf_timer.stop()
        self.prober.close()
        for t in self.all_tiles:
            t.shutdown()
        if self.tray:
            self.tray.hide()
        super().closeEvent(e)
