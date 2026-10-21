"""Overlay widgets: the playlist drawer and the performance traffic light.

Videos are native child windows, so anything drawn on top of them must be a
native window too (``WA_NativeWindow``) or it would be hidden underneath.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMenu, QToolButton,
                               QVBoxLayout, QWidget)

from .perf import GREEN, IDLE, RED, YELLOW

COLORS = {
    GREEN: QColor("#2ecc71"),
    YELLOW: QColor("#f1c40f"),
    RED: QColor("#e74c3c"),
    IDLE: QColor("#7f8c8d"),
}

BAR_HEIGHT = 34

STYLE = """
QFrame#drawer { background: #18181c; border-top: 1px solid #333; }
QToolButton { color: #ddd; background: transparent; border: none; padding: 4px 10px; }
QToolButton:hover { background: #2a2a31; border-radius: 4px; }
QToolButton:checked { color: white; font-weight: bold; }
QListWidget { background: #111114; color: #ddd; border: 1px solid #2a2a31; }
QListWidget::item:selected { background: #2d4f7c; }
QLabel#listTitle { color: #999; padding: 2px 4px; }
"""


def _native(w: QWidget) -> None:
    w.setAttribute(Qt.WA_DontCreateNativeAncestors)
    w.setAttribute(Qt.WA_NativeWindow)


def fmt_time(seconds) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def light_icon(status: str, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor(0, 0, 0, 160))
    p.setBrush(COLORS.get(status, COLORS[IDLE]))
    m = size * 0.08
    p.drawEllipse(QRectF(m, m, size - 2 * m, size - 2 * m))
    p.end()
    return QIcon(pm)


class TrafficLight(QWidget):
    """Three lamps; the lit one shows how hard the computer is working."""

    clicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        _native(self)
        self.status = IDLE
        self.setFixedSize(QSize(66, 24))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("No videos playing")

    def set_status(self, status: str, tooltip: str) -> None:
        self.setToolTip(tooltip)
        if status != self.status:
            self.status = status
            self.update()

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()

    def resizeEvent(self, e) -> None:
        # Native windows can't be transparent; clip to the rounded badge.
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 11, 11)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor("#1c1c20"))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#1c1c20"))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)
        d = 14
        y = (self.height() - d) / 2
        for i, s in enumerate((GREEN, YELLOW, RED)):
            c = QColor(COLORS[s])
            if s != self.status:
                c = c.darker(420)
            p.setBrush(c)
            p.drawEllipse(QRectF(6 + i * 19, y, d, d))
        p.end()


class CloseButton(QWidget):
    """The "X" shown in the corner of the video under the mouse."""

    clicked = Signal()
    SIZE = 28

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        _native(self)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Done with this video (move it to Played)")
        self.setMouseTracking(True)
        self._hover = False

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    def resizeEvent(self, e) -> None:
        # A native window can't be transparent, so clip it to a circle.
        self.setMask(QRegion(self.rect(), QRegion.Ellipse))

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 170), 1.5))
        p.setBrush(QColor("#c0392b") if self._hover else QColor(20, 20, 24, 235))
        p.drawEllipse(QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75))
        pen = QPen(QColor("white"), 2.2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        m = self.SIZE * 0.34
        p.drawLine(QPointF(m, m), QPointF(self.SIZE - m, self.SIZE - m))
        p.drawLine(QPointF(self.SIZE - m, m), QPointF(m, self.SIZE - m))
        p.end()


class _List(QListWidget):
    deleted = Signal()

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.deleted.emit()
        else:
            super().keyPressEvent(e)


class Drawer(QFrame):
    """The three collapsible playlists that slide up from the bottom."""

    requeue = Signal(int, bool)   # item id, to the front?
    play_now = Signal(int)
    focus = Signal(int)
    skip = Signal(int)
    remove = Signal(list)
    reorder = Signal(list)
    add_files = Signal()
    add_folder = Signal()
    open_settings = Signal()
    toggle_pause = Signal()
    layout_changed = Signal()

    TITLES = ("Played", "Playing", "Queued")

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        _native(self)
        self.setObjectName("drawer")
        self.setStyleSheet(STYLE)
        self.setAutoFillBackground(True)

        self.toggles: list[QToolButton] = []
        self.lists: list[_List] = []
        self.panels: list[QWidget] = []

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 0, 6, 0)
        bar.setSpacing(2)
        body = QHBoxLayout()
        body.setContentsMargins(6, 0, 6, 6)

        for i, title in enumerate(self.TITLES):
            t = QToolButton(checkable=True)
            t.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            t.setArrowType(Qt.UpArrow)
            t.toggled.connect(lambda on, i=i: self._toggled(i, on))
            bar.addWidget(t)
            self.toggles.append(t)

            lst = _List()
            lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
            lst.setContextMenuPolicy(Qt.CustomContextMenu)
            lst.customContextMenuRequested.connect(lambda pos, i=i: self._menu(i, pos))
            lst.itemDoubleClicked.connect(lambda item, i=i: self._double(i, item))
            lst.deleted.connect(lambda i=i: self._delete(i))
            self.lists.append(lst)

            panel = QWidget()
            pl = QVBoxLayout(panel)
            pl.setContentsMargins(0, 0, 0, 0)
            pl.setSpacing(2)
            label = QLabel(title)
            label.setObjectName("listTitle")
            pl.addWidget(label)
            pl.addWidget(lst)
            panel.hide()
            body.addWidget(panel, 1)
            self.panels.append(panel)

        queued = self.lists[2]
        queued.setDragDropMode(QAbstractItemView.InternalMove)
        queued.setDefaultDropAction(Qt.MoveAction)
        queued.model().rowsMoved.connect(self._queue_moved)

        bar.addStretch(1)
        for text, tip, sig in (("⏯", "Pause / resume all (Space)", self.toggle_pause),
                               ("+ Files", "Add video files (O)", self.add_files),
                               ("+ Folder", "Add a folder of videos (Ctrl+O)", self.add_folder),
                               ("⚙", "Settings and benchmark (Ctrl+,)", self.open_settings)):
            b = QToolButton(text=text)
            b.setToolTip(tip)
            b.clicked.connect(sig.emit)
            bar.addWidget(b)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        barw = QWidget()
        barw.setFixedHeight(BAR_HEIGHT)
        barw.setLayout(bar)
        root.addWidget(barw)
        root.addLayout(body, 1)
        self._counts = (0, 0, 0)
        self._update_titles()

    # -- state -----------------------------------------------------------------
    @property
    def expanded(self) -> bool:
        return any(t.isChecked() for t in self.toggles)

    def preferred_height(self, stage_height: int) -> int:
        if not self.expanded:
            return BAR_HEIGHT
        return BAR_HEIGHT + max(120, min(320, int(stage_height * 0.26)))

    def _toggled(self, i: int, on: bool) -> None:
        self.panels[i].setVisible(on)
        self.toggles[i].setArrowType(Qt.DownArrow if on else Qt.UpArrow)
        self.layout_changed.emit()

    def _update_titles(self) -> None:
        for t, title, n in zip(self.toggles, self.TITLES, self._counts):
            t.setText(f"{title} ({n})")

    # -- contents --------------------------------------------------------------
    def refresh(self, played, playing, queued) -> None:
        self._counts = (len(played), len(playing), len(queued))
        self._update_titles()
        for lst, items in zip(self.lists, (played, playing, queued)):
            if not lst.isVisible() and lst.count() == len(items) == 0:
                continue
            ids = [lst.item(r).data(Qt.UserRole) for r in range(lst.count())]
            if ids == [it.id for it in items] and not any(
                    lst.item(r).text() != self._text(it) for r, it in enumerate(items)):
                continue
            selected = {i.data(Qt.UserRole) for i in lst.selectedItems()}
            scroll = lst.verticalScrollBar().value()
            lst.clear()
            for it in items:
                row = QListWidgetItem(self._text(it))
                row.setData(Qt.UserRole, it.id)
                row.setToolTip(it.path + (f"\n⚠ {it.error}" if it.error else ""))
                if it.error:
                    row.setForeground(QColor("#e57373"))
                lst.addItem(row)
                if it.id in selected:
                    row.setSelected(True)
            lst.verticalScrollBar().setValue(scroll)

    @staticmethod
    def _text(it) -> str:
        parts = [("⚠ " if it.error else "") + it.name]
        if it.duration:
            parts.append(fmt_time(it.duration))
        if it.aspect:
            parts.append(it.orientation)
        elif not it.probed:
            parts.append("…")
        return "  ·  ".join(parts)

    def _ids(self, i: int) -> list[int]:
        return [x.data(Qt.UserRole) for x in self.lists[i].selectedItems()]

    def _queue_moved(self, *_):
        lst = self.lists[2]
        self.reorder.emit([lst.item(r).data(Qt.UserRole) for r in range(lst.count())])

    def _double(self, i: int, row) -> None:
        item_id = row.data(Qt.UserRole)
        if i == 0:
            self.requeue.emit(item_id, True)
        elif i == 1:
            self.focus.emit(item_id)
        else:
            self.play_now.emit(item_id)

    def _delete(self, i: int) -> None:
        ids = self._ids(i)
        if not ids:
            return
        if i == 1:
            for item_id in ids:
                self.skip.emit(item_id)
        else:
            self.remove.emit(ids)

    def _menu(self, i: int, pos) -> None:
        ids = self._ids(i)
        if not ids:
            return
        m = QMenu(self)
        if i == 0:
            m.addAction("Play again next", lambda: [self.requeue.emit(x, True) for x in reversed(ids)])
            m.addAction("Add to end of queue", lambda: [self.requeue.emit(x, False) for x in ids])
            m.addAction("Remove from list", lambda: self.remove.emit(ids))
        elif i == 1:
            m.addAction("Focus (fill window)", lambda: self.focus.emit(ids[0]))
            m.addAction("Skip", lambda: [self.skip.emit(x) for x in ids])
        else:
            m.addAction("Play next", lambda: [self.play_now.emit(x) for x in reversed(ids)])
            m.addAction("Move to end", lambda: [self.requeue.emit(x, False) for x in ids])
            m.addAction("Remove from queue", lambda: self.remove.emit(ids))
        m.exec(self.lists[i].viewport().mapToGlobal(pos))
