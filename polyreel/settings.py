"""User-tweakable settings, persisted with QSettings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import math

from pathlib import Path

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .perf import DEFAULT_MAX_DROP, DropCurve
from .profiler import log_folder


@dataclass
class Settings:
    max_streams: int = 6
    max_drop_pct: float = DEFAULT_MAX_DROP * 100   # acceptable dropped frames, %
    bench_curve: str = ""          # benchmark measurements, "streams:fraction,..."
    bench_date: str = ""
    min_area_pct: float = 4.0      # smallest video, as % of the window area
    gap: int = 2                   # pixels between videos
    crop_pct: float = 0.0          # how much of a video's edges may be cropped to fill gaps
    lookahead: int = 4             # >1 lets a later queued video jump ahead if it fits better
    sound: str = "silent"          # "silent", "hover": video under the mouse, or "all"
    endless: bool = False
    shuffle_on_add: bool = False
    hwdec: str = "auto-safe"
    show_light: bool = True
    light_corner: str = "top-right"
    tray_icon: bool = True
    benchmark_asked: bool = False
    profile_log: bool = False      # record a performance log for troubleshooting

    @property
    def max_drop(self) -> float:
        return self.max_drop_pct / 100.0

    @property
    def curve(self) -> DropCurve:
        return DropCurve.from_text(self.bench_curve)

    @property
    def min_area(self) -> float:
        return self.min_area_pct / 100.0

    @property
    def crop(self) -> float:
        return self.crop_pct / 100.0

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


SOUND_MODES = [("hover", "Video under the mouse"),
               ("all", "All videos at once"),
               ("silent", "Silent")]


class LimitsEditor(QWidget):
    """The two linked limits: acceptable dropped frames and max simultaneous
    videos.  With benchmark results, changing either one sets the other from
    them; without, they are independent."""

    MIN_DROP_PCT = 0.5

    def __init__(self, curve: DropCurve, max_streams: int, max_drop_pct: float,
                 bench_date: str = "", parent=None) -> None:
        super().__init__(parent)
        self.curve = curve
        self.bench_date = bench_date
        self.drop = QDoubleSpinBox(minimum=self.MIN_DROP_PCT, maximum=50.0, decimals=1,
                                   singleStep=0.5, suffix=" %", value=max_drop_pct)
        self.drop.setToolTip("How many dropped frames you accept before playback counts as struggling.\n"
                             "The performance light turns red above this, and yellow from a fifth of it.")
        self.streams = QSpinBox(minimum=1, maximum=64, value=max_streams)
        self.streams.setToolTip("Most videos that play at the same time; the rest wait in the queue.")
        self.note = QLabel(wordWrap=True)
        self.note.setStyleSheet("color: gray")
        self.drop.valueChanged.connect(self._drop_changed)
        self.streams.valueChanged.connect(self._streams_changed)

        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("Acceptable dropped frames", self.drop)
        form.addRow("Max simultaneous videos", self.streams)
        form.addRow(self.note)
        self._update_note()

    def _set_quietly(self, box, value) -> None:
        box.blockSignals(True)
        box.setValue(value)
        box.blockSignals(False)

    def _drop_changed(self, pct: float) -> None:
        if self.curve:
            self._set_quietly(self.streams, self.curve.max_streams_for(pct / 100))
        self._update_note()

    def _streams_changed(self, n: int) -> None:
        if self.curve:
            r = self.curve.drop_at(n)
            if r is not None:
                pct = max(self.MIN_DROP_PCT, math.ceil(r * 1000 - 1e-9) / 10)   # round up to 0.1%
                self._set_quietly(self.drop, pct)
        self._update_note()

    def _update_note(self) -> None:
        if not self.curve:
            self.note.setText("Run the benchmark to link these: it measures how many frames your "
                              "computer drops at each number of videos.")
            return
        n = self.streams.value()
        r = self.curve.drop_at(n)
        when = f" ({self.bench_date})" if self.bench_date else ""
        if r is None:
            self.note.setText(f"The benchmark{when} tested up to {self.curve.max_tested} videos; "
                              f"{n} is untested, so it may drop more than "
                              f"{self.drop.value():.1f}% of frames.")
        else:
            self.note.setText(f"From the benchmark{when}: about {r * 100:.1f}% of frames dropped "
                              f"with {n} video{'s' * (n != 1)} playing.")

    @property
    def values(self) -> tuple[int, float]:
        return self.streams.value(), self.drop.value()


class BenchmarkResultsDialog(QDialog):
    """Shown after a benchmark: what was measured, and the two linked limits
    preset to the most videos within the acceptable drop rate."""

    def __init__(self, curve: DropCurve, measured: list[tuple[int, float]],
                 max_drop_pct: float, bench_date: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Benchmark results")
        lines = "\n".join(f"{n:>3} videos at once: {r * 100:5.1f}% of frames dropped"
                          for n, r in sorted(measured))
        table = QLabel(lines)
        table.setStyleSheet("font-family: monospace")
        recommended = curve.max_streams_for(max_drop_pct / 100)
        intro = QLabel(f"With at most {max_drop_pct:.1f}% of frames dropped, this computer can play "
                       f"about {recommended} of your videos at once. Adjust either value; the other "
                       f"follows the measurements.", wordWrap=True)
        self.limits = LimitsEditor(curve, recommended, max_drop_pct, bench_date)
        buttons = QDialogButtonBox()
        buttons.addButton("Use these settings", QDialogButtonBox.AcceptRole)
        buttons.addButton("Keep my current settings", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box = QVBoxLayout(self)
        box.addWidget(table)
        box.addWidget(intro)
        box.addWidget(self.limits)
        box.addWidget(buttons)


class SettingsDialog(QDialog):
    """Edits a copy of the settings; ``benchmark_requested`` is set if the
    user pressed the benchmark button."""

    CORNERS = ["top-right", "top-left", "bottom-right", "bottom-left"]

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Polyreel settings")
        self.benchmark_requested = False
        s = settings

        self.limits = LimitsEditor(s.curve, s.max_streams, s.max_drop_pct, s.bench_date)
        bench = QPushButton("Run benchmark…")
        bench.setToolTip("Measure how many frames this computer drops at each number of your videos")
        bench.clicked.connect(self._bench)
        row = QHBoxLayout()
        row.addWidget(self.limits, 1)
        row.addWidget(bench)

        self.min_area = QDoubleSpinBox(minimum=0.5, maximum=100.0, decimals=1,
                                       singleStep=0.5, suffix=" % of window area",
                                       value=s.min_area_pct)
        self.min_area.setToolTip("A video is never shrunk below this size; extra videos wait in the queue instead.\n"
                                 "25% ≈ a 2×2 grid, 11% ≈ 3×3, 6% ≈ 4×4, 4% ≈ 5×5.")
        self.gap = QSpinBox(minimum=0, maximum=40, value=s.gap, suffix=" px")
        self.crop = QDoubleSpinBox(minimum=0.0, maximum=30.0, decimals=0, singleStep=5.0,
                                   suffix=" %", value=s.crop_pct)
        self.crop.setToolTip("To fill the window without gaps, a video may be enlarged so up to this\n"
                             "much of its width or height is cut off at the edges (half on each side).\n"
                             "0% never crops: videos are shown whole with black bars around them.")
        self.lookahead = QSpinBox(minimum=1, maximum=50, value=s.lookahead)
        self.lookahead.setToolTip("1 = always play strictly in queue order.\n"
                                  "Higher values let one of the next N queued videos jump ahead "
                                  "when its shape fits the free space better.")
        self.sound = QComboBox()
        for key, label in SOUND_MODES:
            self.sound.addItem(label, key)
        self.sound.setCurrentIndex(max(0, self.sound.findData(s.sound)))
        self.sound.setToolTip("Which videos you hear. A focused video is heard unless Silent is chosen.\n"
                              "Press M to switch modes.")
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
        self.profile = QCheckBox("Record a performance log (for troubleshooting)", checked=s.profile_log)
        self.profile.setToolTip("Writes what Polyreel is doing, and any moment the window stops responding,\n"
                                "to a text file you can send along with a problem report.")
        logs = QPushButton("Open log folder")
        logs.clicked.connect(self._open_logs)
        self.profile_row = QHBoxLayout()
        self.profile_row.addWidget(self.profile, 1)
        self.profile_row.addWidget(logs)

        form = QFormLayout()
        form.addRow(row)
        form.addRow("Smallest video size", self.min_area)
        form.addRow("Gap between videos", self.gap)
        form.addRow("Crop edges to fill gaps, up to", self.crop)
        form.addRow("Queue look-ahead", self.lookahead)
        form.addRow("Sound", self.sound)
        form.addRow("Hardware decoding", self.hwdec)
        form.addRow("Light position", self.corner)

        box = QVBoxLayout(self)
        box.addLayout(form)
        for w in (self.endless, self.shuffle, self.show_light, self.tray):
            box.addWidget(w)
        box.addLayout(self.profile_row)
        hint = QLabel("Keys: Space pause all · F fullscreen · O add files · Ctrl+O add folder · "
                      "Esc leave focus/fullscreen · N skip video under mouse · M sound mode")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        box.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def _open_logs(self) -> None:
        folder = Path(log_folder())
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _bench(self) -> None:
        self.benchmark_requested = True
        self.accept()

    def apply_to(self, s: Settings) -> None:
        s.max_streams, s.max_drop_pct = self.limits.values
        s.min_area_pct = self.min_area.value()
        s.gap = self.gap.value()
        s.crop_pct = self.crop.value()
        s.lookahead = self.lookahead.value()
        s.sound = self.sound.currentData()
        s.endless = self.endless.isChecked()
        s.shuffle_on_add = self.shuffle.isChecked()
        s.hwdec = self.hwdec.currentText()
        s.show_light = self.show_light.isChecked()
        s.light_corner = self.corner.currentText()
        s.tray_icon = self.tray.isChecked()
        s.profile_log = self.profile.isChecked()
