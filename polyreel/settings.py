"""User-tweakable settings, persisted with QSettings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout)


@dataclass
class Settings:
    max_streams: int = 6
    min_area_pct: float = 4.0      # smallest video, as % of the window area
    gap: int = 2                   # pixels between videos
    lookahead: int = 4             # >1 lets a later queued video jump ahead if it fits better
    hover_audio: bool = True       # play the sound of the video under the mouse
    endless: bool = False
    shuffle_on_add: bool = False
    hwdec: str = "auto-safe"
    show_light: bool = True
    light_corner: str = "top-right"
    tray_icon: bool = True
    benchmark_asked: bool = False

    @property
    def min_area(self) -> float:
        return self.min_area_pct / 100.0

    @classmethod
    def load(cls) -> "Settings":
        qs = QSettings()
        s = cls()
        for f in fields(cls):
            if qs.contains(f.name):
                raw = qs.value(f.name)
                default = getattr(s, f.name)
                try:
                    if isinstance(default, bool):
                        val = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes")
                    else:
                        val = type(default)(raw)
                except (TypeError, ValueError):
                    continue
                setattr(s, f.name, val)
        return s

    def save(self) -> None:
        qs = QSettings()
        for k, v in asdict(self).items():
            qs.setValue(k, v)


class SettingsDialog(QDialog):
    """Edits a copy of the settings; ``benchmark_requested`` is set if the
    user pressed the benchmark button."""

    CORNERS = ["top-right", "top-left", "bottom-right", "bottom-left"]

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Polyreel settings")
        self.benchmark_requested = False
        s = settings

        self.max_streams = QSpinBox(minimum=1, maximum=64, value=s.max_streams)
        bench = QPushButton("Benchmark…")
        bench.setToolTip("Measure how many of your videos this computer can play at once")
        bench.clicked.connect(self._bench)
        row = QHBoxLayout()
        row.addWidget(self.max_streams, 1)
        row.addWidget(bench)

        self.min_area = QDoubleSpinBox(minimum=0.5, maximum=100.0, decimals=1,
                                       singleStep=0.5, suffix=" % of window area",
                                       value=s.min_area_pct)
        self.min_area.setToolTip("A video is never shrunk below this size; extra videos wait in the queue instead.\n"
                                 "25% ≈ a 2×2 grid, 11% ≈ 3×3, 6% ≈ 4×4, 4% ≈ 5×5.")
        self.gap = QSpinBox(minimum=0, maximum=40, value=s.gap, suffix=" px")
        self.lookahead = QSpinBox(minimum=1, maximum=50, value=s.lookahead)
        self.lookahead.setToolTip("1 = always play strictly in queue order.\n"
                                  "Higher values let one of the next N queued videos jump ahead "
                                  "when its shape fits the free space better.")
        self.hover_audio = QCheckBox("Play the sound of the video under the mouse", checked=s.hover_audio)
        self.endless = QCheckBox("Endless playback: when the queue runs out, "
                                 "queue everything already played again", checked=s.endless)
        self.shuffle = QCheckBox("Shuffle videos when adding them", checked=s.shuffle_on_add)
        self.hwdec = QComboBox()
        self.hwdec.addItems(["auto-safe", "auto", "no"])
        self.hwdec.setCurrentText(s.hwdec)
        self.hwdec.setToolTip("Hardware decoding lets far more videos play at once. "
                              "Applies to videos started after the change.")
        self.show_light = QCheckBox("Show performance light in the window", checked=s.show_light)
        self.corner = QComboBox()
        self.corner.addItems(self.CORNERS)
        self.corner.setCurrentText(s.light_corner)
        self.tray = QCheckBox("Show performance light in the system tray", checked=s.tray_icon)

        form = QFormLayout()
        form.addRow("Max simultaneous videos", row)
        form.addRow("Smallest video size", self.min_area)
        form.addRow("Gap between videos", self.gap)
        form.addRow("Queue look-ahead", self.lookahead)
        form.addRow("Hardware decoding", self.hwdec)
        form.addRow("Light position", self.corner)

        box = QVBoxLayout(self)
        box.addLayout(form)
        for w in (self.hover_audio, self.endless, self.shuffle, self.show_light, self.tray):
            box.addWidget(w)
        hint = QLabel("Keys: Space pause all · F fullscreen · O add files · Ctrl+O add folder · "
                      "Esc leave focus/fullscreen · N skip video under mouse")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        box.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def _bench(self) -> None:
        self.benchmark_requested = True
        self.accept()

    def apply_to(self, s: Settings) -> None:
        s.max_streams = self.max_streams.value()
        s.min_area_pct = self.min_area.value()
        s.gap = self.gap.value()
        s.lookahead = self.lookahead.value()
        s.hover_audio = self.hover_audio.isChecked()
        s.endless = self.endless.isChecked()
        s.shuffle_on_add = self.shuffle.isChecked()
        s.hwdec = self.hwdec.currentText()
        s.show_light = self.show_light.isChecked()
        s.light_corner = self.corner.currentText()
        s.tray_icon = self.tray.isChecked()
