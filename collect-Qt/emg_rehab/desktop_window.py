from __future__ import annotations
import csv
import sys
import time
from dataclasses import asdict
from collections import deque
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, QSettings, QStandardPaths, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSpinBox,QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget, QInputDialog,
      QTableWidget, QTableWidgetItem, QHeaderView, QDoubleSpinBox, QToolButton, QStyle, QGraphicsProxyWidget, QSizePolicy,)
from modules.collection import CollectionInfoRecorder, SubjectInfo, analysisOutputPath, infoOutputPath
from emg_rehab.emg.body_map import BodyMapPanel
from modules.emg.sensor_mapping import (SensorMapping, duplicateMappingSlots, loadMappings, saveMappings, matchesCsvColumn, recordingChannelSides, projectMappingPath)
from emg_rehab.emg.device_stream import CHANNEL_COUNT, SAMPLE_RATE
from modules.emg.baseline import BaselineManager, CalibrationSession, EMGPreprocessor, FilterSettings, RMSFeatureExtractor, load_raw_emg_csv
from emg_rehab.emg.stream_workers import ConnectionWorker, StreamWorker
from emg_rehab.resistance.recording import TrainerCsvRecorder, trainerOutputPath
from emg_rehab.resistance.trainer import BikeTelemetry, NumericRange, TrainerWorker
from emg_rehab.pedal.recording import PedalCsvRecorder, pedalOutputPath
from modules.pedal.analysis import analyze_pedal_records, pedal_analysis_lines, pedal_summary_lines, summarize_pedal_records
from emg_rehab.pedal.worker import PedalWorker
from emg_rehab.pedal.fit_export import exportPedalFit
from modules.pedal.csv_import import is_calibrated_angle_csv, load_pedal_csv
from modules.pedal.angle_emg import (AngleSettings, _angles, analysis_summary_text, analyze_angle_emg,export_angle_emg, phase_intensities)
from emg_rehab.cycling_bluetooth import CyclingDeviceScanWorker
from emg_rehab.gopro import GoProCommandWorker, GoProDownloadWorker, GoProMediaListWorker, GoProResult, GoProWifiConnectWorker
from emg_rehab.fluent_theme import FluentThemeManager
from modules.session_analysis import (add_point, analyze, compare_rms, load_session, new_session, peak_envelope, save_session)
from modules.session_compare import compare_sessions
from modules.storage_db import DatabaseConfig, SecondAggregator, SqlServerStorage, prefixed_id
from emg_rehab.database_writer import DatabaseWriter
from modules.reporting import export_report
from modules.archive import Archive, default_config

COLORS = (
    "#1684d7", "#e95f54", "#7e57c2", "#00897b",
    "#f39c12", "#5c6bc0", "#43a047", "#c2185b",
    "#00a6a6", "#d1495b", "#725ac1", "#6b8e23",
    "#d97706", "#0077b6", "#9d4edd", "#e76f51",
)
MAPPING_PATH = projectMappingPath()


class ArchiveWorker(QThread):
    archived = Signal(str)
    failed = Signal(str)

    def __init__(self, config: DatabaseConfig, session: dict, files: dict[str, Path], test_id: str | None):
        super().__init__()
        self.config, self.session, self.files, self.test_id = config, session, files, test_id
        self.identifier: str | None = None

    def run(self) -> None:
        try:
            identifier = Archive(self.config).save_recording(self.session, self.files, session_id=self.test_id)
            self.identifier = identifier
            self.archived.emit(identifier)
        except Exception as exc:
            self.failed.emit(str(exc))

class AngleAnalysisWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    def __init__(self, emg_path: Path, pedal_path: Path, sides: dict[str, str],
                 settings: AngleSettings, side_source: str) -> None:
        super().__init__()
        self.emg_path, self.pedal_path, self.sides, self.settings = emg_path, pedal_path, sides, settings
        self.side_source = side_source
    def run(self) -> None:
        try:
            result = analyze_angle_emg(self.emg_path, self.pedal_path, self.sides, self.settings)
            result["sync"]["side_source"] = self.side_source
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))

class AngleExportWorker(QThread):
    completed = Signal(str)
    failed = Signal(str)
    def __init__(self, result: dict, directory: Path, include_raw: bool) -> None:
        super().__init__()
        self.result, self.directory, self.include_raw = result, directory, include_raw
    def run(self) -> None:
        try:
            export_angle_emg(self.result, self.directory, self.include_raw)
            self.completed.emit(str(self.directory))
        except Exception as exc:
            self.failed.emit(str(exc))


class PolarAngleProfileWidget(QWidget):
    """Read-only polar view of early and late EMG activation by pedal angle."""

    def __init__(self, title: str, start_angles, start_values, end_angles, end_values, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.start_angles = np.asarray(start_angles, dtype=float)
        self.start_values = np.asarray(start_values, dtype=float)
        self.end_angles = np.asarray(end_angles, dtype=float)
        self.end_values = np.asarray(end_values, dtype=float)
        self.setMinimumSize(280, 280)
        self.setAccessibleName(f"圓形踩踏角度圖：{title}")

    @staticmethod
    def _point(center: QPointF, radius: float, angle: float, value: float) -> QPointF:
        theta = np.deg2rad(angle)
        scale = max(0.0, min(1.0, value / 100.0))
        return QPointF(center.x() + radius * scale * np.sin(theta),
                       center.y() - radius * scale * np.cos(theta))

    def _draw_curve(self, painter: QPainter, center: QPointF, radius: float,
                    angles: np.ndarray, values: np.ndarray, color: str) -> None:
        if len(angles) == 0:
            return
        path = QPainterPath()
        valid_points = [self._point(center, radius, angle, value)
                        for angle, value in zip(angles, values, strict=False)
                        if np.isfinite(value)]
        if len(valid_points) < 2:
            return
        path.moveTo(valid_points[0])
        for point in valid_points[1:]:
            path.lineTo(point)
        path.lineTo(valid_points[0])
        painter.setPen(QPen(QColor(color), 2.2))
        painter.drawPath(path)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = self.palette().color(QPalette.ColorRole.Base)
        text = self.palette().color(QPalette.ColorRole.Text)
        muted = self.palette().color(QPalette.ColorRole.Mid)
        painter.fillRect(self.rect(), background)

        title_rect = self.rect().adjusted(10, 8, -10, 0)
        painter.setPen(text)
        title_font = QFont(self.font())
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                         self.fontMetrics().elidedText(self.title, Qt.TextElideMode.ElideRight, title_rect.width()))

        bounds = self.rect().adjusted(28, 38, -28, -30)
        radius = max(10.0, min(bounds.width(), bounds.height()) / 2.0 - 12.0)
        center = QPointF(bounds.center().x(), bounds.center().y())
        painter.setPen(QPen(muted, 1))
        for fraction, label in ((.25, "25"), (.5, "50"), (.75, "75"), (1.0, "100")):
            painter.drawEllipse(center, radius * fraction, radius * fraction)
            painter.drawText(QPointF(center.x() + 4, center.y() - radius * fraction + 12), label)
        for angle in range(0, 360, 45):
            theta = np.deg2rad(angle)
            outer = QPointF(center.x() + radius * np.sin(theta), center.y() - radius * np.cos(theta))
            painter.drawLine(center, outer)
            label_point = QPointF(center.x() + (radius + 14) * np.sin(theta),
                                  center.y() - (radius + 14) * np.cos(theta))
            label_rect = QRectF(label_point.x() - 16, label_point.y() - 9, 32, 18)
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, f"{angle}°")
        self._draw_curve(painter, center, radius, self.start_angles, self.start_values, "#1684d7")
        self._draw_curve(painter, center, radius, self.end_angles, self.end_values, "#d13438")
        painter.setPen(QPen(QColor("#1684d7"), 3))
        painter.drawLine(10, self.height() - 13, 26, self.height() - 13)
        painter.setPen(text)
        painter.drawText(31, self.height() - 8, "前段")
        painter.setPen(QPen(QColor("#d13438"), 3))
        painter.drawLine(76, self.height() - 13, 92, self.height() - 13)
        painter.setPen(text)
        painter.drawText(97, self.height() - 8, "後段")

class EMGAnalysisImportWorker(QThread):
    completed = Signal(object, str)
    failed = Signal(str)
    def __init__(self, path: Path, mappings: list[SensorMapping], baselines: dict,subject: dict, settings: FilterSettings) -> None:
        super().__init__()
        self.path = path
        self.mappings = mappings
        self.baselines = baselines
        self.subject = subject
        self.settings = settings

    def run(self) -> None:
        try:
            times, columns, sample_rate = load_raw_emg_csv(self.path, limit_seconds=None)
            channel_columns: list[tuple[int, str]] = []
            for channel, mapping in enumerate(self.mappings):
                header = next((name for name in columns if matchesCsvColumn(mapping, name)), None)
                if header is not None:
                    channel_columns.append((channel, header))
            if not channel_columns:
                raise ValueError("CSV does not contain a mapped EMG channel.")

            session = new_session(self.subject, self.baselines, sample_rate)
            session["filter"] = asdict(self.settings)
            session["raw_files"] = {"emg": self.path.name}
            session["channels"] = {
                str(channel + 1): {"mapping_name": f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}",
                                   "label": mapping.chinese_name,
                                   "sensor_serial": "Imported CSV"}
                for channel, mapping in ((channel, self.mappings[channel]) for channel, _ in channel_columns)
            }
            frames = np.column_stack([columns[header] for _, header in channel_columns])
            preprocessor = EMGPreprocessor(len(channel_columns), sample_rate, self.settings)
            extractor = RMSFeatureExtractor(len(channel_columns), sample_rate)
            update_samples = max(1, round(sample_rate * 0.2))
            for start in range(0, len(frames), update_samples):
                end = min(len(frames), start + update_samples)
                rms_values = extractor.add(preprocessor.process(frames[start:end]))
                for compact_channel, (channel, _header) in enumerate(channel_columns):
                    mapping = self.mappings[channel]
                    record = self.baselines.get(str(channel + 1))
                    mapping_name = f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"
                    compatible = bool(record and record.get("mapping_name") == mapping_name
                                      and abs(float(record.get("sample_rate_hz", 0)) - sample_rate) <= 0.01
                                      and record.get("filter") == asdict(self.settings))
                    comparison = compare_rms(rms_values.get(compact_channel),
                                             record.get("rms_mv") if compatible else None)
                    if record is not None and not compatible:
                        comparison["quality"] = "基準不相容"
                    add_point(session, "emg", {
                        "time_s": float(times[end - 1]), "slot": channel + 1,
                        "rms_mv": rms_values.get(compact_channel), **comparison,})
            self.completed.emit(session, str(self.path))
        except (OSError, ValueError, TypeError) as exc:
            self.failed.emit(str(exc))

def baselineSettingsPath() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(location) / "emg_baseline_settings.json"

class SensorLegendToggle(QWidget):
    toggled = Signal(bool)
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._checked = False
        self.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self.title = QLabel(objectName="sensorLegendTitle")
        self.english = QLabel(objectName="sensorLegendEnglish")
        for label in (self.title, self.english):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(self.title)
        layout.addWidget(self.english)

    def setLabels(self, title: str, english: str) -> None:
        self.title.setText(title)
        self.english.setText(english)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, checked: bool) -> None:
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self.toggled.emit(checked)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            event.accept()
            return
        super().mousePressEvent(event)


class PowerBalanceRing(QWidget):
    def __init__(self, side: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.side = side
        self.balance: float | None = None
        self.power_w: float | None = None
        self.active_color = QColor("#107c10")
        self.track_color = QColor("#d1d1d1")
        self.text_color = QColor("#242424")
        # Keep the summary visual compact while retaining enough space for
        # the power and balance labels at normal desktop text scaling.
        self.setMinimumSize(145, 170)
        self.setMaximumSize(165, 185)
        self.setAccessibleName(f"{side}腳估計功率與平衡")

    def setValues(self, balance: float | None, power_w: float | None) -> None:
        self.balance = balance if balance is not None and 0 <= balance <= 100 else None
        self.power_w = power_w
        if self.balance is None or self.power_w is None:
            self.setAccessibleDescription("沒有有效的左右平衡資料。")
        else:
            self.setAccessibleDescription(f"平均估計功率 {self.power_w:.1f} W，平衡 {self.balance:.1f}%。")
        self.update()

    def setThemeColors(self, active: str, track: str, text: str) -> None:
        self.active_color = QColor(active)
        self.track_color = QColor(track)
        self.text_color = QColor(text)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        content = self.rect().adjusted(12, 12, -12, -12)
        diameter = min(content.width(), content.height())
        ring = QRectF(content.center().x() - diameter / 2, content.center().y() - diameter / 2,diameter, diameter,)
        pen = QPen(self.track_color, 18)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(ring)
        if self.balance is not None:
            pen.setColor(self.active_color)
            painter.setPen(pen)
            painter.drawArc(ring, 90 * 16, -round(self.balance * 3.6 * 16))
        painter.setPen(self.text_color)
        def label_rect(ratio: float) -> QRectF:
            return QRectF(ring.left(), ring.top() + diameter * ratio, diameter, 19)
        painter.drawText(label_rect(.20), Qt.AlignmentFlag.AlignHCenter, self.side)
        power_text = "--" if self.power_w is None else f"{self.power_w:.1f} W"
        painter.drawText(label_rect(.42), Qt.AlignmentFlag.AlignHCenter, power_text)
        balance_text = "無有效資料" if self.balance is None else f"平衡 {self.balance:.1f}%"
        painter.drawText(label_rect(.64), Qt.AlignmentFlag.AlignHCenter, balance_text)

class MappingEditorDialog(QDialog):
    def __init__(self, mappings: list[SensorMapping], active_slots: set[int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("編輯感測器／肌肉對應")
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        column_widths = (32, 55, 200, 200, 200)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)
        grid.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for column, title in enumerate(("Slot", "狀態", "中文肌肉名稱", "English EMG name", "縮寫")):
            label = QLabel(title, objectName="editorHeader")
            label.setFixedWidth(column_widths[column])
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(label, 0, column)
        self.edits: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        for row, mapping in enumerate(mappings, start=1):
            slot = QLabel(f"S{mapping.slot:02d}")
            slot.setFixedWidth(column_widths[0])
            slot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(slot, row, 0)
            state = QLabel("● 啟用" if mapping.slot in active_slots else "未啟用")
            state.setFixedWidth(column_widths[1])
            state.setAlignment(Qt.AlignmentFlag.AlignCenter)
            state.setStyleSheet("color: #16a34a; font-weight: 700;" if mapping.slot in active_slots else "color: #94a3b8;")
            grid.addWidget(state, row, 1)
            chinese, english, abbreviation = QLineEdit(mapping.chinese_name), QLineEdit(mapping.english_name), QLineEdit(mapping.abbreviation)
            for edit, width in zip((chinese, english, abbreviation), column_widths[2:], strict=True):
                edit.setFixedWidth(width)
                edit.setFixedHeight(28)
                edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(chinese, row, 2)
            grid.addWidget(english, row, 3)
            grid.addWidget(abbreviation, row, 4)
            self.edits.append((chinese, english, abbreviation))
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        buttons.button(QDialogButtonBox.Save).setText("儲存並套用")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


    def mappings(self) -> list[SensorMapping]:
        return [
            SensorMapping(slot, chinese.text().strip(), english.text().strip(), abbreviation.text().strip())
            for slot, (chinese, english, abbreviation) in enumerate(self.edits, 1) ]

    def accept(self) -> None:
        duplicates = duplicateMappingSlots(self.mappings())
        if duplicates:
            conflicts = "\n".join(f"{muscle.title()} ({side.upper() or '?' }): "+ ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items())
            QMessageBox.warning(self,"重複肌肉對應","同一側的同一肌肉只能對應一個感測器：\n" + conflicts,)
            return
        super().accept()


class BaselineSettingsDialog(QDialog):
    """Show saved baseline RMS values and configure deviation thresholds."""

    def __init__(self, mappings: list[SensorMapping], manager: BaselineManager,
                 settings: FilterSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("基準值與判定設定")
        self.resize(840, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        description_row = QHBoxLayout()
        description_row.addWidget(QLabel("顯示此電腦已儲存的基準 RMS；尚未匯入或量測的肌肉會顯示「--」。",
                                         objectName="muted"), 1)
        self.import_requested = False
        import_button = QPushButton("匯入基準 CSV")
        FluentThemeManager.set_property(import_button, "fluentAppearance", "primary")
        import_button.clicked.connect(self.request_import)
        description_row.addWidget(import_button)
        layout.addLayout(description_row)

        self.table = QTableWidget(0, 6)
        table = self.table
        table.setHorizontalHeaderLabels(("肌肉／側別", "基準 RMS", "來源", "取樣率", "濾波", "狀態"))
        table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        rows = [mapping for mapping in mappings
                if mapping.english_name.strip().casefold() != "unassigned"
                or mapping.slot in manager.records]
        self._row_slots = []
        self._initial_baselines: dict[int, float | None] = {}
        table.setRowCount(len(rows))
        for row, mapping in enumerate(rows):
            self._row_slots.append(mapping.slot)
            record = manager.records.get(mapping.slot)
            self._initial_baselines[mapping.slot] = record.rms_mv if record is not None else None
            identity = f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"
            if record is None:
                baseline, source, sample_rate, filter_text, status = "--", "--", "--", "--", "尚無基準"
            else:
                baseline = f"{record.rms_mv:.4f} mV"
                source = {"imported_csv": "匯入 CSV", "measured": "量測", "manual": "手動"}.get(
                    record.source, record.source)
                sample_rate = f"{record.sample_rate_hz:.1f} Hz"
                notch = record.filter.get("notch_hz")
                filter_text = (f"{record.filter.get('highpass_hz', '--')}–"
                               f"{record.filter.get('lowpass_hz', '--')} Hz"
                               + (f"；陷波 {notch:g} Hz" if notch else ""))
                if record.mapping_name != identity:
                    status = "肌肉對應已變更"
                elif record.filter != asdict(settings):
                    status = "濾波設定已變更"
                else:
                    status = "可用"
            values = (f"S{mapping.slot:02d} {mapping.chinese_name}（{mapping.abbreviation or mapping.english_name}）",
                      baseline, source, sample_rate, filter_text, status)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column != 1:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                else:
                    item.setToolTip("雙擊後可輸入新的基準 RMS（mV）。")
                table.setItem(row, column, item)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, table.columnCount()):
            table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(table, 1)

        threshold_frame = QFrame(objectName="panel")
        threshold_layout = QGridLayout(threshold_frame)
        threshold_layout.setContentsMargins(12, 10, 12, 10)
        threshold_layout.addWidget(QLabel("偏離判定", objectName="sectionTitle"), 0, 0, 1, 4)
        self.warning_spin = QDoubleSpinBox()
        self.warning_spin.setRange(0.1, 1000.0)
        self.warning_spin.setDecimals(1)
        self.warning_spin.setSuffix(" %")
        self.warning_spin.setValue(manager.warning_percent)
        self.focus_spin = QDoubleSpinBox()
        self.focus_spin.setRange(0.2, 1000.0)
        self.focus_spin.setDecimals(1)
        self.focus_spin.setSuffix(" %")
        self.focus_spin.setValue(manager.focus_percent)
        self.warning_spin.valueChanged.connect(self._update_focus_minimum)
        self._update_focus_minimum(self.warning_spin.value())
        threshold_row = QHBoxLayout()
        threshold_row.setSpacing(6)
        threshold_row.addWidget(QLabel("警告偏離"))
        threshold_row.addWidget(self.warning_spin)
        threshold_row.addSpacing(16)
        threshold_row.addWidget(QLabel("重點察看偏離"))
        threshold_row.addWidget(self.focus_spin)
        threshold_row.addStretch()
        threshold_layout.addLayout(threshold_row, 1, 0, 1, 4)
        threshold_layout.addWidget(QLabel("偏離提示固定啟用；偏離以目前 RMS 相對於基準 RMS 的百分比計算。", objectName="muted"),
                                   2, 0, 1, 4)
        layout.addWidget(threshold_frame)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("儲存設定")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_focus_minimum(self, warning: float) -> None:
        self.focus_spin.setMinimum(warning + 0.1)

    def request_import(self) -> None:
        self.import_requested = True
        self.accept()

    def values(self) -> tuple[float, float]:
        return self.warning_spin.value(), self.focus_spin.value()

    def baseline_changes(self) -> dict[int, float]:
        changes: dict[int, float] = {}
        for row, slot in enumerate(self._row_slots):
            text = self.table.item(row, 1).text().replace("mV", "").strip()
            if text in {"", "--"}:
                continue
            try:
                value = float(text)
            except ValueError as exc:
                raise ValueError(f"S{slot:02d} 的基準 RMS 必須是數字。") from exc
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"S{slot:02d} 的基準 RMS 必須大於 0。")
            if value != self._initial_baselines[slot]:
                changes[slot] = value
        return changes

    def accept(self) -> None:
        try:
            self.baseline_changes()
        except ValueError as exc:
            QMessageBox.warning(self, "基準值格式無效", str(exc))
            return
        super().accept()


class PedalSummaryDialog(QDialog):
    def __init__(self, summary: dict[str, float | int | None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("本次騎乘摘要")
        self.resize(480, 310)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("本次踏板功率／踏頻資料", objectName="sectionTitle"))
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText("\n".join(pedal_summary_lines(summary)))
        layout.addWidget(details, 1)
        note = QLabel("左右功率由總功率與踏板回報的左右平衡估算，並非 Power Phase。", objectName="muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

class EmgRehabilitationWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("EMG Cycling Monitor")
        self.resize(1500, 920)
        self.streamWorker: StreamWorker | None = None
        self.testWorker: ConnectionWorker | None = None
        self.emgTested = False
        self.trainerWorker = TrainerWorker()
        self.trainerIsConnected = False
        self.pedalWorker = PedalWorker()
        self.pedalIsConnected = False
        self.bluetoothScanWorker: CyclingDeviceScanWorker | None = None
        self.trainerBluetoothReady = False
        self.pedalBluetoothReady = False
        self.initialBluetoothScanStarted = False
        self.selectedScannedDevice: tuple[str, str] | None = None
        self.settings = QSettings("EMG Rehab System", "EMG Rehabilitation Desktop")
        self.goproWorker: GoProCommandWorker | None = None
        self.goproDownloadWorker: GoProDownloadWorker | None = None
        self.goproMediaListWorker: GoProMediaListWorker | None = None
        self.goproWifiWorker: GoProWifiConnectWorker | None = None
        self.goproWifiSsid: str | None = None
        self.goproWifiPassword: str | None = None
        self.goproWifiNextAction: str | None = None
        self.goproDownloadStartedAt = 0.0
        self.goproDownloadName = "影片"
        self.goproAddress = str(self.settings.value("gopro/address", ""))
        self.goproIdentifier = str(self.settings.value("gopro/identifier", ""))
        # BLE commands are short-lived; this records the latest verified
        # camera-control state rather than a permanently open OS connection.
        self.goproIsConnected = False
        self.goproRecordingStarted = False
        # The camera acknowledgement and command-completed signal can arrive
        # in either order, so retain the acknowledgement independently.
        self.goproStartConfirmed = False
        self.goproAwaitingStartConfirmation = False
        self.goproStopPending = False
        self.goproStopRequested = False
        self.savedPedalAddress = str(self.settings.value("pedal/address", ""))
        self.pedalScanInProgress = False
        self.histories = [deque(maxlen=int(SAMPLE_RATE * 20)) for _ in range(CHANNEL_COUNT)]
        self.mappings = loadMappings(MAPPING_PATH)
        self.sensorChecks: list[SensorLegendToggle] = []
        self.sensorIcons: list[QLabel] = []
        self.sensorRows: list[QFrame] = []
        self.sensorActive = [False] * CHANNEL_COUNT
        self.sensorSerials: dict[int, str] = {}
        self.curves: dict[int, tuple[pg.PlotItem, pg.PlotDataItem, pg.TextItem]] = {}
        self.collectionSummary: tuple[int, float, float] | None = None
        self.collectionError: str | None = None
        self.collectionStoppedEarly = False
        self.collectionOutput: Path | None = None
        self.collectionSubject = SubjectInfo()
        self.infoRecorder: CollectionInfoRecorder | None = None
        self.infoOutput: Path | None = None
        self.trainerRecorder: TrainerCsvRecorder | None = None
        self.trainerOutput: Path | None = None
        self.pedalRecorder: PedalCsvRecorder | None = None
        self.pedalOutput: Path | None = None
        self.latestPedalRecords: list[dict] = []
        self.summaryRecords: list[dict] = []
        self.pedalSummarySource = "current"
        self.rideSession: dict | None = None
        self.latestRideSession: dict | None = None
        self.collectedRideSession: dict | None = None
        self.referenceRideSession: dict | None = None
        self.rideStartedAt = 0.0
        self.lastEmgAnalysisAt = 0.0
        self.databaseWriter: DatabaseWriter | None = None
        self.databaseWriters: set[DatabaseWriter] = set()
        self.databaseAggregator: SecondAggregator | None = None
        self.databaseTestId: str | None = None
        self.databaseLastTestId: str | None = None
        self.databaseDiscardAfterWriter: dict[DatabaseWriter, str] = {}
        self.databaseStartedAt: datetime | None = None
        self.archiveWorkers: set[ArchiveWorker] = set()
        self.archiveLastTestId: str | None = None
        self.activeDatabaseConfig: DatabaseConfig | None = None
        self.discardCurrentCollection = False
        self.bikeOnlySession = False
        self.commandedNativeResistance: float | None = None
        self.currentSampleRate = SAMPLE_RATE
        # A calibration belongs to the current patient and measurement session.
        # Keep thresholds/settings, but never restore a previous patient's RMS.
        self.baselineManager = BaselineManager(
            baselineSettingsPath(), restore_records=False, persist_records=False,
        )
        self.baselineManager.thresholds_enabled = True
        self.filterSettings = FilterSettings()
        self.preprocessor: EMGPreprocessor | None = None
        self.rmsExtractor: RMSFeatureExtractor | None = None
        self.emgAnalysisImportWorker: EMGAnalysisImportWorker | None = None
        self.angleAnalysisWorker: AngleAnalysisWorker | None = None
        self.angleExportWorker: AngleExportWorker | None = None
        self.angleAnalysisResult: dict | None = None
        self.angleEmgPath: Path | None = None
        self.anglePedalPath: Path | None = None
        self.pendingAutoAngleAnalysis = False
        self.calibrationSession: CalibrationSession | None = None
        self.calibrationOnly = False
        self.calibrationRecording = False
        # Per-slot live comparison state drives the body-map research cue.
        self.muscleStatuses: dict[int, str] = {}
        self.baselineMetricLabels: list[QLabel] = []
        self.baselineStatusLabels: list[QLabel] = []
        self.lastTrainerDistanceMeters: int | None = None
        self.collectionDistanceBaselineMeters: int | None = None
        self.collectionDistanceMeters: int | None = None
        self.collectionLastTrainerDistanceMeters: int | None = None
        self.collectionDistanceUpdatedAt: float | None = None
        self.lastDistanceRejectAt = 0.0
        self.distanceSessionActive = False
        self.buildUi()
        self.loadArchivePatients()
        if hasattr(QApplication.instance().styleHints(), "colorSchemeChanged"):
            QApplication.instance().styleHints().colorSchemeChanged.connect(self.onSystemThemeChanged)
        self.applySummaryPlotTheme()
        self.connectTrainerSignals()
        self.trainerWorker.start()
        self.connectPedalSignals()
        self.pedalWorker.start()
        self.connectionStateTimer = QTimer(self)
        self.connectionStateTimer.setInterval(500)
        self.connectionStateTimer.timeout.connect(self.refreshConnectionStates)
        self.connectionStateTimer.start()
        self.analysisTimer = QTimer(self)
        self.analysisTimer.setInterval(1000)
        self.analysisTimer.timeout.connect(self.refreshLiveRideSummary)
        self.analysisTimer.start()
        self.setStreaming(False)
        self.setEmgStatus("尚未連線", "idle")
        self.setTrainerConnectionState(False)
        self.setPedalConnectionState(False)
        self.setGoProConnectionState(False)
        self.setEmgConnectionState(False)
        self.setDatabaseConnectionState(False)

    def buildUi(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 12)
        self.tabs = QTabWidget()
        statusCorner = QWidget()
        header = QHBoxLayout(statusCorner)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.trainerConnectionLabel = QLabel(objectName="connectionIndicator")
        self.pedalConnectionLabel = QLabel(objectName="connectionIndicator")
        self.goproConnectionLabel = QLabel(objectName="connectionIndicator")
        self.emgConnectionLabel = QLabel(objectName="connectionIndicator")
        self.databaseConnectionLabel = QLabel(objectName="connectionIndicator")
        header.addWidget(self.trainerConnectionLabel)
        header.addWidget(self.pedalConnectionLabel)
        header.addWidget(self.goproConnectionLabel)
        header.addWidget(self.emgConnectionLabel)
        header.addWidget(self.databaseConnectionLabel)
        self.tabs.setCornerWidget(statusCorner, Qt.Corner.TopRightCorner)
        self.resistanceTab = self.createResistanceTab()
        self.trainerTab = self.createTrainerTab()
        self.sessionDevicesPanel = self.createSessionDevicesPanel()
        self.collectionTab = self.createCollectionTab()
        self.summaryTab = self.createSummaryTab()
        self.settingsTab = self.createSettingsTab()
        self.startButton.setText("開始收集資料")
        self.tabs.addTab(self.settingsTab, "設定")
        self.tabs.addTab(self.collectionTab, "收集資料")
        # Analysis and historical comparison are available in the Flask portal.
        self.tabs.currentChanged.connect(self.onMainTabChanged)
        outer.addWidget(self.tabs, 1)

    def createPedalPanel(self) -> QWidget:
        panel = QFrame(objectName="panel")
        layout = QGridLayout(panel)
        layout.addWidget(QLabel("踏板", objectName="sectionTitle"), 0, 0)
        self.pedalDevices = QComboBox()
        self.pedalDevices.setEditable(True)
        self.pedalDevices.setPlaceholderText("掃描所有藍牙裝置，或輸入 Bluetooth MAC 位址")
        layout.addWidget(self.pedalDevices, 0, 1)
        self.scanPedalButton = QPushButton("使用上方掃描")
        self.connectPedalButton = QPushButton("連線", objectName="primary")
        layout.addWidget(self.scanPedalButton, 0, 2)
        layout.addWidget(self.connectPedalButton, 0, 3)
        self.pedalStatus = QLabel("踏板：尚未連線", objectName="status")
        self.pedalTelemetryLabel = QLabel("踏板資料：等待連線", objectName="muted")
        layout.addWidget(self.pedalStatus, 1, 0, 1, 4)
        layout.addWidget(self.pedalTelemetryLabel, 2, 0, 1, 4)
        self.scanPedalButton.clicked.connect(self.scanCyclingDevices)
        self.connectPedalButton.clicked.connect(self.connectPedal)
        return panel

    def createSessionDevicesPanel(self) -> QFrame:
        panel = QFrame()
        FluentThemeManager.set_property(panel, "fluentRole", "surface")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 10, 14, 10)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignmentFlag.AlignLeft)
        row.addWidget(QLabel("測試項目", objectName="sectionTitle"))
        row.addSpacing(28)
        for text, attribute in (("EMG", "useEmgCheck"), ("阻力計", "useTrainerCheck"), ("踏板", "usePedalCheck"), ("攝影機", "useGoProCheck")):
            check = QCheckBox(text)
            check.setChecked(True)
            setattr(self, attribute, check)
            row.addWidget(check)
            row.addSpacing(14)
        row.addStretch(1)
        layout.addLayout(row)
        return panel

    def createSettingsTab(self) -> QWidget:
        tab = QWidget(objectName="settingsPage")
        page = QVBoxLayout(tab)
        page.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(6)

        heading = QFrame()
        FluentThemeManager.set_property(heading, "fluentRole", "surface")
        headingLayout = QVBoxLayout(heading)
        headingLayout.setContentsMargins(20, 18, 20, 18)
        title = QLabel("測試設定", objectName="title")
        subtitle = QLabel("先設定受試者與本次設備，再掃描並連線騎乘裝置。", objectName="muted")
        headingLayout.addWidget(title)
        headingLayout.addWidget(subtitle)
        heading.setParent(None)
        heading.deleteLater()

        subject = QFrame()
        FluentThemeManager.set_property(subject, "fluentRole", "surface")
        subjectLayout = QGridLayout(subject)
        subjectLayout.setContentsMargins(14, 12, 14, 12)
        subjectLayout.setHorizontalSpacing(12)
        subjectLayout.setVerticalSpacing(10)
        subjectLayout.addWidget(QLabel("受試者資料", objectName="sectionTitle"), 0, 0, 1, 6)
        self.patientIdEdit = QLineEdit()
        self.patientIdEdit.setReadOnly(True)
        self.patientIdEdit.setPlaceholderText("請從網站建立並選擇病人")
        self.patientNameEdit = QLineEdit()
        self.patientNameEdit.setReadOnly(True)
        self.genderCombo = QComboBox()
        self.genderCombo.addItems(("未指定", "男性", "女性", "其他"))
        self.genderCombo.setEnabled(False)
        self.archivePatientCombo = QComboBox()
        self.archivePatientCombo.setMinimumWidth(220)
        self.archivePatientCombo.currentIndexChanged.connect(self.archivePatientSelected)
        self.reloadPatientsButton = QPushButton("重新整理病人")
        self.reloadPatientsButton.clicked.connect(self.loadArchivePatients)
        subjectLayout.addWidget(QLabel("選擇病人"), 0, 6)
        subjectLayout.addWidget(self.archivePatientCombo, 0, 7)
        subjectLayout.addWidget(self.reloadPatientsButton, 0, 8)
        subjectLayout.addWidget(QLabel("ID"), 1, 0)
        subjectLayout.addWidget(self.patientIdEdit, 1, 1)
        subjectLayout.addWidget(QLabel("姓名"), 1, 2)
        subjectLayout.addWidget(self.patientNameEdit, 1, 3)
        subjectLayout.addWidget(QLabel("性別"), 1, 4)
        subjectLayout.addWidget(self.genderCombo, 1, 5)
        subjectLayout.addWidget(QLabel("\u8cc7\u6599\u8f38\u51fa"), 1, 6)
        self.pathEdit = QLineEdit()
        self.pathEdit.setPlaceholderText("\u8acb\u9078\u64c7 CSV \u8207\u651d\u5f71\u6a5f\u5f71\u7247\u7684\u8f38\u51fa\u8cc7\u6599\u593e")
        self.pathEdit.setText(str(self.settings.value("output/directory", "")).strip())
        self.browseButton = QPushButton("\u9078\u64c7\u8cc7\u6599\u593e")
        self.browseButton.setToolTip("\u9078\u64c7 CSV \u8207\u651d\u5f71\u6a5f\u5f71\u7247\u5171\u7528\u7684\u8f38\u51fa\u8cc7\u6599\u593e")
        subjectLayout.addWidget(self.pathEdit, 1, 7)
        subjectLayout.addWidget(self.browseButton, 1, 8)
        self.patientIdEdit.setFixedWidth(160)
        self.patientNameEdit.setFixedWidth(180)
        self.genderCombo.setFixedWidth(100)
        subjectLayout.setColumnStretch(7, 1)
        self.browseButton.clicked.connect(self.chooseOutput)
        self.pathEdit.editingFinished.connect(self.saveOutputDirectory)
        layout.addWidget(subject)

        connectionColumns = QHBoxLayout()
        connectionColumns.setSpacing(10)
        leftConnectionColumn = QVBoxLayout()
        leftConnectionColumn.setSpacing(10)
        rightConnectionColumn = QVBoxLayout()
        rightConnectionColumn.setSpacing(10)
        connectionColumns.addLayout(leftConnectionColumn, 1)
        connectionColumns.addLayout(rightConnectionColumn, 1)
        layout.addLayout(connectionColumns)

        usageGuide = QFrame()
        self.usageGuide = usageGuide
        FluentThemeManager.set_property(usageGuide, "fluentRole", "surface")
        usageGuideLayout = QVBoxLayout(usageGuide)
        usageGuideLayout.setContentsMargins(14, 12, 14, 12)
        usageGuideLayout.setSpacing(8)
        usageGuideLayout.addWidget(QLabel("\u64cd\u4f5c\u8aaa\u660e", objectName="sectionTitle"))
        usageGuideText = QLabel(
            "1. \u586b\u5beb\u53d7\u8a66\u8005\u8cc7\u6599\u8207\u8cc7\u6599\u8f38\u51fa\u8cc7\u6599\u593e\n"
            "2. \u6307\u5b9a\u963b\u529b\u8a08\u3001\u8e0f\u677f\u8207\u651d\u5f71\u6a5f\u88dd\u7f6e\n"
            "3. \u6e2c\u8a66 EMG \u3001\u651d\u5f71\u6a5f\u8207\u8cc7\u6599\u5eab\u9023\u7dda\n"
            "4. \u78ba\u8a8d\u5404\u88dd\u7f6e\u8207\u8cc7\u6599\u5eab\u7684\u9023\u7dda\u72c0\u614b\n"
            "5. \u5b8c\u6210\u5f8c\u524d\u5f80\u300c\u6536\u96c6\u8cc7\u6599\u300d\u958b\u59cb\u6e2c\u8a66"
        , objectName="muted")
        usageGuideText.setWordWrap(True)
        usageGuideLayout.addWidget(usageGuideText)
        usageGuideLayout.addStretch(1)
        leftConnectionColumn.addWidget(usageGuide)

        bluetooth = QFrame()
        FluentThemeManager.set_property(bluetooth, "fluentRole", "surface")
        bluetoothLayout = QVBoxLayout(bluetooth)
        bluetoothLayout.setContentsMargins(14, 12, 14, 12)
        bluetoothLayout.setSpacing(8)
        bluetoothHeader = QHBoxLayout()
        bluetoothHeader.addWidget(QLabel("騎乘裝置藍牙", objectName="sectionTitle"))
        bluetoothHeader.addStretch()
        bluetoothHeader.itemAt(0).widget().setText("裝置連線")
        self.scanTrainerButton.setText("掃描所有藍牙裝置")
        FluentThemeManager.set_property(self.scanTrainerButton, "fluentAppearance", "primary")
        self.scanTrainerButton.setText("重新掃描")
        bluetoothLayout.addLayout(bluetoothHeader)
        bluetoothLayout.addWidget(self.trainerTab)
        self.selectedScannedDeviceLabel = QLabel("未選擇裝置", objectName="muted")
        self.selectedScannedDeviceLabel.setMaximumWidth(180)
        self.assignTrainerButton = QPushButton("設定為阻力計")
        self.assignPedalButton = QPushButton("設定為踏板")
        self.assignGoProButton = QPushButton("設定為攝影機")
        self.assignTrainerButton.setEnabled(False)
        self.assignPedalButton.setEnabled(False)
        self.assignGoProButton.setEnabled(False)
        FluentThemeManager.set_property(self.assignTrainerButton, "fluentAppearance", "dark")
        FluentThemeManager.set_property(self.assignPedalButton, "fluentAppearance", "dark")
        FluentThemeManager.set_property(self.assignGoProButton, "fluentAppearance", "dark")
        self.assignTrainerButton.clicked.connect(lambda: self.assignSelectedScannedDevice("阻力計"))
        self.assignPedalButton.clicked.connect(lambda: self.assignSelectedScannedDevice("踏板"))
        self.assignGoProButton.clicked.connect(lambda: self.assignSelectedScannedDevice("攝影機"))

        emg = QFrame()
        FluentThemeManager.set_property(emg, "fluentRole", "surface")
        emgLayout = QGridLayout(emg)
        emgLayout.setContentsMargins(14, 12, 14, 12)
        emgLayout.setHorizontalSpacing(12)
        emgLayout.setVerticalSpacing(10)
        emgLayout.addWidget(QLabel("\u0045\u004d\u0047 \u611f\u6e2c\u5668\u5c0d\u61c9\u9023\u7dda", objectName="sectionTitle"), 0, 0, 1, 4)
        emgLayout.addWidget(QLabel("Server"), 1, 0)
        emgLayout.addWidget(self.hostEdit, 1, 1)
        self.testButton.setText("測試 EMG 連線")
        FluentThemeManager.set_property(self.testButton, "fluentAppearance", "dark")
        self.testButton.setFixedWidth(180)
        emgLayout.addWidget(self.testButton, 1, 2)
        self.saveInlineMappingsButton = QPushButton("\u5132\u5b58\u611f\u6e2c\u5668\u5c0d\u61c9")
        self.saveInlineMappingsButton.setFixedWidth(180)
        FluentThemeManager.set_property(self.saveInlineMappingsButton, "fluentAppearance", "dark")
        self.saveInlineMappingsButton.clicked.connect(self.saveInlineMappings)
        emgLayout.addWidget(self.saveInlineMappingsButton, 1, 3)
        emgLayout.setColumnStretch(1, 1)

        inlineMappingPanel = QFrame()
        inlineMappingLayout = QVBoxLayout(inlineMappingPanel)
        inlineMappingLayout.setContentsMargins(0, 8, 0, 0)
        inlineMappingLayout.setSpacing(6)
        self.inlineMappingEdits: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        mappingContent = QWidget()
        mappingGrid = QGridLayout(mappingContent)
        mappingGrid.setContentsMargins(0, 0, 0, 0)
        mappingGrid.setHorizontalSpacing(6)
        mappingGrid.setVerticalSpacing(4)
        for column, title in enumerate(("Slot", "\u72c0\u614b", "\u90e8\u4f4d", "English EMG name", "\u7e2e\u5beb")):
            header = QLabel(title, objectName="editorHeader")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            mappingGrid.addWidget(header, 0, column)
        for row, mapping in enumerate(self.mappings, start=1):
            slot = QLabel(f"S{mapping.slot:02d}")
            slot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            mappingGrid.addWidget(slot, row, 0)
            state = QLabel("\u5df2\u555f\u7528" if self.sensorActive[mapping.slot - 1] else "\u5f85\u9023\u7dda")
            state.setAlignment(Qt.AlignmentFlag.AlignCenter)
            FluentThemeManager.set_property(state, "fluentSeverity", "success" if self.sensorActive[mapping.slot - 1] else "info")
            mappingGrid.addWidget(state, row, 1)
            chinese = QLineEdit(mapping.chinese_name)
            english = QLineEdit(mapping.english_name)
            abbreviation = QLineEdit(mapping.abbreviation)
            mappingGrid.addWidget(chinese, row, 2)
            mappingGrid.addWidget(english, row, 3)
            mappingGrid.addWidget(abbreviation, row, 4)
            self.inlineMappingEdits.append((chinese, english, abbreviation))
        mappingGrid.setColumnStretch(2, 1)
        mappingGrid.setColumnStretch(3, 2)
        mappingGrid.setColumnStretch(4, 1)
        inlineMappingLayout.addWidget(mappingContent)
        emgLayout.addWidget(inlineMappingPanel, 3, 0, 1, 4)
        leftConnectionColumn.addWidget(emg)

        gopro = QFrame()
        FluentThemeManager.set_property(gopro, "fluentRole", "surface")
        goproLayout = QGridLayout(gopro)
        goproLayout.setContentsMargins(14, 12, 14, 12)
        goproLayout.setHorizontalSpacing(12)
        goproLayout.setVerticalSpacing(8)
        goproLayout.addWidget(QLabel("攝影機連線", objectName="sectionTitle"), 0, 0, 1, 4)
        self.goproTestButton = QPushButton("配對並測試攝影機")
        self.goproTestButton.setFixedWidth(180)
        FluentThemeManager.set_property(self.goproTestButton, "fluentAppearance", "dark")
        self.goproDisconnectButton = QPushButton("中斷攝影機")
        self.goproDisconnectButton.setEnabled(False)
        self.goproStatusLabel = QLabel("尚未測試；請先讓相機進入配對模式，測試不會開始錄影", objectName="muted")
        self.goproStatusLabel.setWordWrap(True)
        goproLayout.addWidget(self.goproTestButton, 1, 0)
        goproLayout.addWidget(self.goproDisconnectButton, 1, 1)
        goproLayout.addWidget(self.goproStatusLabel, 1, 2, 1, 2)
        self.goproDownloadButton = QPushButton("下載最新影片")
        self.goproDownloadButton.setToolTip("自動連上攝影機 Wi-Fi，下載最新 MP4")
        FluentThemeManager.set_property(self.goproDownloadButton, "fluentAppearance", "primary")
        self.goproChooseVideoButton = QPushButton("選擇影片下載")
        self.goproDownloadStatusLabel = QLabel("配對成功後可自動連上攝影機 Wi-Fi 並下載", objectName="muted")
        self.goproDownloadStatusLabel.setWordWrap(True)
        goproLayout.addWidget(self.goproDownloadButton, 2, 0)
        goproLayout.addWidget(self.goproChooseVideoButton, 2, 1)
        goproLayout.addWidget(self.goproDownloadStatusLabel, 2, 2, 1, 2)
        goproLayout.setColumnStretch(2, 1)
        self.goproTestButton.clicked.connect(self.testGoProConnection)
        self.goproDisconnectButton.clicked.connect(self.disconnectGoPro)
        self.goproDownloadButton.clicked.connect(self.downloadLatestGoProVideo)
        self.goproChooseVideoButton.clicked.connect(self.chooseGoProVideoToDownload)
        rightConnectionColumn.addWidget(gopro)

        # The scan list belongs to the device connection workflow.  Keep it in
        # the same panel so choosing a device and connecting it are adjacent.
        scanResultsLayout = QVBoxLayout()
        scanResultsLayout.setContentsMargins(0, 6, 0, 0)
        scanResultsLayout.setSpacing(4)
        scanResultsLayout.addWidget(QLabel("掃描到的裝置", objectName="sectionTitle"))
        scanResultsLayout.addWidget(QLabel("為每個裝置選擇用途，指定後會套用至上方連線選單。", objectName="muted"))
        scanResultsLayout.itemAt(0).widget().hide()
        scanResultsLayout.itemAt(1).widget().hide()
        scanResultsHeader = QHBoxLayout()
        scanResultsHeader.addWidget(QLabel("\u6383\u63cf\u5230\u7684\u88dd\u7f6e", objectName="sectionTitle"))
        scanResultsHeader.addStretch()
        scanResultsLayout.addLayout(scanResultsHeader)
        scanAssignmentRow = QHBoxLayout()
        scanAssignmentRow.addWidget(QLabel("\u5df2\u9078\uff1a", objectName="muted"))
        scanAssignmentRow.addWidget(self.selectedScannedDeviceLabel)
        scanAssignmentRow.addStretch()
        FluentThemeManager.set_property(self.assignTrainerButton, "fluentAppearance", "success")
        FluentThemeManager.set_property(self.assignPedalButton, "fluentAppearance", "warning")
        FluentThemeManager.set_property(self.assignGoProButton, "fluentAppearance", "gopro")
        scanAssignmentRow.addWidget(self.assignTrainerButton)
        scanAssignmentRow.addWidget(self.assignPedalButton)
        scanAssignmentRow.addWidget(self.assignGoProButton)
        scanAssignmentRow.addWidget(self.scanTrainerButton)
        scanResultsLayout.addLayout(scanAssignmentRow)
        self.scanResultsScroll = QScrollArea()
        self.scanResultsScroll.setWidgetResizable(True)
        self.scanResultsScroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scanResultsScroll.setStyleSheet(
            "QScrollArea, QScrollArea::viewport { border: 0; background: transparent; }"
        )
        self.scanResultsScroll.setMinimumHeight(180)
        self.scanResultsScroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scanResultsContent = QWidget()
        FluentThemeManager.set_property(scanResultsContent, "fluentRole", "scanResultsContent")
        self.scanResultsLayout = QVBoxLayout(scanResultsContent)
        self.scanResultsLayout.setContentsMargins(0, 0, 0, 0)
        self.scanResultsLayout.setSpacing(3)
        self.scanResultsLayout.addStretch()
        self.scanResultsScroll.setWidget(scanResultsContent)
        scanResultsLayout.addWidget(self.scanResultsScroll, 1)
        bluetoothLayout.addLayout(scanResultsLayout)
        rightConnectionColumn.addWidget(bluetooth, 1)

        database = QFrame()
        FluentThemeManager.set_property(database, "fluentRole", "surface")
        databaseLayout = QVBoxLayout(database)
        databaseLayout.setContentsMargins(14, 12, 14, 12)
        databaseLayout.setSpacing(6)
        databaseLayout.addWidget(QLabel("SQL Server 資料庫", objectName="sectionTitle"))
        self.databaseStatusLabel = QLabel("填入帳密後測試連線，即可載入資料庫。", objectName="muted")
        databaseLayout.addWidget(self.databaseStatusLabel)
        self.databaseServerEdit = QLineEdit(default_config().server)
        self.databaseUserEdit = QLineEdit(default_config().username)
        self.databasePasswordEdit = QLineEdit()
        self.databasePasswordEdit.setText(default_config().password)
        self.databasePasswordEdit.setEchoMode(QLineEdit.EchoMode.Password)
        self.databaseServerEdit.setReadOnly(True)
        self.databaseUserEdit.setReadOnly(True)
        self.databasePasswordEdit.setReadOnly(True)
        self.databaseNameCombo = QComboBox()
        self.databaseNameCombo.addItem(default_config().database)
        self.databaseNameCombo.setEnabled(False)
        self.databaseNameCombo.setPlaceholderText("先測試連線以載入資料庫")
        self.databaseTestButton = QPushButton("測試連線")
        FluentThemeManager.set_property(self.databaseTestButton, "fluentAppearance", "dark")
        self.databaseServerEdit.setFixedWidth(160)
        self.databaseUserEdit.setFixedWidth(130)
        self.databasePasswordEdit.setFixedWidth(130)
        self.databaseTestButton.setFixedWidth(180)
        credentialsRow = QHBoxLayout()
        credentialsRow.setSpacing(6)
        credentialsRow.addWidget(QLabel("Server"))
        credentialsRow.addWidget(self.databaseServerEdit)
        credentialsRow.addWidget(QLabel("\u5e33\u865f"))
        credentialsRow.addWidget(self.databaseUserEdit)
        credentialsRow.addWidget(QLabel("\u5bc6\u78bc"))
        credentialsRow.addWidget(self.databasePasswordEdit)
        credentialsRow.addWidget(self.databaseTestButton)
        credentialsRow.addStretch()
        databaseLayout.addLayout(credentialsRow)
        databaseRow = QHBoxLayout()
        databaseRow.setSpacing(6)
        databaseRow.addWidget(QLabel("資料庫"))
        databaseRow.addWidget(self.databaseNameCombo, 1)
        databaseLayout.addLayout(databaseRow)
        databaseLayout.removeWidget(self.databaseStatusLabel)
        self.databaseStatusLabel.setWordWrap(True)
        databaseLayout.addWidget(self.databaseStatusLabel)
        self.databaseTestButton.clicked.connect(self.testDatabaseConnection)
        self.databaseServerEdit.textEdited.connect(self.databaseConnectionEdited)
        self.databaseUserEdit.textEdited.connect(self.databaseConnectionEdited)
        self.databasePasswordEdit.textEdited.connect(self.databaseConnectionEdited)
        self.databaseNameCombo.currentTextChanged.connect(self.databaseConnectionEdited)
        usageGuide.setFixedHeight(database.sizeHint().height())
        rightConnectionColumn.insertWidget(0, database)

        resistance = QFrame()
        FluentThemeManager.set_property(resistance, "fluentRole", "surface")
        resistanceLayout = QVBoxLayout(resistance)
        resistanceLayout.setContentsMargins(14, 12, 14, 12)
        resistanceLayout.addWidget(QLabel("阻力控制", objectName="sectionTitle"))

        eventLogPanel = QFrame(objectName="eventLogPanel")
        FluentThemeManager.set_property(eventLogPanel, "fluentRole", "surface")
        eventLogPanel.setFixedHeight(110)
        eventLayout = QVBoxLayout(eventLogPanel)
        eventLayout.setContentsMargins(10, 4, 10, 4)
        eventLayout.addWidget(QLabel("事件紀錄", objectName="sectionTitle"))
        self.eventLog = QPlainTextEdit(objectName="eventLog")
        self.eventLog.setReadOnly(True)
        self.eventLog.setMaximumBlockCount(200)
        self.eventLog.setPlaceholderText("連線與收集事件會顯示在這裡。")
        self.eventLog.setFixedHeight(78)
        eventLayout.addWidget(self.eventLog)
        layout.addStretch()
        scroll.setWidget(content)
        page.addWidget(scroll, 1)
        page.addWidget(eventLogPanel, 0)
        return tab

    def databaseConfig(self) -> DatabaseConfig:
        return default_config()

    def databaseConnectionEdited(self, *_args) -> None:
        self.activeDatabaseConfig = None
        self.databaseStatusLabel.setText("連線資料已變更，請重新測試連線")
        self.setDatabaseConnectionState(False)

    def testDatabaseConnection(self) -> None:
        try:
            config = self.databaseConfig()
            store = SqlServerStorage(config)
            try:
                name = store.test_connection()
            finally:
                store.close()
            Archive(config).list_patients()
        except Exception as exc:
            self.activeDatabaseConfig = None
            self.databaseStatusLabel.setText(f"連線失敗：{exc}")
            self.setDatabaseConnectionState(False)
            return
        self.activeDatabaseConfig = config
        self.databaseStatusLabel.setText(f"已連線：{name}；量測與封存資料表可用")
        self.loadArchivePatients()
        self.setDatabaseConnectionState(True)
        if hasattr(self, "databaseComparePatientCombo"):
            self.loadDatabaseComparisonPatients()

    def onMainTabChanged(self, index: int) -> None:
        if (self.tabs.widget(index) is self.comparisonTab
                and self.activeDatabaseConfig is not None
                and self.databaseComparePatientCombo.count() == 0):
            self.loadDatabaseComparisonPatients()

    def startDatabaseRecording(self) -> None:
        config = self.activeDatabaseConfig
        if config is None:
            self.appendLog("資料庫未寫入：請先在設定頁測試 SQL Server 連線")
            return
        self.databaseLastTestId = None
        self.databaseTestId = prefixed_id("TEST")
        self.databaseStartedAt = datetime.now()
        self.databaseAggregator = SecondAggregator(self.databaseTestId, self.databaseStartedAt)
        baselines: list[dict] = []
        for slot, record in self.baselineManager.records.items():
            mapping = self.mappings[slot - 1]
            baselines.append({"muscle_name": f"{mapping.chinese_name} ({mapping.abbreviation})", "rms_mv": record.rms_mv,
                "base_time": 10, "action_type": record.protocol or "基準測試", "yellow_pct": self.baselineManager.warning_percent,
                "red_pct": self.baselineManager.focus_percent, "quality_status": "VALID"})
        subject = self.collectionSubject
        payload = {"test_id": self.databaseTestId, "patient_id": subject.patientId, "name": subject.name,
            "sex": subject.gender, "action_type": "固定阻力騎乘", "test_time": None,
            "resistance_level": self.resistanceSpin.value(), "has_baseline": bool(baselines), "notes": "EMG Cycling Monitor",
            "started_at": self.databaseStartedAt, "baselines": baselines}
        self.databaseWriter = DatabaseWriter(config)
        writer = self.databaseWriter
        self.databaseWriters.add(writer)
        self.databaseWriter.connected.connect(lambda name: self.appendLog(f"資料庫已連線：{name}；TestID {self.databaseTestId}"))
        self.databaseWriter.failed.connect(lambda message: self.appendLog(f"資料庫寫入失敗（CSV 收集未受影響）：{message}"))
        self.databaseWriter.completed.connect(self.appendLog)
        self.databaseWriter.finished.connect(lambda: self.databaseWriterFinished(writer))
        self.databaseWriter.start()
        self.databaseWriter.enqueue("start", payload)

    def databaseAddSample(self, kind: str, time_s: float, sample: dict) -> None:
        if self.databaseAggregator is None or self.databaseWriter is None:
            return
        rows = self.databaseAggregator.add(kind, time_s, sample)
        if rows:
            self.databaseWriter.enqueue("rows", (kind, rows))

    def finishDatabaseRecording(self, result: dict | None) -> None:
        writer, aggregator, test_id = self.databaseWriter, self.databaseAggregator, self.databaseTestId
        self.databaseAggregator = None
        self.databaseTestId = None
        if writer is None or aggregator is None or test_id is None:
            return
        for kind, rows in aggregator.flush_all().items():
            if rows:
                writer.enqueue("rows", (kind, rows))
        pedal = (result or {}).get("pedal", {})
        trainer = (result or {}).get("trainer", {})
        def stat(group: dict, key: str, field: str = "mean"):
            value = group.get(key, {})
            return value.get(field) if isinstance(value, dict) else None
        avg_power = stat(pedal, "power_w") if stat(pedal, "power_w") is not None else stat(trainer, "power_watts")
        max_power = stat(pedal, "power_w", "max") if stat(pedal, "power_w", "max") is not None else stat(trainer, "power_watts", "max")
        avg_cadence = stat(pedal, "cadence_rpm") if stat(pedal, "cadence_rpm") is not None else stat(trainer, "cadence_rpm")
        distance_m = stat(trainer, "session_distance_meters", "max")
        elapsed = int(round(self.rideTime()))
        status = "失敗" if self.collectionError else ("中斷" if self.collectionStoppedEarly else "完成")
        writer.enqueue("finish", {"test_id": test_id, "test_time": elapsed, "status": status,
            "avg_power_w": avg_power, "max_power_w": max_power,
            "avg_cadence_rpm": avg_cadence, "distance_km": None if distance_m is None else distance_m / 1000,
            "left_balance_pct": stat(pedal, "left_balance_percent"),
            "right_balance_pct": None if stat(pedal, "left_balance_percent") is None else 100 - stat(pedal, "left_balance_percent"),
            "notes": self.collectionError or ""})
        self.databaseLastTestId = test_id
        writer.enqueue("stop")

    def databaseWriterFinished(self, writer: DatabaseWriter) -> None:
        """Retain every writer until its QThread has actually exited."""
        discard_test_id = self.databaseDiscardAfterWriter.pop(writer, None)
        self.databaseWriters.discard(writer)
        if self.databaseWriter is writer:
            self.databaseWriter = None
        writer.deleteLater()
        if discard_test_id:
            self.startDatabaseDiscardWorker(discard_test_id)

    def startDatabaseDiscardWorker(self, test_id: str) -> None:
        """Delete a completed test after its original writer has closed."""
        config = self.activeDatabaseConfig
        if config is None:
            self.appendLog("資料庫捨棄略過：本次沒有可用的資料庫連線設定")
            return
        writer = DatabaseWriter(config)
        self.databaseWriters.add(writer)
        writer.failed.connect(lambda message: self.appendLog(f"資料庫捨棄失敗：{message}"))
        writer.completed.connect(lambda _message: self.appendLog("資料庫：已刪除本次測試資料"))
        writer.finished.connect(lambda: self.databaseWriterFinished(writer))
        writer.start()
        writer.enqueue("discard", test_id)
        writer.enqueue("stop")

    def discardDatabaseRecording(self, active_writer: bool) -> None:
        """Discard active rows, or wait until a completed writer has drained."""
        test_id = self.databaseTestId or self.databaseLastTestId
        writer = self.databaseWriter
        self.databaseAggregator = None
        self.databaseTestId = None
        self.databaseLastTestId = None
        if not test_id:
            return
        if active_writer and writer is not None and writer.isRunning():
            writer.enqueue("discard", test_id)
            writer.enqueue("stop")
            return
        if writer is not None and writer.isRunning():
            self.databaseDiscardAfterWriter[writer] = test_id
            return
        self.startDatabaseDiscardWorker(test_id)

    def createResistanceTab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.resistanceRangeLabel = QLabel("連線阻力計後，可在此手動調整阻力。",objectName="muted",)
        self.resistanceRangeLabel.hide()
        self.decreaseResistanceButton = QPushButton("降低")
        self.decreaseResistanceButton.setAccessibleName("降低阻力")
        self.resistanceSpin = QSpinBox()
        self.resistanceSpin.setRange(0, 30)
        self.resistanceSpin.setValue(0)
        self.resistanceSpin.setSingleStep(1)
        self.resistanceSpin.setSuffix(" / 30")
        self.resistanceSpin.setReadOnly(True)
        self.resistanceSpin.setAccessibleName("阻力等級")
        self.increaseResistanceButton = QPushButton("提高")
        self.increaseResistanceButton.setAccessibleName("提高阻力")
        layout.addWidget(self.decreaseResistanceButton)
        layout.addWidget(self.resistanceSpin)
        layout.addWidget(self.increaseResistanceButton)
        self.nativeResistanceLabel = QLabel("阻力計狀態：尚未連線", objectName="muted")
        self.nativeResistanceLabel.hide()
        self.resistanceStepLevels = 1
        self.decreaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(-1))
        self.increaseResistanceButton.clicked.connect(lambda: self.adjustFixedResistance(1))
        self.decreaseResistanceButton.setEnabled(False)
        self.increaseResistanceButton.setEnabled(False)
        return tab

    def createSummaryTab(self) -> QWidget:
        tab = QWidget(objectName="summaryPage")
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        self.summarySourceLabel = QLabel("分析資料：EMG 未匯入 ｜ 踏板 未匯入", objectName="muted")
        heading.addWidget(self.summarySourceLabel)
        heading.addStretch()
        self.rideStateLabel = QLabel("等待騎乘", objectName="status")
        FluentThemeManager.set_property(self.rideStateLabel, "fluentSeverity", "info")
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.importPedalCsvButton = QPushButton("匯入踏板 CSV")
        self.importPedalCsvButton.setAccessibleName("匯入踏板 CSV")
        self.importPedalCsvButton.setToolTip("匯入踏板 CSV 供踏板摘要與角度分析共用；也可匯入校正角度 CSV 供角度分析")
        FluentThemeManager.set_property(self.importPedalCsvButton, "fluentAppearance", "primary")
        self.importEmgAnalysisButton = QPushButton("匯入原始 EMG")
        self.importEmgAnalysisButton.setToolTip("匯入原始 EMG CSV，重新產生 RMS 平均、Peak 與包絡線分析")
        FluentThemeManager.set_property(self.importEmgAnalysisButton, "fluentAppearance", "primary")
        self.angleSettingsButton = QPushButton("角度分析設定")
        self.angleSettingsButton.setToolTip("設定前後段比較時間，以及 EMG 與踏板的同步校正方式")
        self.angleSettingsButton.setAccessibleName("踩踏角度分析設定")
        FluentThemeManager.set_property(self.angleSettingsButton, "fluentAppearance", "dark")
        self.angleExportButton = QPushButton("匯出角度分析")
        self.angleExportButton.setAccessibleName("匯出踩踏角度分析結果")
        self.angleExportButton.setEnabled(False)
        FluentThemeManager.set_property(self.angleExportButton, "fluentAppearance", "dark")
        self.currentSessionSummaryButton = QPushButton("顯示本次收集")
        self.currentSessionSummaryButton.setEnabled(False)
        self.currentSessionSummaryButton.setToolTip("切換回本程式最新收集的踏板資料")
        FluentThemeManager.set_property(self.currentSessionSummaryButton, "fluentAppearance", "success")
        self.exportReportButton = QPushButton("匯出 HTML 報表")
        self.exportReportButton.setEnabled(False)
        FluentThemeManager.set_property(self.exportReportButton, "fluentAppearance", "success")
        actions.addWidget(self.currentSessionSummaryButton)
        actions.addWidget(self.importEmgAnalysisButton)
        actions.addWidget(self.importPedalCsvButton)
        actions.addWidget(self.angleSettingsButton)
        actions.addWidget(self.angleExportButton)
        actions.addWidget(self.exportReportButton)
        heading.addLayout(actions)
        heading.addSpacing(12)
        heading.addWidget(self.rideStateLabel)
        layout.addLayout(heading)
        metricStrip = QHBoxLayout()
        self.summaryMetricValues = {}
        for key, title, unit in (("duration", "收集時間", "s"), ("muscles", "肌肉", "部位")):
            frame = QFrame(objectName="metricCard")
            column = QVBoxLayout(frame)
            column.setContentsMargins(12, 8, 12, 8)
            column.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            value.setAccessibleName(title)
            valueRow = QHBoxLayout()
            valueRow.setContentsMargins(0, 0, 0, 0)
            valueRow.addWidget(value)
            valueRow.addStretch()
            if unit:
                valueRow.addWidget(QLabel(unit, objectName="metricUnit"))
            column.addLayout(valueRow)
            metricStrip.addWidget(frame, 1)
            self.summaryMetricValues[key] = value
        layout.addLayout(metricStrip)
        self.analysisFtpSpin = QSpinBox()
        self.analysisFtpSpin.setRange(1, 600)
        self.analysisFtpSpin.setSuffix(" W")
        self.analysisFtpSpin.setValue(int(self.settings.value("analysis/ftp_w", 200)))
        self.analysisFtpSpin.setAccessibleName("FTP 功率設定")
        self.analysisFtpSpin.setToolTip("用於計算 NP、IF、TSS 與功率區間；不是醫療判讀")
        analysisControls = QHBoxLayout()
        analysisControls.addWidget(QLabel("FTP"))
        analysisControls.addWidget(self.analysisFtpSpin)
        analysisControls.addWidget(QLabel("僅供騎乘訓練指標計算", objectName="muted"))
        analysisControls.addStretch()
        layout.addLayout(analysisControls)
        self.summaryTabs = QTabWidget()
        chartPage = QWidget()
        chartLayout = QVBoxLayout(chartPage)
        chartLayout.setContentsMargins(8, 8, 8, 8)
        self.summaryPlots = pg.GraphicsLayoutWidget()
        self.summaryPlots.setBackground("#ffffff")
        self.powerAnalysisPlot = self.summaryPlots.addPlot(row=0, col=0, title="功率 Power")
        self.cadenceAnalysisPlot = self.summaryPlots.addPlot(row=1, col=0, title="踏頻 Cadence")
        self.balanceAnalysisPlot = self.summaryPlots.addPlot(row=2, col=0, title="左右平衡 Left / Right Balance")
        self.powerAnalysisPlot.setLabel("left", "功率", units="W")
        self.cadenceAnalysisPlot.setLabel("left", "踏頻", units="rpm")
        self.balanceAnalysisPlot.setLabel("left", "平衡", units="%")
        self.balanceAnalysisPlot.setLabel("bottom", "時間", units="s")
        self.balanceAnalysisPlot.setYRange(0, 100, padding=0.04)
        self.cadenceAnalysisPlot.setXLink(self.powerAnalysisPlot)
        self.balanceAnalysisPlot.setXLink(self.powerAnalysisPlot)
        for chart_name, plot in (("功率", self.powerAnalysisPlot), ("踏頻", self.cadenceAnalysisPlot), ("左右平衡", self.balanceAnalysisPlot)):
            plot.showGrid(x=True, y=True, alpha=0.18)
            self.attachPlotFitButton(plot, chart_name)

        chartColumns = QHBoxLayout()
        chartColumns.setSpacing(18)
        pedalInfo = QWidget()
        pedalInfo.setMinimumWidth(300)
        pedalInfoLayout = QVBoxLayout(pedalInfo)
        pedalInfoLayout.setContentsMargins(4, 4, 4, 4)
        pedalInfoLayout.setSpacing(8)
        pedalInfoLayout.addWidget(QLabel("踏板資訊", objectName="sectionTitle"))
        self.summaryDetails = QLabel(objectName="muted")
        self.summaryDetails.setText("完成踏板收集，或匯入踏板 CSV 後，摘要會顯示在這裡")
        self.summaryDetails.setTextFormat(Qt.TextFormat.PlainText)
        self.summaryDetails.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summaryDetails.setWordWrap(True)
        self.summaryDetails.setAccessibleName("騎乘數值摘要")
        pedalInfoLayout.addWidget(self.summaryDetails)
        pedalInfoLayout.addStretch(1)
        pedalInfoLayout.addWidget(QLabel("雙腳平衡與估計功率", objectName="sectionTitle"))
        rings = QHBoxLayout()
        rings.setSpacing(12)
        self.leftPowerBalanceRing = PowerBalanceRing("左腳")
        self.rightPowerBalanceRing = PowerBalanceRing("右腳")
        rings.addWidget(self.leftPowerBalanceRing, 1)
        rings.addWidget(self.rightPowerBalanceRing, 1)
        pedalInfoLayout.addLayout(rings)
        chartColumns.addWidget(pedalInfo, 1)
        self.summaryPlots.setMinimumWidth(760)
        self.summaryPlots.setMinimumHeight(620)
        chartColumns.addWidget(self.summaryPlots, 4)
        chartLayout.addLayout(chartColumns, 1)
        self.pedalMetricCards = {}
        self.pedalZoneTable = None
        self.summaryTabs.addTab(chartPage, "踏板分析")
        ridePage = QWidget()
        rideLayout = QVBoxLayout(ridePage)
        self.rideEmptyLabel = QLabel(objectName="muted")
        self.rideEmptyLabel.hide()
        self.rideMuscleTable = QTableWidget(0, 6)
        self.rideMuscleTable.setHorizontalHeaderLabels(("肌肉／側別", "平均 RMS", "最大 RMS", "基準比較", "前段 → 後段", "資料狀態"))
        self.rideMuscleTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rideMuscleTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.rideMuscleTable.setAccessibleName("本次騎乘各肌肉 RMS 摘要")
        self.rideMuscleTable.setMaximumHeight(210)
        self.ridePlots = pg.GraphicsLayoutWidget()
        self.ridePlots.setBackground("#ffffff")
        self.ridePlots.setMinimumHeight(360)
        self.rmsAverageBarPlot = self.ridePlots.addPlot(row=0, col=0, title="各肌肉 RMS 平均值")
        self.rmsAverageBarPlot.setLabel("left", "RMS", units="mV")
        self.rmsPeakBarPlot = self.ridePlots.addPlot(row=0, col=1, title="各肌肉 RMS Peak 值")
        self.rmsPeakBarPlot.setLabel("left", "RMS", units="mV")
        self.emgEnvelopePlot = self.ridePlots.addPlot(row=1, col=0, colspan=2, title="EMG RMS Peak 包絡線（每 1 秒最高值）")
        self.emgEnvelopePlot.setLabel("left", "RMS", units="mV")
        for chart_name, plot in (("RMS 平均值", self.rmsAverageBarPlot), ("RMS Peak", self.rmsPeakBarPlot), ("EMG RMS 包絡線", self.emgEnvelopePlot)):
            plot.showGrid(x=True, y=True, alpha=0.18)
            self.attachPlotFitButton(plot, chart_name)
        self.emgEnvelopePlot.setLabel("bottom", "騎乘時間", units="s")
        envelopeControls = QHBoxLayout()
        envelopeControls.addWidget(QLabel("肌肉"))
        self.envelopeMuscleCombo = QComboBox()
        self.envelopeMuscleCombo.setAccessibleName("選擇 Peak 包絡線顯示的肌肉")
        self.envelopeMuscleCombo.setToolTip("每 1 秒取該肌肉最高 RMS 並連線；一次顯示一個肌肉，避免曲線重疊")
        self.envelopeMuscleCombo.setMinimumWidth(260)
        self.envelopeMuscleCombo.setEnabled(False)
        envelopeControls.addWidget(self.envelopeMuscleCombo)
        envelopeControls.addStretch()
        rideLayout.addLayout(envelopeControls)
        rideLayout.addWidget(self.ridePlots, 2)
        rideLayout.addWidget(self.rideMuscleTable, 1)
        self.summaryTabs.insertTab(0, ridePage, "肌電分析")
        comparePage = QWidget()
        compareLayout = QVBoxLayout(comparePage)
        compareLayout.setContentsMargins(14, 14, 14, 12)
        compareLayout.setSpacing(10)
        setup = QFrame()
        FluentThemeManager.set_property(setup, "comparisonRole", "summary")
        setupLayout = QVBoxLayout(setup)
        setupLayout.setContentsMargins(18, 16, 18, 14)
        setupLayout.setSpacing(10)
        setupLayout.addWidget(QLabel("選取同一病人的兩次量測；前、後段平均使用未標準化的每秒 RMS（mV）。", objectName="muted"))
        databaseCompareControls = QHBoxLayout()
        databaseCompareControls.setSpacing(8)
        databaseCompareControls.addWidget(QLabel("病人"))
        self.databaseComparePatientCombo = QComboBox()
        self.databaseComparePatientCombo.setMinimumWidth(210)
        self.databaseCompareReloadButton = QPushButton("載入量測")
        FluentThemeManager.set_property(self.databaseCompareReloadButton, "fluentAppearance", "primary")
        databaseCompareControls.addWidget(self.databaseComparePatientCombo)
        databaseCompareControls.addWidget(self.databaseCompareReloadButton)
        databaseCompareControls.addWidget(QLabel("量測 A"))
        self.databaseCompareACombo = QComboBox()
        self.databaseCompareACombo.setMinimumWidth(250)
        databaseCompareControls.addWidget(self.databaseCompareACombo)
        databaseCompareControls.addWidget(QLabel("量測 B"))
        self.databaseCompareBCombo = QComboBox()
        self.databaseCompareBCombo.setMinimumWidth(250)
        databaseCompareControls.addWidget(self.databaseCompareBCombo)
        databaseCompareControls.addStretch()
        setupLayout.addLayout(databaseCompareControls)
        windowControls = QHBoxLayout()
        windowControls.addWidget(QLabel("前段"))
        self.databaseCompareEarlySpin = QSpinBox()
        self.databaseCompareEarlySpin.setRange(1, 600)
        self.databaseCompareEarlySpin.setValue(30)
        self.databaseCompareEarlySpin.setSuffix(" 秒")
        windowControls.addWidget(self.databaseCompareEarlySpin)
        windowControls.addWidget(QLabel("後段"))
        self.databaseCompareLateSpin = QSpinBox()
        self.databaseCompareLateSpin.setRange(1, 600)
        self.databaseCompareLateSpin.setValue(30)
        self.databaseCompareLateSpin.setSuffix(" 秒")
        windowControls.addWidget(self.databaseCompareLateSpin)
        self.databaseCompareRunButton = QPushButton("比較 RMS")
        FluentThemeManager.set_property(self.databaseCompareRunButton, "fluentAppearance", "success")
        windowControls.addWidget(self.databaseCompareRunButton)
        windowControls.addStretch()
        setupLayout.addLayout(windowControls)
        compareLayout.addWidget(setup)
        self.comparisonStatusLabel = QLabel("請先在設定頁測試資料庫連線，接著載入病人與量測紀錄。", objectName="muted")
        self.comparisonStatusLabel.setWordWrap(True)
        compareLayout.addWidget(self.comparisonStatusLabel)
        self.databaseComparisonTabs = QTabWidget()
        self.databaseComparisonLayouts: dict[str, QVBoxLayout] = {}
        self.databaseComparisonScrolls: dict[str, QScrollArea] = {}
        self.databaseComparisonPlots: list[pg.PlotWidget] = []
        for side, title in (("R", "右腳"), ("L", "左腳")):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            cards = QVBoxLayout(content)
            cards.setContentsMargins(4, 4, 4, 4)
            cards.setSpacing(10)
            cards.addStretch()
            scroll.setWidget(content)
            self.databaseComparisonLayouts[side] = cards
            self.databaseComparisonScrolls[side] = scroll
            self.databaseComparisonTabs.addTab(scroll, title)
        compareLayout.addWidget(self.databaseComparisonTabs, 1)
        self.comparisonNotes = QLabel("RMS 代表肌肉電活動，非肌力。資料庫目前未保存濾波設定與取樣率，跨次比較前請確認兩次收集設定一致。", objectName="muted")
        self.comparisonNotes.setWordWrap(True)
        compareLayout.addWidget(self.comparisonNotes)
        self.comparisonTab = comparePage
        anglePage = QWidget()
        angleLayout = QVBoxLayout(anglePage)
        angleLayout.setSpacing(8)
        self.angleViews = QTabWidget()
        self.angleViews.setUsesScrollButtons(True)
        overviewPage = QWidget()
        overviewLayout = QVBoxLayout(overviewPage)
        overviewLayout.setContentsMargins(12, 10, 12, 10)
        overviewLayout.addWidget(QLabel("本次分析重點", objectName="sectionTitle"))
        self.angleOverviewLabel = QLabel("完成分析後，這裡會顯示前後段的可讀摘要。", objectName="muted")
        self.angleOverviewLabel.setWordWrap(True)
        self.angleOverviewLabel.setAccessibleName("踩踏角度分析摘要")
        overviewLayout.addWidget(self.angleOverviewLabel)
        overviewLayout.addStretch(1)
        # Keep progress and failures in the analysis overview instead of a
        # separate banner above the angle charts.
        self.angleStatusLabel = self.angleOverviewLabel

        phasePage = QWidget()
        phaseLayout = QVBoxLayout(phasePage)
        phaseLayout.setContentsMargins(8, 8, 8, 8)
        phaseRow = QHBoxLayout()
        phaseRow.addWidget(QLabel("查看踩踏相位", objectName="sectionTitle"))
        self.anglePhaseSpin = QSpinBox()
        self.anglePhaseSpin.setRange(0, 359)
        self.anglePhaseSpin.setSingleStep(15)
        self.anglePhaseSpin.setSuffix("°")
        self.anglePhaseSpin.setValue(0)
        self.anglePhaseSpin.setAccessibleName("要比較的踩踏相位")
        self.anglePhaseSpin.setToolTip("以 0° 至 359° 的相對踩踏相位查看各肌肉訊號")
        phaseRow.addWidget(self.anglePhaseSpin)
        phaseRow.addStretch()
        phaseLayout.addLayout(phaseRow)
        self.anglePhaseLabel = QLabel("完成分析後，會在這裡直接指出 0° 相對活化最高的肌肉。", objectName="muted")
        self.anglePhaseLabel.setWordWrap(True)
        self.anglePhaseLabel.setAccessibleName("踩踏相位肌肉訊號答案")
        phaseLayout.addWidget(self.anglePhaseLabel)
        self.anglePhasePlot = pg.PlotWidget(title="0° 肌肉相對 EMG 活化｜藍色：前段；紅色：後段")
        self.anglePhasePlot.setLabel("bottom", "相對自身參考值", units="%")
        self.anglePhasePlot.setMinimumHeight(270)
        self.attachPlotFitButton(self.anglePhasePlot.getPlotItem(), "活化區間圖")
        phaseLayout.addWidget(self.anglePhasePlot, 1)
        phaseLayout.addWidget(QLabel("每條肌肉各自正規化；百分比用於比較該肌肉在不同相位的訊號，不代表實際出力或跨肌肉力量。", objectName="muted"))

        heatPage = QWidget()
        heatLayout = QVBoxLayout(heatPage)
        heatLayout.setContentsMargins(8, 8, 8, 8)
        self.angleHeatPlot = pg.PlotWidget(title="全肌肉角度熱圖｜每列僅比較自身訊號")
        self.angleHeatPlot.setLabel("bottom", "相對踩踏相位", units="°")
        self.angleHeatPlot.setMinimumHeight(250)
        self.attachPlotFitButton(self.angleHeatPlot.getPlotItem(), "角度熱圖")
        heatLayout.addWidget(self.angleHeatPlot, 1)

        comparisonPage = QWidget()
        comparisonLayout = QVBoxLayout(comparisonPage)
        comparisonLayout.setContentsMargins(8, 8, 8, 8)
        comparisonLayout.addWidget(QLabel("前後段數值比較", objectName="sectionTitle"))
        self.angleComparisonTable = QTableWidget(0, 10)
        self.angleComparisonTable.setHorizontalHeaderLabels(("肌肉／側別", "前段圈數", "後段圈數", "前段 RMS µV", "後段 RMS µV", "RMS 變化 %", "前段峰值 °", "後段峰值 °", "峰值位移 °", "MDF 變化 Hz"))
        self.angleComparisonTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.angleComparisonTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.angleComparisonTable.setAccessibleName("踩踏角度分析前後段比較")
        self.angleComparisonTable.setMinimumHeight(220)
        comparisonLayout.addWidget(self.angleComparisonTable, 1)
        self.angleDetailPage = QWidget()
        angleDetailPage = self.angleDetailPage
        angleDetailLayout = QVBoxLayout(angleDetailPage)
        angleDetailLayout.setContentsMargins(8, 8, 8, 8)
        selectorRow = QHBoxLayout()
        selectorRow.addWidget(QLabel("查看肌肉", objectName="sectionTitle"))
        self.angleMuscleCombo = QComboBox()
        self.angleMuscleCombo.setAccessibleName("選擇角度曲線肌肉")
        self.angleMuscleCombo.setMinimumWidth(300)
        selectorRow.addWidget(self.angleMuscleCombo)
        selectorRow.addStretch()
        angleDetailLayout.addLayout(selectorRow)
        self.angleDetailLabel = QLabel("選擇一個肌肉，查看前後段的相位曲線與峰值變化。", objectName="muted")
        self.angleDetailLabel.setWordWrap(True)
        angleDetailLayout.addWidget(self.angleDetailLabel)
        self.angleCurvePlot = pg.PlotWidget(title="選取肌肉的前段／後段相位曲線")
        self.angleCurvePlot.setLabel("left", "相對自身參考值", units="%")
        self.angleCurvePlot.setLabel("bottom", "相對踩踏相位", units="°")
        self.angleCurvePlot.addLegend()
        self.angleCurvePlot.showGrid(x=True, y=True, alpha=.18)
        self.attachPlotFitButton(self.angleCurvePlot.getPlotItem(), "相位曲線")
        angleDetailLayout.addWidget(self.angleCurvePlot, 1)
        angleDetailLayout.addWidget(QLabel("藍線＝前段，紅線＝後段；垂直虛線＝各段峰值相位。肌電訊號強度不等於施力大小。", objectName="muted"))

        reportChartsPage = QScrollArea()
        reportChartsPage.setWidgetResizable(True)
        reportChartsContent = QWidget()
        reportChartsContent.setMinimumHeight(700)
        reportChartsLayout = QVBoxLayout(reportChartsContent)
        reportChartsLayout.setContentsMargins(8, 8, 8, 8)
        reportChartsLayout.setSpacing(18)
        reportChartsLayout.addWidget(QLabel("每個肌肉一組直向長條；藍色為左腳、橘色為右腳。", objectName="muted"))
        self.angleReportRmsPlot = pg.PlotWidget(title="RMS 變化（後段比前段）")
        self.angleReportRmsPlot.setLabel("bottom", "RMS 變化", units="%")
        self.angleReportRmsPlot.setMinimumHeight(300)
        reportChartsLayout.addWidget(self.angleReportRmsPlot, 1)
        self.angleReportMdfPlot = pg.PlotWidget(title="MDF 變化（後段減前段）")
        self.angleReportMdfPlot.setLabel("bottom", "MDF 變化", units="Hz")
        self.angleReportMdfPlot.setMinimumHeight(300)
        reportChartsLayout.addWidget(self.angleReportMdfPlot, 1)
        reportChartsPage.setWidget(reportChartsContent)

        fatigueTrendsPage = QScrollArea()
        fatigueTrendsPage.setWidgetResizable(True)
        fatigueTrendsContent = QWidget()
        # Four full-height plots need more space than a typical report tab.
        # Keep the page scrollable instead of clipping the lower two plots.
        fatigueTrendsContent.setMinimumHeight(760)
        fatigueTrendsLayout = QVBoxLayout(fatigueTrendsContent)
        fatigueTrendsLayout.setContentsMargins(8, 8, 8, 8)
        fatigueTrendControls = QHBoxLayout()
        fatigueTrendControls.addWidget(QLabel("肌肉", objectName="sectionTitle"))
        self.angleReportFatigueMuscleCombo = QComboBox()
        self.angleReportFatigueMuscleCombo.setMinimumWidth(260)
        self.angleReportFatigueMuscleCombo.setAccessibleName("PPT 逐圈疲勞趨勢肌肉選擇")
        fatigueTrendControls.addWidget(self.angleReportFatigueMuscleCombo)
        fatigueTrendControls.addStretch(1)
        fatigueTrendsLayout.addLayout(fatigueTrendControls)
        fatigueTrendsLayout.addWidget(QLabel("每條線為一側肌肉，以 9 圈移動平均顯示逐圈變化。", objectName="muted"))
        fatigueTrendGrid = QGridLayout()
        self.angleReportRmsTrendPlot = pg.PlotWidget(title="逐圈 RMS 趨勢：左右腳疊圖")
        self.angleReportMdfTrendPlot = pg.PlotWidget(title="逐圈 MDF 趨勢：左右腳疊圖")
        self.angleReportFatigueTrendPlots = (
            self.angleReportRmsTrendPlot, self.angleReportMdfTrendPlot,
        )
        for plot in self.angleReportFatigueTrendPlots:
            plot.setMinimumHeight(330)
            plot.setLabel("bottom", "踩踏圈數")
            plot.showGrid(x=True, y=True, alpha=.16)
            plot.addLegend(offset=(8, 8), colCount=2)
        self.angleReportRmsTrendPlot.setLabel("left", "RMS", units="µV")
        self.angleReportMdfTrendPlot.setLabel("left", "MDF", units="Hz")
        fatigueTrendGrid.addWidget(self.angleReportRmsTrendPlot, 0, 0)
        fatigueTrendGrid.addWidget(self.angleReportMdfTrendPlot, 1, 0)
        fatigueTrendsLayout.addLayout(fatigueTrendGrid, 1)
        fatigueTrendsPage.setWidget(fatigueTrendsContent)

        reportProfilesPage = QWidget()
        reportProfilesLayout = QVBoxLayout(reportProfilesPage)
        reportProfilesLayout.setContentsMargins(8, 8, 8, 8)
        profileControls = QHBoxLayout()
        profileControls.addWidget(QLabel("肌肉", objectName="sectionTitle"))
        self.angleReportProfileMuscleCombo = QComboBox()
        self.angleReportProfileMuscleCombo.setMinimumWidth(260)
        self.angleReportProfileMuscleCombo.setAccessibleName("PPT 肌肉角度曲線肌肉選擇")
        profileControls.addWidget(self.angleReportProfileMuscleCombo)
        profileControls.addStretch(1)
        reportProfilesLayout.addLayout(profileControls)
        reportProfilesLayout.addWidget(QLabel("每個肌肉的左、右腳並排；藍線為前段、紅線為後段。", objectName="muted"))
        self.angleReportProfileScroll = QScrollArea()
        self.angleReportProfileScroll.setWidgetResizable(True)
        self.angleReportProfiles = pg.GraphicsLayoutWidget()
        self.angleReportProfiles.setMinimumHeight(1620)
        self.angleReportProfiles.setBackground("#ffffff")
        self.angleReportProfileScroll.setWidget(self.angleReportProfiles)
        reportProfilesLayout.addWidget(self.angleReportProfileScroll, 1)

        polarPage = QWidget()
        polarLayout = QVBoxLayout(polarPage)
        polarLayout.setContentsMargins(8, 8, 8, 8)
        polarControls = QHBoxLayout()
        polarControls.addWidget(QLabel("肌肉", objectName="sectionTitle"))
        self.angleReportPolarMuscleCombo = QComboBox()
        self.angleReportPolarMuscleCombo.setMinimumWidth(260)
        self.angleReportPolarMuscleCombo.setAccessibleName("圓形踩踏角度圖肌肉選擇")
        polarControls.addWidget(self.angleReportPolarMuscleCombo)
        polarControls.addStretch(1)
        polarLayout.addLayout(polarControls)
        polarLayout.addWidget(QLabel(
            "0° 位於正上方，角度順時針增加；半徑是各肌肉自身相對活化（%）。藍線為前段，紅線為後段。",
            objectName="muted"
        ))
        self.angleReportPolarScroll = QScrollArea()
        self.angleReportPolarScroll.setWidgetResizable(True)
        self.angleReportPolarContent = QWidget()
        self.angleReportPolarGrid = QGridLayout(self.angleReportPolarContent)
        self.angleReportPolarGrid.setContentsMargins(10, 10, 10, 10)
        self.angleReportPolarGrid.setSpacing(14)
        self.angleReportPolarScroll.setWidget(self.angleReportPolarContent)
        polarLayout.addWidget(self.angleReportPolarScroll, 1)

        reportCyclesPage = QWidget()
        reportCyclesLayout = QVBoxLayout(reportCyclesPage)
        reportCyclesLayout.setContentsMargins(8, 8, 8, 8)
        cycleControls = QHBoxLayout()
        cycleControls.addWidget(QLabel("肌肉", objectName="sectionTitle"))
        self.angleReportCycleMuscleCombo = QComboBox()
        self.angleReportCycleMuscleCombo.setMinimumWidth(260)
        self.angleReportCycleMuscleCombo.setAccessibleName("PPT 逐圈 8 角度肌肉選擇")
        cycleControls.addWidget(self.angleReportCycleMuscleCombo)
        cycleControls.addWidget(QLabel("踩踏角度", objectName="sectionTitle"))
        self.angleReportCycleAngleCombo = QComboBox()
        self.angleReportCycleAngleCombo.setMinimumWidth(130)
        self.angleReportCycleAngleCombo.setAccessibleName("逐圈活化踩踏角度選擇")
        self.angleReportCycleAngleCombo.addItem("全部角度", None)
        for angle in range(0, 360, 45):
            self.angleReportCycleAngleCombo.addItem(f"{angle}°", angle)
        cycleControls.addWidget(self.angleReportCycleAngleCombo)
        cycleControls.addStretch()
        reportCyclesLayout.addLayout(cycleControls)
        self.angleReportCycleHelpLabel = QLabel(
            "可選擇單一角度來比較左、右腳逐圈變化；「全部角度」顯示 0°、45°、90°…315°。已以 5 圈移動平均平滑，淡藍為前段、淡紅為後段。",
            objectName="muted"
        )
        reportCyclesLayout.addWidget(self.angleReportCycleHelpLabel)
        cyclePlots = QHBoxLayout()
        self.angleReportCycleLeftPlot = pg.PlotWidget(title="左腳")
        self.angleReportCycleRightPlot = pg.PlotWidget(title="右腳")
        for plot in (self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot):
            plot.setMinimumHeight(410)
            plot.setLabel("left", "相對活化", units="%")
            plot.setLabel("bottom", "踩踏圈數")
            plot.showGrid(x=True, y=True, alpha=.16)
            plot.addLegend(offset=(8, 8), colCount=4)
        cyclePlots.addWidget(self.angleReportCycleLeftPlot, 1)
        cyclePlots.addWidget(self.angleReportCycleRightPlot, 1)
        reportCyclesLayout.addLayout(cyclePlots, 1)

        reportValuesPage = QWidget()
        reportValuesLayout = QVBoxLayout(reportValuesPage)
        reportValuesLayout.setContentsMargins(8, 8, 8, 8)
        valueControls = QHBoxLayout()
        valueControls.addWidget(QLabel("肌肉", objectName="sectionTitle"))
        self.angleReportPhaseMuscleCombo = QComboBox()
        self.angleReportPhaseMuscleCombo.setMinimumWidth(260)
        self.angleReportPhaseMuscleCombo.setAccessibleName("PPT 前後段 8 角度表格肌肉選擇")
        valueControls.addWidget(self.angleReportPhaseMuscleCombo)
        valueControls.addStretch()
        reportValuesLayout.addLayout(valueControls)
        reportValuesLayout.addWidget(QLabel("每個角度為 ±15° 視窗平均；Δ = 後段 − 前段。", objectName="muted"))
        self.angleReportPhaseTable = QTableWidget(0, 7)
        self.angleReportPhaseTable.setHorizontalHeaderLabels(("踩踏角度", "左前段", "左後段", "左 Δ", "右前段", "右後段", "右 Δ"))
        self.angleReportPhaseTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.angleReportPhaseTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.angleReportPhaseTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.angleReportPhaseTable.setMinimumHeight(360)
        self.angleReportPhaseTable.setAccessibleName("PPT 前後段 8 角度比較表")
        reportValuesLayout.addWidget(self.angleReportPhaseTable, 1)
        reportTextPage = QWidget()
        reportTextLayout = QVBoxLayout(reportTextPage)
        reportTextLayout.setContentsMargins(8, 8, 8, 8)
        self.angleReportDetailsScroll = QScrollArea()
        self.angleReportDetailsScroll.setWidgetResizable(True)
        reportTextContent = QWidget()
        reportTextContentLayout = QVBoxLayout(reportTextContent)
        reportTextContentLayout.setContentsMargins(4, 4, 4, 4)
        reportTextContentLayout.setSpacing(8)

        def report_table(headers: tuple[str, ...], accessible_name: str) -> QTableWidget:
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.setAlternatingRowColors(True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setAccessibleName(accessible_name)
            return table

        reportTextContentLayout.addWidget(QLabel("【測試與同步】", objectName="sectionTitle"))
        self.angleReportSyncTable = report_table(("項目", "數值"), "踩踏角度測試與同步表格")
        self.angleReportSyncTable.setMinimumHeight(190)
        reportTextContentLayout.addWidget(self.angleReportSyncTable)
        reportTextContentLayout.addWidget(QLabel("【通道與貼片對應】", objectName="sectionTitle"))
        self.angleReportMappingTable = report_table(("肌肉 / 部位", "通道"), "踩踏角度通道對應表格")
        self.angleReportMappingTable.setMinimumHeight(360)
        reportTextContentLayout.addWidget(self.angleReportMappingTable)
        reportTextContentLayout.addWidget(QLabel("【重點結果】", objectName="sectionTitle"))
        self.angleReportHighlightsTable = report_table(("項目", "結果"), "踩踏角度重點結果表格")
        self.angleReportHighlightsTable.setMinimumHeight(150)
        reportTextContentLayout.addWidget(self.angleReportHighlightsTable)
        reportTextContentLayout.addWidget(QLabel("【通道前後段數值】", objectName="sectionTitle"))
        self.angleReportChannelTable = QTableWidget(0, 6)
        self.angleReportChannelTable.setHorizontalHeaderLabels((
            "肌肉 / 通道", "RMS 前→後 (µV)", "RMS %",
            "MDF 前→後 (Hz)", "峰值角度變化", "判讀提示",
        ))
        self.angleReportChannelTable.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.angleReportChannelTable.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.angleReportChannelTable.setAlternatingRowColors(True)
        self.angleReportChannelTable.setMinimumHeight(300)
        self.angleReportChannelTable.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.angleReportChannelTable.setAccessibleName("通道前後段數值表格")
        reportTextContentLayout.addWidget(self.angleReportChannelTable)
        reportTextContentLayout.addWidget(QLabel("【判讀限制】", objectName="sectionTitle"))
        self.angleReportLimitationsTable = report_table(("注意事項",), "踩踏角度判讀限制表格")
        self.angleReportLimitationsTable.setMinimumHeight(150)
        reportTextContentLayout.addWidget(self.angleReportLimitationsTable)
        reportTextContentLayout.addStretch()
        self.angleReportDetailsScroll.setWidget(reportTextContent)
        reportTextLayout.addWidget(self.angleReportDetailsScroll, 1)
        for title, page in (
            ("分析重點", overviewPage),
            ("疲勞摘要", reportChartsPage),
            ("逐圈疲勞趨勢", fatigueTrendsPage),
            ("肌肉曲線", angleDetailPage),
            ("指定角度", phasePage),
            ("全肌肉熱圖", heatPage),
            ("肌肉角度曲線", reportProfilesPage),
            ("圓形角度圖", polarPage),
            ("逐圈 8 角度", reportCyclesPage),
            ("前後段數值", comparisonPage),
            ("前後段表格", reportValuesPage),
            ("數值與判讀", reportTextPage),
        ):
            self.angleViews.addTab(page, title)
        angleLayout.addWidget(self.angleViews, 1)
        self.summaryTabs.insertTab(2, anglePage, "踩踏角度分析")
        self.angleExportButton.clicked.connect(self.exportAngleAnalysis)
        self.angleMuscleCombo.currentIndexChanged.connect(self.refreshAngleCurve)
        self.anglePhaseSpin.valueChanged.connect(self.refreshAnglePhase)
        self.angleComparisonTable.itemSelectionChanged.connect(self.showSelectedAngleMuscle)
        self.angleReportCycleMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportCycleTrends)
        self.angleReportCycleAngleCombo.currentIndexChanged.connect(self.refreshAngleReportCycleTrends)
        self.angleReportPhaseMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportPhaseTable)
        self.angleReportFatigueMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportFatigueTrends)
        self.angleReportProfileMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportProfiles)
        self.angleReportPolarMuscleCombo.currentIndexChanged.connect(self.refreshAngleReportPolarProfiles)
        self.lockAnalysisPlots(
            self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot,
            self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot,
            self.anglePhasePlot, self.angleHeatPlot,
            self.angleCurvePlot, self.angleReportRmsPlot, self.angleReportMdfPlot,
            self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot,
            *self.angleReportFatigueTrendPlots,
        )
        self.summaryTabs.setCurrentIndex(0)
        layout.addWidget(self.summaryTabs, 1)
        self.importPedalCsvButton.clicked.connect(self.importPedalCsvSummary)
        self.importEmgAnalysisButton.clicked.connect(self.importEmgAnalysis)
        self.angleSettingsButton.clicked.connect(self.openAngleAnalysisSettings)
        self.currentSessionSummaryButton.clicked.connect(self.showCurrentSessionSummary)
        self.analysisFtpSpin.valueChanged.connect(self.refreshSummaryAnalysis)
        self.envelopeMuscleCombo.currentIndexChanged.connect(self.refreshRideAnalysis)
        self.exportReportButton.clicked.connect(self.exportRideReport)
        self.databaseCompareReloadButton.clicked.connect(self.loadDatabaseComparisonPatients)
        self.databaseComparePatientCombo.currentIndexChanged.connect(self.loadDatabaseComparisonSessions)
        self.databaseCompareRunButton.clicked.connect(self.refreshDatabaseComparison)
        return tab

    @staticmethod
    def lockAnalysisPlots(*plots) -> None:
        """Keep report charts readable; body-map zoom remains intentionally separate."""
        for plot in plots:
            item = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
            item.setMouseEnabled(x=False, y=False)
            item.setMenuEnabled(False)

    def applySummaryPlotTheme(self, *_args) -> None:
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        for widget in (self.summaryPlots, self.ridePlots, self.angleCurvePlot,
                       self.angleHeatPlot, self.anglePhasePlot, self.angleReportRmsPlot,
                       self.angleReportMdfPlot, self.angleReportProfiles,
                       self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot,
                       *self.angleReportFatigueTrendPlots):
            widget.setBackground(tokens["surface_background"])
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot,
                     self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot,
                     self.angleCurvePlot.getPlotItem(),
                     self.angleHeatPlot.getPlotItem(), self.anglePhasePlot.getPlotItem(),
                     self.angleReportRmsPlot.getPlotItem(), self.angleReportMdfPlot.getPlotItem(),
                     self.angleReportCycleLeftPlot.getPlotItem(), self.angleReportCycleRightPlot.getPlotItem(),
                     *(plot.getPlotItem() for plot in self.angleReportFatigueTrendPlots)):
            for axis_name in ("left", "bottom"):
                axis = plot.getAxis(axis_name)
                axis.setPen(tokens["text_secondary"])
                axis.setTextPen(tokens["text_primary"])
        for plot in getattr(self, "databaseComparisonPlots", []):
            plot.setBackground("#ffffff")
            for axis_name in ("left", "bottom"):
                axis = plot.getAxis(axis_name)
                axis.setPen("#667085")
                axis.setTextPen("#1f2937")
            plot.getPlotItem().titleLabel.setAttr("color", "#1f2937")
        if hasattr(self, "plotWidget"):
            self.plotWidget.setBackground(tokens["surface_background"])
            for plot, _curve, stats in self.curves.values():
                stats.setColor(tokens["text_secondary"])
                for axis_name in ("left", "bottom"):
                    axis = plot.getAxis(axis_name)
                    axis.setPen(tokens["text_secondary"])
                    axis.setTextPen(tokens["text_primary"])
        if hasattr(self, "leftPowerBalanceRing"):
            self.leftPowerBalanceRing.setThemeColors(tokens["success"], tokens["border_primary"], tokens["text_primary"],)
            self.rightPowerBalanceRing.setThemeColors(tokens["brand_background"], tokens["border_primary"], tokens["text_primary"],)

    def onSystemThemeChanged(self, *_args) -> None:
        FluentThemeManager(QApplication.instance()).apply()
        self.applySummaryPlotTheme()

    def attachPlotFitButton(self, plot: pg.PlotItem, chart_name: str, callback=None) -> QToolButton:
        """Place an independent auto-fit control inside a plot's title row."""
        button = QToolButton()
        button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setToolTip(f"自動縮放：{chart_name}")
        button.setAccessibleName(f"自動縮放 {chart_name}")

        def fit_plot() -> None:
            plot.enableAutoRange(x=True, y=True)
            plot.autoRange()

        button.clicked.connect(callback or fit_plot)
        proxy = QGraphicsProxyWidget()
        proxy.setWidget(button)
        # PlotItem's first layout row is outside the data ViewBox, so the
        # icon remains at the upper right while panning or zooming data.
        plot.layout.addItem(proxy, 0, 2)
        return button

    def resetLivePlotView(self) -> None:
        self.refreshPlots()

    def createCollectionTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        statusRow = QHBoxLayout()
        self.emgStatus = QLabel(tab, objectName="status")
        self.baseLabel = QLabel("Base: 尚未確認", tab, objectName="muted")
        statusRow.addWidget(self.emgStatus)
        statusRow.addWidget(self.baseLabel)
        statusRow.addStretch()
        self.emgRateLabel = QLabel("EMG 頻率：等待 SDK 查詢", objectName="muted" )
        statusRow.addWidget(self.emgRateLabel)
        self.emgStatus.hide()
        self.baseLabel.hide()
        controls = QHBoxLayout()
        self.hostEdit = QLineEdit("127.0.0.1")
        self.hostEdit.setMinimumWidth(160)
        self.testButton, self.startButton, self.stopButton = QPushButton("測試連線"), QPushButton("開始串流", objectName="record"), QPushButton("停止")
        self.discardButton = QPushButton("捨棄本次測試")
        self.calibrateButton = QPushButton("測量 20 秒基準")
        FluentThemeManager.set_property(self.calibrateButton, "fluentAppearance", "danger")
        self.calibrationLabel = QLabel(objectName="muted")
        self.calibrationLabel.hide()
        FluentThemeManager.set_property(self.startButton, "fluentAppearance", "success")
        FluentThemeManager.set_property(self.stopButton, "fluentAppearance", "danger")
        FluentThemeManager.set_property(self.discardButton, "fluentAppearance", "danger")
        self.discardButton.setToolTip("刪除剛完成或正在收集的測試檔案與資料庫資料")
        for widget in (self.hostEdit, self.testButton, self.startButton, self.calibrateButton, self.stopButton, self.discardButton):
            controls.addWidget(widget)
        controls.addWidget(self.calibrationLabel)
        controls.addSpacing(10)
        controls.addWidget(QLabel("收集時間"))
        self.durationMinutesSpin = QSpinBox()
        self.durationMinutesSpin.setRange(0, 120)
        self.durationMinutesSpin.setSuffix(" m")
        self.durationSecondsSpin = QSpinBox()
        self.durationSecondsSpin.setRange(0, 59)
        self.durationSecondsSpin.setSuffix(" s")
        for widget in (self.durationMinutesSpin, self.durationSecondsSpin):
            controls.addWidget(widget)
        self.sampleLabel = QLabel("0 samples", objectName="muted")
        controls.addWidget(self.sampleLabel)
        controls.addStretch()
        self.recordingIndicator = QLabel("● 錄製中", objectName="recordingIndicator")
        self.recordingIndicator.hide()
        controls.addWidget(self.recordingIndicator)
        self.countdownLabel = QLabel(objectName="countdown")
        self.countdownLabel.setMinimumWidth(112)
        self.countdownLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.countdownLabel.hide()
        controls.addWidget(self.countdownLabel)
        layout.addLayout(controls)
        layout.addWidget(self.sessionDevicesPanel)
        metrics = QHBoxLayout()
        self.bikeMetricValues: dict[str, QLabel] = {}
        for key, title, unit in (("speed", "速度", "km/h"),("cadence", "踏頻", "rpm"),("power", "即時功率", "W"),("resistance", "阻力", "level"),):
            card = QFrame(objectName="metricCard")
            cardLayout = QVBoxLayout(card)
            cardLayout.setContentsMargins(12, 8, 12, 8)
            cardLayout.setSpacing(1)
            cardLayout.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            valueRow = QHBoxLayout()
            valueRow.setSpacing(6)
            valueRow.addWidget(value)
            valueRow.addStretch()
            valueRow.addWidget(QLabel(unit, objectName="metricUnit"))
            cardLayout.addLayout(valueRow)
            self.bikeMetricValues[key] = value
            metrics.addWidget(card, 1)
        distanceCard = QFrame(objectName="metricCard")
        distanceLayout = QVBoxLayout(distanceCard)
        distanceLayout.setContentsMargins(12, 8, 12, 8)
        distanceLayout.setSpacing(1)
        distanceLayout.addWidget(QLabel("騎乘公里", objectName="metricTitle"))
        distanceValue = QLabel("--", objectName="metricValue")
        distanceRow = QHBoxLayout()
        distanceRow.setSpacing(6)
        distanceRow.addWidget(distanceValue)
        distanceRow.addStretch()
        distanceRow.addWidget(QLabel("km", objectName="metricUnit"))
        distanceLayout.addLayout(distanceRow)
        self.bikeMetricValues["distance"] = distanceValue
        metrics.insertWidget(0, distanceCard, 1)
        layout.addLayout(metrics)
        pedalMetrics = QHBoxLayout()
        self.pedalMetricValues: dict[str, QLabel] = {}
        for key, title, unit in (("left", "踏板左功率平衡", "%"), ("right", "踏板右功率平衡", "%")):
            card = QFrame(objectName="metricCard")
            cardLayout = QVBoxLayout(card)
            cardLayout.setContentsMargins(12, 8, 12, 8)
            cardLayout.addWidget(QLabel(title, objectName="metricTitle"))
            value = QLabel("--", objectName="metricValue")
            row = QHBoxLayout()
            row.addWidget(value)
            row.addStretch()
            row.addWidget(QLabel(unit, objectName="metricUnit"))
            cardLayout.addLayout(row)
            self.pedalMetricValues[key] = value
            pedalMetrics.addWidget(card, 1)
        layout.addLayout(pedalMetrics)
        body = QHBoxLayout()
        panel = self.createSensorPanel()
        self.bodyMap = BodyMapPanel(COLORS)
        # Give the anatomy view more room while retaining the existing
        # sensor legend and the independently resizable waveform area.
        self.bodyMap.setFixedWidth(720)
        self.bodyMap.setLegendWidget(panel)
        self.bodyMap.slotToggled.connect(self.toggleSensorFromBodyMap)
        body.addWidget(self.bodyMap)
        charts = QWidget()
        chartLayout = QVBoxLayout(charts)
        chartLayout.setContentsMargins(0, 0, 0, 0)
        chartHeader = QHBoxLayout()
        chartHeader.addWidget(QLabel("即時波形", objectName="sectionTitle"))
        chartHeader.addStretch()
        chartLayout.addLayout(chartHeader)
        self.plotWidget = pg.GraphicsLayoutWidget()
        self.plotWidget.setBackground("#ffffff")
        chartLayout.addWidget(self.plotWidget)
        body.addWidget(charts, 1)
        layout.addLayout(body, 1)
        self.testButton.clicked.connect(self.testConnection)
        self.startButton.clicked.connect(self.startStream)
        self.stopButton.clicked.connect(self.stopStream)
        self.discardButton.clicked.connect(self.requestDiscardCurrentTest)
        self.plotTimer = QTimer(self)
        self.plotTimer.setInterval(80)
        self.plotTimer.timeout.connect(self.refreshPlots)
        self.plotTimer.start()
        self.countdownTimer = QTimer(self)
        self.countdownTimer.setInterval(1000)
        self.countdownTimer.timeout.connect(self.updateCountdown)
        self.remainingSeconds = 0
        # The calibration clock is display-only.  Sample count, rather than
        # this wall clock, decides when a calibration is complete.
        self.countdownMode = "collection"
        self.countdownStopsStream = True
        self.durationMinutesSpin.valueChanged.connect(self.resetCountdownDisplay)
        self.durationSecondsSpin.valueChanged.connect(self.resetCountdownDisplay)
        self.applyStyles()
        self.syncMuscleMap()
        self.rebuildPlots()
        return tab

    def createSensorPanel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.baselineSettingsButton = QPushButton("基準值與判定設定")
        FluentThemeManager.set_property(self.baselineSettingsButton, "fluentAppearance", "warning")
        controls.addWidget(self.baselineSettingsButton)
        controls.addStretch()
        layout.addLayout(controls)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        items = QVBoxLayout(content)
        items.setContentsMargins(0, 0, 0, 0)
        for index in range(CHANNEL_COUNT):
            row = QFrame(objectName="sensorRow")
            rowLayout = QHBoxLayout(row)
            rowLayout.setContentsMargins(8, 6, 8, 6)
            rowLayout.setSpacing(3)
            icon = QLabel("〽", objectName="sensorIcon")
            icon.setFixedSize(28, 28)
            icon.setAlignment(Qt.AlignCenter)
            icon.setStyleSheet(f"background: {COLORS[index % len(COLORS)]}; color: white; border-radius: 5px; font-weight: bold; font-size: 16px;")
            check = SensorLegendToggle()
            check.setChecked(False)
            rowLayout.addWidget(icon, 0, Qt.AlignTop)
            rowLayout.addWidget(check, 1)
            items.addWidget(row)
            check.toggled.connect(self.sensorSelectionChanged)
            self.sensorChecks.append(check)
            self.sensorIcons.append(icon)
            self.sensorRows.append(row)
        items.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.updateSensorRows()
        self.baselineSettingsButton.clicked.connect(self.showBaselineSettings)
        self.calibrateButton.clicked.connect(self.startBaselineCalibration)
        return panel

    def sensorSelectionChanged(self) -> None:
        self.rebuildPlots()
        self.updateSensorRows()

    def toggleSensorFromBodyMap(self, slot: int) -> None:
        if 1 <= slot <= len(self.sensorChecks):
            check = self.sensorChecks[slot - 1]
            check.setChecked(not check.isChecked())

    def syncMuscleMap(self) -> None:
        if not hasattr(self, "bodyMap"):
            return
        selected = {index + 1 for index, check in enumerate(self.sensorChecks) if check.isChecked()}
        active = {index + 1 for index, isActive in enumerate(self.sensorActive) if isActive}
        self.bodyMap.setMappings(self.mappings, selected, active, self.muscleStatuses)

    def connectTrainerSignals(self) -> None:
        self.trainerWorker.ready.connect(self.trainerReady)
        self.trainerWorker.scanReady.connect(self.showTrainerDevices)
        self.trainerWorker.connected.connect(self.trainerConnected)
        self.trainerWorker.disconnected.connect(self.trainerDisconnected)
        self.trainerWorker.resistanceSet.connect(self.resistanceSet)
        self.trainerWorker.fixedResistanceSet.connect(self.fixedResistanceSet)
        self.trainerWorker.telemetryReceived.connect(self.updateTrainerTelemetry)
        self.trainerWorker.failed.connect(self.trainerFailed)

    def connectPedalSignals(self) -> None:
        self.pedalWorker.ready.connect(self.pedalReady)
        self.pedalWorker.devicesFound.connect(self.showPedalDevices)
        self.pedalWorker.connected.connect(self.pedalConnected)
        self.pedalWorker.disconnected.connect(self.pedalDisconnected)
        self.pedalWorker.dataReceived.connect(self.updatePedalTelemetry)
        self.pedalWorker.failed.connect(self.pedalFailed)

    def createTrainerTab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        trainerRow = QHBoxLayout()
        trainerRow.setSpacing(8)
        trainerTitle = QLabel("阻力計")
        trainerTitle.setFixedWidth(70)
        trainerRow.addWidget(trainerTitle)
        self.trainerDevices = QComboBox()
        self.trainerDevices.setEditable(True)
        self.trainerDevices.setFixedWidth(260)
        self.trainerDevices.setPlaceholderText("選擇阻力計")
        trainerRow.addWidget(self.trainerDevices)
        self.connectTrainerButton = QPushButton("連線")
        self.connectTrainerButton.setFixedWidth(100)
        FluentThemeManager.set_property(self.connectTrainerButton, "fluentAppearance", "dark")
        trainerRow.addWidget(self.connectTrainerButton)
        self.trainerTelemetryLabel = QLabel("阻力計即時騎乘資料：--", objectName="muted")
        self.trainerTelemetryLabel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        trainerRow.addWidget(self.trainerTelemetryLabel)
        trainerRow.addStretch()
        self.resistanceTab.show()
        trainerRow.addWidget(self.resistanceTab)
        layout.addLayout(trainerRow)
        pedalRow = QHBoxLayout()
        pedalRow.setSpacing(8)
        pedalTitle = QLabel("踏板")
        pedalTitle.setFixedWidth(70)
        pedalRow.addWidget(pedalTitle)
        self.pedalDevices = QComboBox()
        self.pedalDevices.setEditable(True)
        self.pedalDevices.setFixedWidth(260)
        self.pedalDevices.setPlaceholderText("選擇主踏板")
        pedalRow.addWidget(self.pedalDevices)
        self.connectPedalButton = QPushButton("連線")
        self.connectPedalButton.setFixedWidth(100)
        FluentThemeManager.set_property(self.connectPedalButton, "fluentAppearance", "dark")
        pedalRow.addWidget(self.connectPedalButton)
        self.pedalTelemetryLabel = QLabel("踏板即時功率資料：--", objectName="muted")
        self.pedalTelemetryLabel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        pedalRow.addWidget(self.pedalTelemetryLabel)
        pedalRow.addStretch()
        layout.addLayout(pedalRow)
        self.trainerStatus = QLabel()
        self.trainerStatus.hide()
        self.pedalStatus = QLabel()
        self.pedalStatus.hide()
        self.scanTrainerButton = QPushButton("重新掃描")
        self.scanPedalButton = self.scanTrainerButton
        FluentThemeManager.set_property(self.scanTrainerButton, "fluentAppearance", "primary")
        self.scanTrainerButton.clicked.connect(self.scanCyclingDevices)
        self.connectTrainerButton.clicked.connect(self.connectTrainer)
        self.connectPedalButton.clicked.connect(self.connectPedal)
        self.setTrainerControls(False)
        return tab

    def startInitialBluetoothScan(self) -> None:
        if (self.initialBluetoothScanStarted or not self.trainerBluetoothReady or not self.pedalBluetoothReady):
            return
        self.initialBluetoothScanStarted = True
        self.scanCyclingDevices()

    def scanPedal(self) -> None:
        self.scanCyclingDevices()

    def assignScannedDevice(self, address: str, name: str, role: str) -> None:
        label = f"{name or 'Unknown device'} · {address}"
        if role == "阻力計":
            self.trainerDevices.clear()
            self.trainerDevices.addItem(label, address)
            self.trainerDevices.setCurrentIndex(0)
            self.appendLog(f"Device assignment | trainer: {label}")
        elif role == "踏板":
            if name.strip().upper().endswith(" R"):
                self.appendLog(f"Device assignment skipped | right pedal: {label}")
                return
            self.pedalDevices.clear()
            self.pedalDevices.addItem(label, address)
            self.pedalDevices.setCurrentIndex(0)
            self.appendLog(f"Device assignment | pedal: {label}")

    def cyclingScanFailed(self, message: str) -> None:
        self.setTrainerStatus(f"藍牙掃描失敗：{message}", "error")
        self.setPedalStatus(f"藍牙掃描失敗：{message}", "error")

    def finishCyclingScan(self) -> None:
        self.pedalScanInProgress = False
        self.scanTrainerButton.setEnabled(True)
        self.scanPedalButton.setEnabled(True)

    def finishPedalScan(self) -> None:
        self.pedalScanInProgress = False
        self.scanPedalButton.setEnabled(True)

    def autoConnectPedal(self) -> None:
        """Compatibility hook: background Bluetooth scanning is intentionally disabled."""
        return

    def showPedalDevices(self, devices: list[tuple[str, str]]) -> None:
        self.pedalDevices.clear()
        # P715's left pedal is the Bluetooth master.  A name ending in "R"
        # identifies the right pedal, which should not be selected on its own.
        devices.sort(key=lambda device: (device[1].strip().upper().endswith(" R"), device[1]))
        p715Devices = [name for _address, name in devices if name.upper().startswith("P715")]
        onlyRightPedal = bool(p715Devices) and all(name.strip().upper().endswith(" R") for name in p715Devices)
        for address, name in devices:
            self.pedalDevices.addItem(f"{name} · {address}", address)
        remembered = next((index for index, (address, _name) in enumerate(devices) if address == self.savedPedalAddress), None)
        if remembered is not None and not self.pedalIsConnected:
            self.pedalDevices.setCurrentIndex(remembered)
            self.setPedalStatus("已找到記憶的踏板，正在自動連線…", "working")
            self.setPedalStatus("已選取記憶的踏板，請按「連線」。", "idle")
            return
        if onlyRightPedal:
            self.setPedalStatus("只找到右踏板（R）；請喚醒左踏板，並選擇沒有 R 的裝置", "working")
        else:
            self.setPedalStatus(f"找到 {len(devices)} 個藍牙裝置，請選擇沒有 R 的踏板連線" if devices else "找不到藍牙裝置；請確認踏板已喚醒","idle",)

    def pedalAddress(self) -> str:
        address = self.pedalDevices.currentData()
        return str(address) if address else self.pedalDevices.currentText().strip().rsplit("·", 1)[-1].strip()

    def connectPedal(self) -> None:
        address = self.pedalAddress()
        if not address:
            QMessageBox.warning(self, "缺少踏板位址", "請先掃描藍牙裝置或輸入 Bluetooth MAC 位址。")
            return
        name = self.pedalDevices.currentText().rsplit("·", 1)[0].strip().upper()
        if name.startswith("P715") and name.endswith(" R"):
            QMessageBox.warning(self, "請選左踏板", "請選擇不帶 R 的踏板（左側主踏板）。右踏板會由左踏板自動連線。")
            return
        self.setPedalStatus("正在連線踏板…", "working")
        self.connectPedalButton.setEnabled(False)
        self.pedalWorker.connectDevice(address)

    def pedalConnected(self, address: str) -> None:
        self.pedalIsConnected = True
        self.savedPedalAddress = address
        self.settings.setValue("pedal/address", address)
        self.setPedalConnectionState(True)
        self.setPedalStatus(f"踏板已連線：{address}", "connected")
        self.pedalTelemetryLabel.setText("踏板功率資訊：等待踩踏…")

    def pedalDisconnected(self) -> None:
        self.pedalIsConnected = False
        self.setPedalConnectionState(False)
        self.connectPedalButton.setEnabled(True)
        self.setPedalStatus("踏板已中斷", "idle")

    def pedalFailed(self, message: str) -> None:
        self.pedalIsConnected = False
        self.setPedalConnectionState(False)
        self.connectPedalButton.setEnabled(True)
        self.setPedalStatus(f"踏板錯誤：{message}", "error")

    def setPedalStatus(self, text: str, state: str) -> None:
        self.setStatusLabel(self.pedalStatus, text, state)
        self.appendLog(f"踏板 | {text}")

    def setPedalConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.pedalConnectionLabel, "踏板", connected)
        if connected and hasattr(self, "pedalTelemetryLabel"):
            self.pedalTelemetryLabel.setToolTip("踏板已連線")

    def refreshConnectionStates(self) -> None:
        """Catch silent BLE link loss even when no telemetry is arriving."""
        trainer = self.trainerWorker.trainer
        trainerConnected = bool(trainer and trainer.client and trainer.client.is_connected)
        if self.trainerIsConnected and not trainerConnected:
            self.trainerDisconnected()
        pedal = self.pedalWorker.client
        pedalConnected = bool(pedal and pedal.is_connected)
        if self.pedalIsConnected and not pedalConnected:
            self.pedalDisconnected()
        # Unlike BLE devices, the short-lived EMG connection test has already
        # ended by the time this timer runs.  Preserve that test result while
        # still giving an active stream precedence in the header.
        self.updateEmgConnectionIndicator()

    def updatePedalTelemetry(self, data: dict) -> None:
        power, cadence, left = data["power_w"], data["cadence_rpm"], data["left_balance_percent"]
        # Power and cadence are shared cycling metrics; P715 takes precedence
        # when it is connected, while the trainer remains the speed/distance source.
        self.bikeMetricValues["power"].setText(f"{power:.0f}")
        if cadence is not None:
            self.bikeMetricValues["cadence"].setText(f"{cadence:.0f}")
        if left is not None:
            self.pedalMetricValues["left"].setText(f"{left:.1f}")
            self.pedalMetricValues["right"].setText(f"{100 - left:.1f}")
        fields = [f"功率 {power} W"]
        if cadence is not None:
            fields.append(f"踏頻 {cadence:.1f} rpm")
        if left is not None:
            fields.append(f"左右 {left:.1f}% / {100-left:.1f}%")
        self.pedalTelemetryLabel.setText("踏板功率資訊：" + " · ".join(fields))
        if self.rideSession is not None:
            point = {key: data.get(key) for key in ("power_w", "cadence_rpm", "left_balance_percent", "right_balance_percent", "left_power_w", "right_power_w", "crank_revolutions")}
            point["time_s"] = self.rideTime()
            add_point(self.rideSession, "pedal", point)
            point["quality_status"] = "VALID"
            self.databaseAddSample("pedal", point["time_s"], point)
        if self.pedalRecorder:
            try:
                self.pedalRecorder.append(data)
            except OSError as exc:
                self.appendLog(f"踏板 CSV 寫入失敗：{exc}")
                self.closePedalRecording()


    def resistanceSet(self, value: float) -> None:
        self.commandedNativeResistance = value
        self.nativeResistanceLabel.setText(f"原生阻力：{value:g}")
        self.setTrainerStatus(f"原生阻力已設定為 {value:g}", "connected")

    def adjustFixedResistance(self, direction: int) -> None:
        self.resistanceSpin.setValue(self.resistanceSpin.value() + direction * self.resistanceStepLevels)
        self.trainerWorker.setFixedResistanceLevel(self.resistanceSpin.value())

    def updateResistanceStep(self, step: int) -> None:
        self.resistanceStepLevels = step
        self.resistanceSpin.setSingleStep(step)
        self.decreaseResistanceButton.setText(f"− {step} 級")
        self.increaseResistanceButton.setText(f"+ {step} 級")

    def fixedResistanceSet(self, level: int, nativeResistance: float) -> None:
        self.commandedNativeResistance = nativeResistance
        self.nativeResistanceLabel.setText(f"原生阻力：{nativeResistance:g}（固定模式 · 等級 {level}/30）")
        self.bikeMetricValues["resistance"].setText(f"{nativeResistance:g}")
        self.setTrainerStatus(f"固定阻力等級 {level}/30：原生阻力 {nativeResistance:g}", "connected")

    def updateTrainerTelemetry(self, data: BikeTelemetry) -> None:
        if data.distanceMeters is not None:
            self.lastTrainerDistanceMeters = data.distanceMeters
        fields: list[str] = []
        if data.speedKph is not None:
            fields.append(f"速度 {data.speedKph:.1f} km/h")
        if data.cadenceRpm is not None:
            fields.append(f"踏頻 {data.cadenceRpm:.1f} rpm")
        if data.powerWatts is not None:
            fields.append(f"功率 {data.powerWatts} W")
        if data.heartRate is not None:
            fields.append(f"心率 {data.heartRate} bpm")
        if data.distanceMeters is not None:
            fields.append(f"距離 {data.distanceMeters / 1000:.2f} km")
        if data.resistance is not None:
            fields.append(f"原生阻力 {data.resistance:g}")
        self.trainerTelemetryLabel.setText("阻力計資訊：" + (" · ".join(fields) or "等待踩踏…"))
        if data.speedKph is not None:
            self.bikeMetricValues["speed"].setText(f"{data.speedKph:.1f}")
        if data.cadenceRpm is not None and not self.pedalIsConnected:
            self.bikeMetricValues["cadence"].setText(f"{data.cadenceRpm:.0f}")
        if data.powerWatts is not None and not self.pedalIsConnected:
            self.bikeMetricValues["power"].setText(f"{data.powerWatts}")
        sessionDistanceMeters = self.sessionDistanceMeters(data.distanceMeters)
        if sessionDistanceMeters is not None:
            self.bikeMetricValues["distance"].setText(f"{sessionDistanceMeters / 1000:.2f}")
        if data.resistance is not None:
            self.bikeMetricValues["resistance"].setText(f"{data.resistance:g}")
        if self.rideSession is not None:
            point = {
                "time_s": self.rideTime(), "resistance_level": self.resistanceSpin.value(),
                "resistance_native_commanded": self.commandedNativeResistance,
                "resistance_native_reported": data.resistance, "power_watts": data.powerWatts,
                "speed_kph": data.speedKph, "cadence_rpm": data.cadenceRpm,
                "heart_rate_bpm": data.heartRate, "session_distance_meters": sessionDistanceMeters,
                "energy_kcal": data.energyKcal}
            add_point(self.rideSession, "trainer", point)
            self.databaseAddSample("trainer", point["time_s"], {
                "distance_km": None if sessionDistanceMeters is None else sessionDistanceMeters / 1000,
                "speed_kph": data.speedKph, "power_w": data.powerWatts, "cadence_rpm": data.cadenceRpm,
                "quality_status": "VALID"})
        if self.trainerRecorder:
            try:
                self.trainerRecorder.append(data,self.resistanceSpin.value(),self.commandedNativeResistance,sessionDistanceMeters,)
            except OSError as exc:
                self.appendLog(f"阻力計 CSV 輸出失敗：{exc}")
                self.trainerRecorder.close()
                self.trainerRecorder = None

    def resetBikeMetrics(self) -> None:
        for value in self.bikeMetricValues.values():
            value.setText("--")

    def beginCollectionDistance(self) -> None:
        """Start an isolated, plausibility-checked distance counter for this collection."""
        self.collectionDistanceBaselineMeters = self.lastTrainerDistanceMeters
        self.collectionLastTrainerDistanceMeters = self.lastTrainerDistanceMeters
        self.collectionDistanceMeters = 0
        self.collectionDistanceUpdatedAt = time.monotonic()
        self.distanceSessionActive = True
        self.bikeMetricValues["distance"].setText("0.00")

    def sessionDistanceMeters(self, totalDistanceMeters: int | None) -> int | None:
        if totalDistanceMeters is None:
            return self.collectionDistanceMeters
        if not self.distanceSessionActive:
            return totalDistanceMeters

        now = time.monotonic()
        previous = self.collectionLastTrainerDistanceMeters
        if previous is None:
            self.collectionDistanceBaselineMeters = totalDistanceMeters
            self.collectionLastTrainerDistanceMeters = totalDistanceMeters
            self.collectionDistanceUpdatedAt = now
            return self.collectionDistanceMeters

        elapsed = max(0.1, now - (self.collectionDistanceUpdatedAt or now))
        increment = totalDistanceMeters - previous
        # 72 km/h plus a short notification allowance.  This is well above a
        # rehabilitation ride but filters a stale/corrupt FTMS distance packet.
        maximum_increment = max(12, int(elapsed * 20) + 4)
        if increment < 0:
            # Some trainers reset their total counter. Rebase without adding a
            # negative distance, then continue accumulating the same session.
            self.collectionLastTrainerDistanceMeters = totalDistanceMeters
            self.collectionDistanceUpdatedAt = now
            return self.collectionDistanceMeters
        if increment > maximum_increment:
            if now - self.lastDistanceRejectAt >= 5:
                self.appendLog(
                    f"已略略阻力計距離異常跳值：+{increment} m / {elapsed:.1f} s"
                )
                self.lastDistanceRejectAt = now
            self.collectionDistanceUpdatedAt = now
            return self.collectionDistanceMeters

        self.collectionLastTrainerDistanceMeters = totalDistanceMeters
        self.collectionDistanceUpdatedAt = now
        self.collectionDistanceMeters = (self.collectionDistanceMeters or 0) + increment
        return self.collectionDistanceMeters

    def scanTrainer(self) -> None:
        self.scanCyclingDevices()

    def showTrainerDevices(self, devices: list[tuple[str, str]]) -> None:
        current = self.trainerDevices.currentText()
        self.trainerDevices.clear()
        for address, name in devices:
            self.trainerDevices.addItem(f"{name} · {address}", address)
        if not devices:
            self.trainerDevices.setEditText(current)
        else:
            self.trainerDevices.setCurrentIndex(0)
        self.setTrainerStatus(f"掃描完成：找到 {len(devices)} 個裝置", "idle")

    def trainerAddress(self) -> str:
        data = self.trainerDevices.currentData()
        if data:
            return str(data)
        text = self.trainerDevices.currentText().strip()
        return text.rsplit("·", 1)[-1].strip()

    def connectTrainer(self) -> None:
        address = self.trainerAddress()
        if not address:
            QMessageBox.warning(self, "缺少藍牙位址", "請掃描後選擇阻力計，或輸入 MAC 位址。")
            return
        self.setTrainerStatus("正在連線…", "working")
        self.trainerWorker.connectDevice(address)

    def trainerConnected(self, info: dict) -> None:
        resistance: NumericRange = info["resistance"]
        nativeMinimum = resistance.minimum
        nativeMaximum = resistance.maximum
        self.resistanceSpin.setRange(0, 30)
        self.resistanceSpin.setValue(0)
        self.updateResistanceStep(1)
        self.resistanceRangeLabel.setText(f"設備原生阻力：{nativeMinimum:g}–{nativeMaximum:g}；第 0–30 級自動換算")
        self.setTrainerControls(True)
        self.trainerIsConnected = True
        self.commandedNativeResistance = info["nativeResistance"]
        self.setTrainerConnectionState(True)
        self.resetBikeMetrics()
        self.bikeMetricValues["resistance"].setText(f"{info['nativeResistance']:g}")
        self.setTrainerStatus(f"已連線：{info['address']}，控制權已取得", "connected")
        if info["telemetryEnabled"]:
            self.trainerTelemetryLabel.setText("阻力計資訊：等待踩踏…")
        else:
            self.trainerTelemetryLabel.setText("阻力計資訊：此設備未提供 Indoor Bike Data")

    def trainerDisconnected(self) -> None:
        self.setTrainerControls(False)
        self.trainerIsConnected = False
        self.setTrainerConnectionState(False)
        self.resetBikeMetrics()
        self.setTrainerStatus("阻力計已中斷連線", "idle")
        self.trainerTelemetryLabel.setText("阻力計資訊：--")

    def trainerFailed(self, message: str) -> None:
        self.setTrainerControls(False)
        self.trainerIsConnected = False
        self.setTrainerConnectionState(False)
        self.setTrainerStatus(f"阻力計錯誤：{message}", "error")
        if "藍牙目前未開啟" in message:
            QMessageBox.warning(self, "Bluetooth 未開啟", message)

    def setTrainerControls(self, connected: bool) -> None:
        self.decreaseResistanceButton.setEnabled(connected)
        self.increaseResistanceButton.setEnabled(connected)
        self.connectTrainerButton.setEnabled(not connected)

    def setTrainerStatus(self, text: str, state: str) -> None:
        self.setStatusLabel(self.trainerStatus, text, state)
        self.appendLog(f"trainer | {text}")

    def setTrainerConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.trainerConnectionLabel, "阻力計", connected)

    def setGoProConnectionState(self, connected: bool, recording: bool = False) -> None:
        """Update the 攝影機 control state shown in the upper-right header."""
        self.goproIsConnected = connected
        if recording:
            # BLE write acknowledgement only confirms delivery to the camera;
            # this lightweight protocol has no encoding-state response.
            state, severity = "錄影指令已送出（未確認）", "warning"
            tooltip = "已送出開始錄影指令，正在等待攝影機的快門成功回覆。"
        elif connected:
            state, severity = "已連線（待命）", "success"
            tooltip = "已完成攝影機藍牙控制測試；開始收集時可直接同步開始錄影。"
        else:
            state, severity = "未連線", "danger"
            tooltip = "尚未建立可用的攝影機藍牙控制連線。"
        self.goproConnectionLabel.setText(f"● 攝影機：{state}")
        self.goproConnectionLabel.setToolTip(tooltip)
        FluentThemeManager.set_property(self.goproConnectionLabel, "fluentSeverity", severity)

    def setDatabaseConnectionState(self, connected: bool) -> None:
        self.setConnectionIndicator(self.databaseConnectionLabel, "資料庫", connected)

    def setEmgConnectionState(self, tested: bool) -> None:
        """Record whether the Trigno SDK test has succeeded."""
        self.emgTested = tested
        self.updateEmgConnectionIndicator()

    def updateEmgConnectionIndicator(self) -> None:
        """Show the EMG lifecycle without mistaking an idle SDK for failure."""
        if self.streamWorker and self.streamWorker.isRunning():
            state, severity = "串流中", "info"
        elif self.emgTested:
            state, severity = "已測試（待收集）", "success"
        else:
            state, severity = "未連線", "danger"
        self.emgConnectionLabel.setText(f"● EMG：{state}")
        FluentThemeManager.set_property(self.emgConnectionLabel, "fluentSeverity", severity)

    @staticmethod
    def setConnectionIndicator(label: QLabel, name: str, connected: bool) -> None:
        state = "連線" if connected else "未連線"
        label.setText(f"● {name}：{state}")
        FluentThemeManager.set_property(label, "fluentSeverity", "success" if connected else "danger")

    def editMappings(self) -> None:
        active_slots = {index + 1 for index, active in enumerate(self.sensorActive) if active}
        dialog = MappingEditorDialog(self.mappings, active_slots, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.mappings = dialog.mappings()
            saveMappings(MAPPING_PATH, self.mappings)
            self.updateSensorRows()
            self.syncMuscleMap()
            self.rebuildPlots()

    def saveInlineMappings(self) -> None:
        """Persist mappings edited directly in the EMG connection panel."""
        mappings = [
            SensorMapping(slot, chinese.text().strip(), english.text().strip(), abbreviation.text().strip())
            for slot, (chinese, english, abbreviation) in enumerate(self.inlineMappingEdits, 1)
        ]
        duplicates = duplicateMappingSlots(mappings)
        if duplicates:
            conflicts = "\n".join(
                f"{muscle.title()} ({side.upper() or '?'}): " + ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items()
            )
            QMessageBox.warning(
                self,
                "\u611f\u6e2c\u5668\u5c0d\u61c9",
                "\u767c\u73fe\u91cd\u8907\u7684\u808c\u8089\u8207\u5074\u5225\u5c0d\u61c9\uff1a\n" + conflicts,
            )
            return
        self.mappings = mappings
        saveMappings(MAPPING_PATH, self.mappings)
        self.updateSensorRows()
        self.syncMuscleMap()
        self.rebuildPlots()
        self.appendLog("\u5df2\u5132\u5b58 EMG \u611f\u6e2c\u5668\u5c0d\u61c9")

    def updateSensorRows(self) -> None:
        for index, mapping in enumerate(self.mappings):
            side = mapping.abbreviation.rsplit("-", 1)[-1] if "-" in mapping.abbreviation else ""
            title = f"{side} {mapping.english_name.upper()}" if side in {"R", "L"} else mapping.english_name.upper()
            detected = self.sensorActive[index]
            self.sensorChecks[index].setLabels(f"S{mapping.slot:02d} · {mapping.chinese_name}", mapping.english_name)
            serial = self.sensorSerials.get(mapping.slot, "未取得 SID")
            tooltip = f"S{mapping.slot:02d} · {title}\nSID：{serial}"
            self.sensorRows[index].setToolTip(tooltip)
            self.sensorIcons[index].setToolTip(tooltip)
            self.sensorChecks[index].setToolTip(tooltip)
            self.sensorRows[index].setVisible(True)
            self.updateBaselineRow(index)
        if hasattr(self, "activeSlotsLabel"):
            active_text = ", ".join(f"S{index + 1:02d}" for index, active in enumerate(self.sensorActive) if active)
            self.activeSlotsLabel.setText(f"目前啟用感測器：{active_text or '無'}")
        self.syncMuscleMap()

    def mappingIdentity(self, channel: int) -> str:
        mapping = self.mappings[channel]
        return f"{mapping.english_name.strip()}|{mapping.abbreviation.strip()}"

    def updateBaselineRow(self, channel: int, current_mv: float | None = None) -> None:
        slot = channel + 1
        result = self.baselineManager.evaluate(slot, current_mv, self.mappingIdentity(channel), self.sensorSerials.get(slot, "Unknown SID"), self.currentSampleRate, self.filterSettings)
        status_changed = self.muscleStatuses.get(slot) != result["state"]
        self.muscleStatuses[slot] = result["state"]
        current = "--" if result["current_mv"] is None else f"{result['current_mv']:.3f} mV"
        baseline = "--" if result["baseline_mv"] is None else f"{result['baseline_mv']:.3f} mV"
        delta = "--" if result["delta_mv"] is None else f"{result['delta_mv']:+.3f} mV"
        percent = "--" if result["percent"] is None else f"{result['percent']:+.1f}%"
        ratio = "--" if result["percent"] is None else f"{1 + result['percent'] / 100:.2f} 倍"
        if channel < len(self.baselineMetricLabels):
            comparison = f"差異：{delta}｜變化：{percent}"
            self.baselineMetricLabels[channel].setText(f"目前 RMS：{current}\n基準：{baseline}｜{comparison}")
            self.baselineStatusLabels[channel].setText(result["text"])
            self.baselineStatusLabels[channel].setToolTip("狀態是相對基準偏離程度，並非醫療診斷。")
        FluentThemeManager.set_property(self.sensorRows[channel], "emgStatus", result["state"])
        if status_changed:
            self.syncMuscleMap()

    def startBaselineCalibration(self) -> None:
        if self.streamWorker and self.streamWorker.isRunning():
            QMessageBox.information(self, "基準測量", "請先停止目前的串流，再開始基準測量。")
            return
        if not self.useEmgCheck.isChecked() or not any(self.sensorActive):
            QMessageBox.warning(self, "基準測量", "請先連線測試，確認至少一個 EMG 感測器啟用。")
            return
        protocol, accepted = QInputDialog.getText(self, "基準測量說明", "動作/姿勢/阻力/踏頻（待臨床確認）：", text=self.baselineManager.protocol)
        if not accepted:
            return
        self.baselineManager.protocol = protocol.strip()
        self.calibrationOnly = True
        self.calibrationSession = None
        self.startStream()

    def importBaselineCsv(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "匯入原始 EMG 基準 CSV", self.pathEdit.text().strip(), "CSV files (*.csv)")
        if not filename:
            return
        path = Path(filename)
        try:
            _, columns, sample_rate = load_raw_emg_csv(path)
            channel_columns: list[tuple[int, str]] = []
            for index, mapping in enumerate(self.mappings):
                header = next((name for name in columns if matchesCsvColumn(mapping, name)), None)
                if header is not None:
                    channel_columns.append((index, header))
            if not channel_columns:
                raise ValueError("CSV 欄位沒有符合目前肌肉對應的 EMG 名稱。")
            frames = np.column_stack([columns[header] for _, header in channel_columns])
            filtered = EMGPreprocessor(len(channel_columns), sample_rate, self.filterSettings).process(frames)
            session = CalibrationSession(sample_rate, set(range(len(channel_columns))))
            session.add(filtered)
            if not session.complete:
                raise ValueError("CSV 的前 20 秒資料不足。")
            imported, invalid = [], []
            for compact_channel, (channel, _header) in enumerate(channel_columns):
                rms_mv = session.rms_mv(compact_channel)
                if rms_mv is None or rms_mv <= 1e-9:
                    invalid.append(f"S{channel + 1:02d}")
                    continue
                self.baselineManager.set_imported(channel + 1, rms_mv, self.mappingIdentity(channel), sample_rate, self.filterSettings, path.name)
                imported.append(f"S{channel + 1:02d}")
            if not imported:
                raise ValueError("CSV 沒有可建立的有效基準。")
            self.baselineManager.save()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "匯入基準 CSV 失敗", str(exc))
            return
        self.currentSampleRate = sample_rate
        self.calibrationLabel.setText(f"已匯入 {path.name}：{', '.join(imported)}" + (f"；無效：{', '.join(invalid)}" if invalid else ""))
        self.updateSensorRows()
        self.showBaselineSettings()

    def editManualBaseline(self) -> None:
        choices = [f"S{index + 1:02d} {mapping.abbreviation or mapping.english_name}" for index, mapping in enumerate(self.mappings)]
        choice, accepted = QInputDialog.getItem(self, "手動修改基準", "肌肉/通道：", choices, 0, False)
        if not accepted:
            return
        channel = choices.index(choice)
        previous = self.baselineManager.records.get(channel + 1)
        value, accepted = QInputDialog.getDouble(self, "手動修改基準", "RMS (mV)：", previous.rms_mv if previous else 0.1, 0.000001, 10000, 6)
        if not accepted:
            return
        reason, accepted = QInputDialog.getText(self, "手動修改基準", "修改原因：")
        if not accepted:
            return
        try:
            self.baselineManager.set_manual(channel + 1, value, self.mappingIdentity(channel), self.sensorSerials.get(channel + 1, "Unknown SID"), self.currentSampleRate, self.filterSettings, reason.strip())
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "無法保存基準", str(exc))
            return
        self.updateBaselineRow(channel)

    def showBaselineSettings(self) -> None:
        dialog = BaselineSettingsDialog(self.mappings, self.baselineManager, self.filterSettings, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        warning, focus = dialog.values()
        for slot, value in dialog.baseline_changes().items():
            channel = slot - 1
            previous = self.baselineManager.records.get(slot)
            serial = previous.sensor_serial if previous else self.sensorSerials.get(slot, "Manual mapping edit")
            if serial == "Unknown SID":
                serial = "Manual mapping edit"
            self.baselineManager.set_manual(
                slot, value, self.mappingIdentity(channel), serial,
                self.currentSampleRate, self.filterSettings, "Edited in baseline settings",
            )
        self.baselineManager.thresholds_enabled = True
        self.baselineManager.warning_percent = warning
        self.baselineManager.focus_percent = focus
        self.baselineManager.save()
        self.updateSensorRows()
        if dialog.import_requested:
            self.importBaselineCsv()

    def editFilterSettings(self) -> None:
        highpass, accepted = QInputDialog.getDouble(self, "EMG 濾波設定", "高通截止頻率 (Hz)：", self.filterSettings.highpass_hz, 0.1, 1000, 1)
        if not accepted:
            return
        lowpass, accepted = QInputDialog.getDouble(self, "EMG 濾波設定", "低通截止頻率 (Hz)：", self.filterSettings.lowpass_hz, highpass + 0.1, 10000, 1)
        if not accepted:
            return
        try:
            settings = FilterSettings(highpass, lowpass)
            settings.validate(self.currentSampleRate)
        except ValueError as exc:
            QMessageBox.warning(self, "濾波設定無效", str(exc))
            return
        self.filterSettings = settings
        self.baselineManager.filter_settings = {"highpass_hz": highpass, "lowpass_hz": lowpass, "notch_hz": None}
        self.baselineManager.save()
        self.updateSensorRows()

    def applyStyles(self) -> None:
        FluentThemeManager(QApplication.instance()).apply()

    def subjectInfo(self) -> SubjectInfo:
        return SubjectInfo(patientId=self.patientIdEdit.text().strip(),name=self.patientNameEdit.text().strip(),gender=self.genderCombo.currentText(),)

    def loadArchivePatients(self) -> None:
        try:
            patients = Archive(self.databaseConfig()).list_patients()
        except Exception as exc:
            self.appendLog(f"病人清單載入失敗：{exc}")
            return
        previous = self.patientIdEdit.text()
        self.archivePatientCombo.blockSignals(True)
        self.archivePatientCombo.clear()
        self.archivePatientCombo.addItem("選擇病人", None)
        for person in patients:
            self.archivePatientCombo.addItem(f"{person['name']} · {person['patient_id']}", person)
        self.archivePatientCombo.blockSignals(False)
        for index in range(1, self.archivePatientCombo.count()):
            if self.archivePatientCombo.itemData(index)["patient_id"] == previous:
                self.archivePatientCombo.setCurrentIndex(index)
                break
        else:
            self.archivePatientSelected()

    def archivePatientSelected(self, *_args) -> None:
        person = self.archivePatientCombo.currentData() or {}
        self.patientIdEdit.setText(person.get("patient_id", ""))
        self.patientNameEdit.setText(person.get("name", ""))
        self.genderCombo.setCurrentText(person.get("gender", "未指定"))

    def collectionSetupIsValid(self) -> bool:
        subject = self.subjectInfo()
        missing = []
        if not subject.patientId:
            missing.append("病人 ID")
        if not subject.name:
            missing.append("姓名")
        if missing:
            QMessageBox.warning(self,"缺少病人資訊","開始收集前請填寫：" + "、".join(missing),)
            self.tabs.setCurrentWidget(self.settingsTab)
            (self.patientIdEdit if not subject.patientId else self.patientNameEdit).setFocus()
            return False

        duplicates = duplicateMappingSlots(self.mappings)
        if duplicates:
            conflicts = "\n".join(f"{muscle.title()} ({side.upper() or '?' }): "+ ", ".join(f"S{slot:02d}" for slot in slots)
                for (muscle, side), slots in duplicates.items()
            )
            QMessageBox.warning(self,"重複肌肉對應","請先修正感測器對應，再開始收集：\n" + conflicts,)
            self.tabs.setCurrentWidget(self.settingsTab)
            return False
        return True

    def outputPathForCollection(self, directory: Path) -> Path:
        patientId = self.subjectInfo().patientId
        invalid = '\\/:*?"<>|'
        safeId = "".join(character for character in patientId if character not in invalid).strip()
        date = datetime.now().strftime("%Y%m%d")
        candidate = directory / f"{safeId}_EMG_{date}.csv"
        suffix = 2
        while candidate.exists() or infoOutputPath(candidate).exists() or analysisOutputPath(candidate).exists() or (
            self.useTrainerCheck.isChecked() and self.trainerIsConnected and trainerOutputPath(candidate).exists()) or (
            self.usePedalCheck.isChecked() and self.pedalIsConnected and (
                pedalOutputPath(candidate).exists() or pedalOutputPath(candidate).with_suffix(".fit").exists())):
            candidate = directory / f"{safeId}_EMG_{date}_{suffix:02d}.csv"
            suffix += 1
        return candidate

    def setStatusLabel(self, label: QLabel, text: str, state: str) -> None:
        label.setText(text)
        severity = {"idle": "info", "working": "warning", "connected": "success", "streaming": "info", "error": "danger"}[state]
        FluentThemeManager.set_property(label, "fluentSeverity", severity)

    def setEmgStatus(self, text: str, state: str) -> None:
        self.setStatusLabel(self.emgStatus, text, state)
        self.appendLog(f"EMG | {text}")

    def appendLog(self, message: str) -> None:
        if hasattr(self, "eventLog"):
            self.eventLog.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def testGoProConnection(self) -> None:
        """Discover and connect to the camera without changing its recording state."""
        if self.goproWorker and self.goproWorker.isRunning():
            QMessageBox.information(self, "攝影機連線", "攝影機指令正在執行，請稍候。")
            return
        self.goproTestButton.setEnabled(False)
        self.goproDisconnectButton.setEnabled(True)
        self.goproStatusLabel.setText("正在以藍牙配對並驗證攝影機控制權限…")
        self.goproConnectionLabel.setText("● 攝影機：連線中")
        FluentThemeManager.set_property(self.goproConnectionLabel, "fluentSeverity", "warning")
        self.appendLog("攝影機 | pairing and verifying Bluetooth control…")
        # Keep this verified BLE session open.  Collection can then send the
        # shutter command immediately instead of reconnecting several seconds
        # later.
        worker = GoProCommandWorker("connect", self.goproAddress or None, self.goproIdentifier or None)
        self.goproWorker = worker
        worker.completed.connect(self.goProCommandCompleted)
        worker.failed.connect(lambda message: self.goProCommandFailed("verify", message))
        worker.diagnostic.connect(self.goProDiagnostic)
        worker.finished.connect(self.goProCommandFinished)
        worker.start()

    def disconnectGoPro(self) -> None:
        """Close the held BLE session without affecting other collection data."""
        if self.goproWorker and self.goproWorker.isRunning() and self.goproWorker.action == "connect":
            self.appendLog("攝影機 | disconnect requested")
            self.goproStatusLabel.setText("正在中斷攝影機藍牙連線…")
            self.goproDisconnectButton.setEnabled(False)
            self.goproWorker.request_disconnect()
            return
        self.setGoProConnectionState(False)
        self.goproDisconnectButton.setEnabled(False)
        self.goproStatusLabel.setText("攝影機已中斷")

    def downloadLatestGoProVideo(self) -> None:
        """Automatically join the 攝影機 AP, then download its latest MP4."""
        self.connectGoProWifiThen("latest")

    def chooseGoProVideoToDownload(self) -> None:
        """Automatically join the 攝影機 AP, then show the camera MP4 list."""
        self.connectGoProWifiThen("choose")

    def connectGoProWifiThen(self, action: str) -> None:
        """Switch Windows to the paired camera AP before a media operation."""
        if self.goproDownloadWorker and self.goproDownloadWorker.isRunning():
            QMessageBox.information(self, "攝影機 影片", "影片正在下載中。")
            return
        if (self.goproMediaListWorker and self.goproMediaListWorker.isRunning()) or (
            self.goproWifiWorker and self.goproWifiWorker.isRunning()
        ):
            return
        if not self.goproWifiSsid or not self.goproWifiPassword:
            QMessageBox.warning(self, "攝影機 Wi-Fi", "請先按「配對並測試攝影機」，讓程式讀取相機的 Wi-Fi 資訊。")
            return
        self.goproDownloadButton.setEnabled(False)
        self.goproChooseVideoButton.setEnabled(False)
        self.goproWifiNextAction = action
        self.goproDownloadStatusLabel.setText("正在自動連線至攝影機 Wi-Fi…")
        self.appendLog(f"攝影機 | 正在連線至攝影機 Wi-Fi：{self.goproWifiSsid}")
        worker = GoProWifiConnectWorker(self.goproWifiSsid, self.goproWifiPassword)
        self.goproWifiWorker = worker
        worker.connected.connect(self.goProWifiConnected)
        worker.failed.connect(self.goProWifiFailed)
        worker.finished.connect(self.goProWifiFinished)
        worker.start()

    def goProWifiConnected(self) -> None:
        action, self.goproWifiNextAction = self.goproWifiNextAction, None
        self.appendLog("攝影機 | Windows connected to 攝影機 Wi-Fi")
        if action == "latest":
            self.downloadGoProVideo(None)
            return
        self.goproDownloadStatusLabel.setText("已連上攝影機 Wi-Fi；正在讀取影片清單…")
        worker = GoProMediaListWorker()
        self.goproMediaListWorker = worker
        worker.completed.connect(self.goProMediaListReady)
        worker.failed.connect(self.goProMediaListFailed)
        worker.finished.connect(self.goProMediaListFinished)
        worker.start()

    def goProWifiFailed(self, message: str) -> None:
        self.goproWifiNextAction = None
        self.goproDownloadStatusLabel.setText("無法自動連上攝影機 Wi-Fi；請確認相機開機且在附近")
        self.appendLog(f"攝影機 | 自動連線 Wi-Fi 失敗：{message or 'Unknown error'}")

    def goProWifiFinished(self) -> None:
        worker, self.goproWifiWorker = self.goproWifiWorker, None
        if worker:
            worker.deleteLater()
        if not (self.goproDownloadWorker and self.goproDownloadWorker.isRunning()) and not (
            self.goproMediaListWorker and self.goproMediaListWorker.isRunning()
        ):
            self.goproDownloadButton.setEnabled(True)
            self.goproChooseVideoButton.setEnabled(True)

    def readGoProMediaList(self) -> None:
        """Fetch the list once Wi-Fi has been connected by the preceding worker."""
        if self.goproMediaListWorker and self.goproMediaListWorker.isRunning():
            return
        self.goproDownloadStatusLabel.setText("正在讀取 攝影機 影片清單…")
        self.appendLog("攝影機 | reading media list from the connected 攝影機 Wi-Fi")
        worker = GoProMediaListWorker()
        self.goproMediaListWorker = worker
        worker.completed.connect(self.goProMediaListReady)
        worker.failed.connect(self.goProMediaListFailed)
        worker.finished.connect(self.goProMediaListFinished)
        worker.start()

    def goProMediaListReady(self, videos: object) -> None:
        selections = list(videos)
        labels = [f"{folder}/{name}" for folder, name in selections]
        label, accepted = QInputDialog.getItem(
            self, "選擇 攝影機 影片", "MP4 影片：", labels, 0, False,
        )
        if not accepted:
            self.goproDownloadStatusLabel.setText("已取消選擇影片")
            return
        selection = selections[labels.index(label)]
        self.downloadGoProVideo(selection)

    def goProMediaListFailed(self, message: str) -> None:
        self.goproDownloadStatusLabel.setText("讀取影片清單失敗；請確認 Windows 已連上 攝影機 Wi-Fi")
        self.appendLog(f"攝影機 | 讀取影片清單失敗：{message or 'Unknown error'}")

    def goProMediaListFinished(self) -> None:
        worker, self.goproMediaListWorker = self.goproMediaListWorker, None
        if worker:
            worker.deleteLater()
        if not (self.goproDownloadWorker and self.goproDownloadWorker.isRunning()):
            self.goproDownloadButton.setEnabled(True)
            self.goproChooseVideoButton.setEnabled(True)

    def downloadGoProVideo(self, selection: tuple[str, str] | None) -> None:
        """Download one selected video, or the newest video when selection is None."""
        if self.goproDownloadWorker and self.goproDownloadWorker.isRunning():
            QMessageBox.information(self, "攝影機 影片", "影片正在下載中。")
            return
        output_directory = self.pathEdit.text().strip()
        if not output_directory:
            self.goproDownloadStatusLabel.setText("請先在「設定 → 資料輸出」選擇 CSV 與影片共用的資料夾")
            return
        root = Path(output_directory).expanduser()
        destination = root
        collision = "rename"
        if selection and (destination / selection[1]).exists():
            prompt = QMessageBox(self)
            prompt.setWindowTitle("同名 攝影機 影片")
            prompt.setText(f"{selection[1]} 已存在。要如何處理？")
            overwrite = prompt.addButton("覆寫", QMessageBox.ButtonRole.DestructiveRole)
            rename = prompt.addButton("重新命名", QMessageBox.ButtonRole.AcceptRole)
            skip = prompt.addButton("略過", QMessageBox.ButtonRole.RejectRole)
            prompt.exec()
            clicked = prompt.clickedButton()
            collision = "overwrite" if clicked is overwrite else "skip" if clicked is skip else "rename"
            if collision == "skip":
                self.goproDownloadStatusLabel.setText(f"已略過：{selection[1]}")
                self.goproDownloadButton.setEnabled(True)
                self.goproChooseVideoButton.setEnabled(True)
                return
        self.goproDownloadButton.setEnabled(False)
        self.goproChooseVideoButton.setEnabled(False)
        title = "選擇的影片" if selection else "最新影片"
        self.goproDownloadName = selection[1] if selection else "最新影片"
        self.goproDownloadStartedAt = time.monotonic()
        self.goproDownloadStatusLabel.setText(f"正在下載{title}…")
        self.appendLog(f"攝影機 | 正在從已連線的攝影機 Wi-Fi 下載{title}")
        worker = GoProDownloadWorker(destination, selection, collision)
        self.goproDownloadWorker = worker
        worker.file_started.connect(self.goProDownloadFileStarted)
        worker.progress.connect(self.goProDownloadProgress)
        worker.completed.connect(self.goProDownloadCompleted)
        worker.failed.connect(self.goProDownloadFailed)
        worker.finished.connect(self.goProDownloadFinished)
        worker.start()

    def goProDownloadFileStarted(self, name: str) -> None:
        self.goproDownloadName = name
        self.goproDownloadStatusLabel.setText(f"正在下載：{name}")

    def goProDownloadProgress(self, received: int, total: int) -> None:
        elapsed = max(time.monotonic() - self.goproDownloadStartedAt, 0.001)
        if total:
            rate = received / elapsed
            remaining = max(0, int((total - received) / rate)) if rate else 0
            self.goproDownloadStatusLabel.setText(
                f"正在下載：{self.goproDownloadName} — {received / total:.0%}，剩約 {remaining} 秒"
            )
        else:
            self.goproDownloadStatusLabel.setText(
                f"正在下載：{self.goproDownloadName} — {received / 1024 / 1024:.1f} MB"
            )

    def goProDownloadCompleted(self, path: object) -> None:
        video_path = Path(str(path))
        self.goproDownloadStatusLabel.setText(f"已下載：{video_path.name}")
        self.appendLog(f"攝影機 | 已下載影片：{video_path}")
        session = self.rideSession or self.latestRideSession
        if session is not None:
            session.setdefault("gopro", {})["downloaded_video"] = str(video_path)
            # A collection that has already ended has had its analysis JSON
            # written. Update that metadata after a later download as well.
            if self.rideSession is None and self.collectionOutput:
                try:
                    save_session(analysisOutputPath(self.collectionOutput), session)
                except (OSError, ValueError) as exc:
                    self.appendLog(f"攝影機 | 無法將影片連結寫入分析 JSON：{exc}")

    def goProDownloadFailed(self, message: str) -> None:
        detail = message or "Unknown download error"
        self.goproDownloadStatusLabel.setText(
            "下載失敗：攝影機 Wi-Fi 無回應。請在 Windows Wi-Fi 選取相機的 GP-xxxx 網路後再試"
        )
        self.appendLog(f"攝影機 | 影片下載失敗：{detail}")

    def goProDownloadFinished(self) -> None:
        worker, self.goproDownloadWorker = self.goproDownloadWorker, None
        if worker:
            worker.deleteLater()
        self.goproDownloadButton.setEnabled(True)
        self.goproChooseVideoButton.setEnabled(True)

    def setStreaming(self, streaming: bool) -> None:
        testing = self.testWorker is not None
        self.startButton.setEnabled(not streaming and not testing)
        self.calibrateButton.setEnabled(not streaming and not testing)
        self.stopButton.setEnabled(streaming)
        self.discardButton.setEnabled(streaming or self.collectedRideSession is not None)
        self.hostEdit.setEnabled(not streaming and not testing)
        self.testButton.setEnabled(not streaming and not testing)
        self.durationMinutesSpin.setEnabled(not streaming)
        self.durationSecondsSpin.setEnabled(not streaming)
        self.pathEdit.setEnabled(not streaming)
        self.browseButton.setEnabled(not streaming)
        self.recordingIndicator.setVisible(streaming)
        if hasattr(self, "goproTestButton"):
            self.goproTestButton.setEnabled(not streaming and not (self.goproWorker and self.goproWorker.isRunning()))
        if hasattr(self, "goproDisconnectButton"):
            self.goproDisconnectButton.setEnabled(
                not streaming and bool(self.goproWorker and self.goproWorker.isRunning())
            )
        for check in (self.useEmgCheck, self.useTrainerCheck, self.usePedalCheck, self.useGoProCheck):
            check.setEnabled(not streaming)

    def chooseOutput(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "選擇 CSV 輸出資料夾", self.pathEdit.text())
        if directory:
            self.pathEdit.setText(directory)
            self.saveOutputDirectory()

    def saveOutputDirectory(self) -> None:
        self.settings.setValue("output/directory", self.pathEdit.text().strip())

    def testConnection(self) -> None:
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
            return
        if not self.patientIdEdit.text().strip():
            QMessageBox.warning(self, "缺少病人 ID", "請先在設定頁填寫病人 ID，再開始收集。")
            self.tabs.setCurrentWidget(self.settingsTab)
            self.patientIdEdit.setFocus()
            return
        if not self.patientIdEdit.text().strip():
            QMessageBox.warning(self, "缺少病人 ID", "請先在設定頁填寫病人 ID，再開始收集。")
            self.tabs.setCurrentWidget(self.settingsTab)
            self.patientIdEdit.setFocus()
            return
        self.setEmgStatus("正在測試連線…", "working")
        self.testWorker = ConnectionWorker(host)
        self.testWorker.connected.connect(lambda banner: self.emgStatus.setToolTip(banner))
        self.testWorker.base_ready.connect(lambda serial: self.baseLabel.setText(f"Base: {serial}"))
        self.testWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.testWorker.failed.connect(self.emgConnectionFailed)
        self.testWorker.finished.connect(self.testFinished)
        self.setStreaming(False)
        self.testWorker.start()

    def testFinished(self) -> None:
        worker, self.testWorker = self.testWorker, None
        if worker:
            worker.deleteLater()
        if not self.emgStatus.text().startswith("連線失敗"):
            self.setEmgStatus("已連線；啟用感測器已更新", "connected")
            self.setEmgConnectionState(True)
        self.setStreaming(False)

    def emgConnectionFailed(self, message: str) -> None:
        self.setEmgStatus(f"連線失敗：{message}", "error")
        self.setEmgConnectionState(False)

    def updateSensorStates(self, paired: list[int], active: list[int], serials: dict[int, str]) -> None:
        self.sensorActive = [index + 1 in active for index in range(CHANNEL_COUNT)]
        self.sensorSerials = serials
        if self.rideSession is not None:
            self.rideSession["channels"] = {
                str(slot): {"mapping_name": self.mappingIdentity(slot - 1),
                            "label": self.mappings[slot - 1].chinese_name,
                            "sensor_serial": serials.get(slot, "Unknown SID")}
                for slot in active}
        self.updateSensorRows()
        for index, check in enumerate(self.sensorChecks):
            check.setChecked(self.sensorActive[index])
        if self.calibrationOnly and self.preprocessor is not None and self.calibrationSession is None:
            self.calibrationSession = CalibrationSession(self.currentSampleRate, {index for index, enabled in enumerate(self.sensorActive) if enabled})

    def rideTime(self) -> float:
        return max(0.0, time.monotonic() - self.rideStartedAt)

    def beginRideAnalysis(self) -> None:
        # A ride owns its archive ID before its asynchronous worker starts.
        self.archiveLastTestId = prefixed_id("TEST")
        self.pendingAutoAngleAnalysis = False
        self.angleEmgPath = None
        self.anglePedalPath = None
        self.angleAnalysisResult = None
        self.updateAngleFileStatus()
        self.angleOverviewLabel.setText("本次收集完成後，會在這裡顯示肌電與踩踏相位聯合分析。")
        self.angleStatusLabel.setText("等待本次 EMG 與踏板資料完成收集。")
        self.angleComparisonTable.setRowCount(0)
        self.clearAngleReportTables()
        self.discardCurrentCollection = False
        self.databaseLastTestId = None
        self.angleExportButton.setEnabled(False)
        baselines = {str(slot): asdict(record) for slot, record in self.baselineManager.records.items()}
        self.rideSession = new_session(asdict(self.collectionSubject), baselines)
        self.rideSession["filter"] = asdict(self.filterSettings)
        self.rideSession["channels"] = {}
        self.rideSession["raw_files"] = {
            name: path.name for name, path in (("emg", self.collectionOutput),
                ("pedal", self.pedalOutput), ("trainer", self.trainerOutput))
            if path is not None and (name != "emg" or self.useEmgCheck.isChecked())}
        self.rideStartedAt = time.monotonic()
        self.lastEmgAnalysisAt = 0.0
        self.latestRideSession = self.rideSession
        self.collectedRideSession = self.rideSession
        self.currentSessionSummaryButton.setEnabled(True)
        self.refreshRideAnalysis()
        self.refreshComparison()

    def startAutomaticAngleAnalysis(self) -> None:
        if not self.pendingAutoAngleAnalysis:
            return
        if self.angleEmgPath is None or self.anglePedalPath is None:
            self.pendingAutoAngleAnalysis = False
            self.updateAngleFileStatus()
            return
        if self.angleAnalysisWorker is not None:
            self.angleStatusLabel.setText("前次相位聯合分析完成後，會分析新匯入的資料。")
            return
        self.pendingAutoAngleAnalysis = False
        self.updateAngleFileStatus()
        self.tabs.setCurrentWidget(self.collectionTab)
        self.summaryTabs.setCurrentIndex(2)
        self.angleViews.setCurrentIndex(0)
        self.angleOverviewLabel.setText("正在分析 EMG 與踏板角度…")
        self.runAngleAnalysis()

    def queueAngleAnalysis(self) -> None:
        if self.angleEmgPath is not None and self.anglePedalPath is not None:
            self.pendingAutoAngleAnalysis = True
            QTimer.singleShot(0, self.startAutomaticAngleAnalysis)

    def finishRideAnalysis(self) -> None:
        session, self.rideSession = self.rideSession, None
        if session is None:
            return
        if self.discardCurrentCollection:
            self.finalizeDiscardedTest(active_database_writer=True)
            return
        result = analyze(session)
        self.finishDatabaseRecording(result)
        self.latestRideSession = session
        self.collectedRideSession = session
        self.angleEmgPath = self.collectionOutput if self.collectionOutput and self.collectionOutput.is_file() else None
        self.anglePedalPath = self.pedalOutput if self.pedalOutput and self.pedalOutput.is_file() else None
        self.updateAngleFileStatus()
        if self.collectionOutput:
            path = analysisOutputPath(self.collectionOutput)
            try:
                session["aligned_1s"] = result["aligned"]
                save_session(path, session)
                self.appendLog(f"騎乘分析已保存：{path.name}")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "無法保存分析", str(exc))
        files = {kind: path for kind, path in (
            ("emg", self.collectionOutput), ("pedal", self.pedalOutput),
            ("trainer", self.trainerOutput), ("info", self.infoOutput))
            if path is not None and path.is_file()}
        worker = ArchiveWorker(self.databaseConfig(), session, files,
                               self.databaseLastTestId or self.archiveLastTestId)
        self.archiveWorkers.add(worker)
        worker.archived.connect(lambda identifier: self.appendLog(f"SQL Server 原始資料已保存：{identifier}"))
        worker.failed.connect(lambda message: self.appendLog(f"SQL Server 原始資料保存失敗，請保留本機 CSV：{message}"))
        worker.finished.connect(lambda: self.archiveWorkers.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        self.refreshRideAnalysis()
        self.refreshComparison()

    def requestDiscardCurrentTest(self) -> None:
        """Ask before permanently deleting the current collection and its database rows."""
        has_current_test = self.rideSession is not None or self.collectedRideSession is not None
        if not has_current_test:
            QMessageBox.information(self, "捨棄本次測試", "目前沒有可捨棄的本次測試。")
            return
        choice = QMessageBox.question(
            self, "捨棄本次測試",
            "將刪除本次測試的 EMG、踏板、阻力計、資訊與分析檔，並刪除資料庫中的同次測試資料。\n\n此操作無法復原，是否繼續？",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if choice != QMessageBox.StandardButton.Discard:
            return
        if self.rideSession is not None:
            self.discardCurrentCollection = True
            self.discardButton.setEnabled(False)
            self.stopStream()
            return
        self.finalizeDiscardedTest(active_database_writer=False)

    def finalizeDiscardedTest(self, active_database_writer: bool) -> None:
        """Clear the rejected session only after all recorders have stopped."""
        archive_id = self.databaseTestId or self.databaseLastTestId or self.archiveLastTestId
        for worker in tuple(self.archiveWorkers):
            if worker.test_id == archive_id and worker.isRunning():
                worker.wait()
        if archive_id:
            try:
                Archive(self.databaseConfig()).delete_recording(archive_id)
            except Exception as exc:
                self.appendLog(f"SQL Server 歸檔刪除失敗：{exc}")
        self.discardDatabaseRecording(active_database_writer)
        self.archiveLastTestId = None
        removed, failures = self.removeCurrentCollectionFiles()
        self.discardCurrentCollection = False
        self.rideSession = None
        self.latestRideSession = None
        self.collectedRideSession = None
        self.latestPedalRecords = []
        self.summaryRecords = []
        self.angleEmgPath = None
        self.anglePedalPath = None
        self.angleAnalysisResult = None
        self.pendingAutoAngleAnalysis = False
        self.angleExportButton.setEnabled(False)
        self.updateAngleFileStatus()
        self.currentSessionSummaryButton.setEnabled(False)
        self.exportReportButton.setEnabled(False)
        self.rideMuscleTable.setRowCount(0)
        self.rideStateLabel.setText("本次測試已捨棄")
        FluentThemeManager.set_property(self.rideStateLabel, "fluentSeverity", "warning")
        self.summarySourceLabel.setText("本次測試已捨棄，未保留分析資料")
        self.refreshSummaryAnalysis()
        self.refreshComparison()
        self.discardButton.setEnabled(False)
        detail = f"已刪除 {removed} 個檔案" if removed else "沒有需要刪除的檔案"
        if failures:
            detail += f"；{len(failures)} 個檔案無法刪除"
        self.appendLog(f"本次測試已捨棄：{detail}")
        if failures:
            QMessageBox.warning(self, "部分檔案未刪除", "\n".join(failures))

    def removeCurrentCollectionFiles(self) -> tuple[int, list[str]]:
        """Delete only files derived from this collection's exact output names."""
        paths = {path for path in (self.collectionOutput, self.infoOutput, self.trainerOutput, self.pedalOutput) if path}
        if self.collectionOutput:
            paths.add(analysisOutputPath(self.collectionOutput))
        if self.pedalOutput:
            paths.add(self.pedalOutput.with_suffix(".fit"))
        removed, failures = 0, []
        for path in paths:
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                failures.append(f"{path.name}：{exc}")
        return removed, failures

    def refreshLiveRideSummary(self) -> None:
        if self.rideSession is None:
            return
        if self.tabs.currentWidget() is self.summaryTab:
            if self.summaryTabs.currentIndex() == 0:
                self.refreshRideAnalysis()
            elif self.summaryTabs.currentIndex() == 1 and self.pedalRecorder is not None and self.pedalSummarySource == "current":
                if len(self.summaryRecords) != len(self.pedalRecorder.records):
                    self.summaryRecords = list(self.pedalRecorder.records)
                    self.refreshSummaryAnalysis()
        elif self.tabs.currentWidget() is self.comparisonTab and self.referenceRideSession is not None:
            self.refreshComparison()

    def refreshRideAnalysis(self) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            return
        normalized = False
        result = analyze(session, normalized)
        self.rideEmptyLabel.hide()
        self.rideMuscleTable.setRowCount(len(result["emg"]))
        for row, (slot, data) in enumerate(result["emg"].items()):
            channel = session.get("channels", {}).get(slot, {})
            label = f"S{int(slot):02d} {channel.get('label') or channel.get('mapping_name') or ''}"
            def fmt(value, suffix="", digits=3):
                return "--" if value is None else f"{value:.{digits}f}{suffix}"
            compare_value = fmt(data["comparison"]["mean"], " 倍" if normalized else " mV")
            phase = f"{fmt(data['early_rms_mv'])} → {fmt(data['late_rms_mv'])} mV"
            status = "、".join(data["quality"]) or "有效"
            for column, value in enumerate((label, fmt(data["rms_mv"]["mean"], " mV"),
                                            fmt(data["rms_mv"]["max"], " mV"), compare_value, phase, status)):
                self.rideMuscleTable.setItem(row, column, QTableWidgetItem(value))
        self.summaryMetricValues["duration"].setText(f"{result['duration_s']:.0f}")
        self.summaryMetricValues["muscles"].setText(str(sum(item["rms_mv"]["count"] > 0 for item in result["emg"].values())))
        self.rideStateLabel.setText("收集中" if self.rideSession is not None else "分析完成")
        FluentThemeManager.set_property(self.rideStateLabel, "fluentSeverity", "info" if self.rideSession is not None else "success")
        self.exportReportButton.setEnabled(True)
        for plot in (self.rmsAverageBarPlot, self.rmsPeakBarPlot, self.emgEnvelopePlot):
            plot.clear()
        bar_positions, average_heights, peak_heights, brushes, ticks = [], [], [], [], []
        for index, slot in enumerate(sorted(result["emg"], key=lambda value: int(value))):
            data = result["emg"][slot]["rms_mv"]
            if data["mean"] is not None:
                color = COLORS[(int(slot) - 1) % len(COLORS)]
                bar_positions.append(index)
                average_heights.append(data["mean"])
                peak_heights.append(data["max"] or 0.0)
                brushes.append(pg.mkBrush(color))
                ticks.append((index, f"S{int(slot):02d}"))
        if bar_positions:
            self.rmsAverageBarPlot.addItem(pg.BarGraphItem(
                x=bar_positions, height=average_heights, width=0.68, brushes=brushes))
            self.rmsPeakBarPlot.addItem(pg.BarGraphItem(
                x=bar_positions, height=peak_heights, width=0.68, brushes=brushes))
            tokens = (FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark"
                      else FluentThemeManager.LIGHT)
            for plot, values in ((self.rmsAverageBarPlot, average_heights),
                                 (self.rmsPeakBarPlot, peak_heights)):
                for position, value in zip(bar_positions, values):
                    label = pg.TextItem(f"{value:.3f}", color=tokens["text_primary"], anchor=(0.5, 1.0))
                    plot.addItem(label)
                    label.setPos(position, value)
            self.rmsAverageBarPlot.getAxis("bottom").setTicks([ticks])
            self.rmsPeakBarPlot.getAxis("bottom").setTicks([ticks])
            self.rmsAverageBarPlot.setXRange(-0.65, len(bar_positions) - 0.35, padding=0)
            self.rmsPeakBarPlot.setXRange(-0.65, len(bar_positions) - 0.35, padding=0)
        previous_slot = self.envelopeMuscleCombo.currentData()
        self.envelopeMuscleCombo.blockSignals(True)
        self.envelopeMuscleCombo.clear()
        for slot in sorted(result["emg"], key=lambda value: int(value)):
            channel = session.get("channels", {}).get(slot, {})
            label = channel.get("label") or channel.get("mapping_name") or ""
            self.envelopeMuscleCombo.addItem(f"S{int(slot):02d} {label}", slot)
        selected_index = self.envelopeMuscleCombo.findData(previous_slot)
        self.envelopeMuscleCombo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self.envelopeMuscleCombo.setEnabled(self.envelopeMuscleCombo.count() > 0)
        self.envelopeMuscleCombo.blockSignals(False)
        selected_slot = self.envelopeMuscleCombo.currentData()
        self.emgEnvelopePlot.setTitle(f"EMG RMS Peak 包絡線（每 1 秒最高值）— {self.envelopeMuscleCombo.currentText()}"
            if selected_slot is not None else "EMG RMS Peak 包絡線（每 1 秒最高值）")
        if selected_slot is not None:
            slot = str(selected_slot)
            rows = [row for row in session["emg"] if str(row.get("slot")) == slot]
            offset = result["emg_trainer_offset_s"]
            envelope = peak_envelope(rows)
            if envelope:
                stride = max(1, len(envelope) // 2000)
                self.emgEnvelopePlot.plot([point[0] + offset for point in envelope[::stride]],[point[1] for point in envelope[::stride]],
                    pen=pg.mkPen(COLORS[(int(slot) - 1) % len(COLORS)], width=2),name=f"S{int(slot):02d}",)

    def importEmgAnalysis(self) -> None:
        if self.emgAnalysisImportWorker is not None:
            return
        filename, _ = QFileDialog.getOpenFileName(self, "匯入 EMG 分析 CSV", self.pathEdit.text().strip(), "EMG CSV (*.csv)")
        if not filename:
            return
        path = Path(filename)
        baselines = {str(slot): asdict(record) for slot, record in self.baselineManager.records.items()}
        self.pendingAutoAngleAnalysis = False
        self.angleEmgPath = None
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.updateAngleFileStatus()
        self.importEmgAnalysisButton.setEnabled(False)
        worker = EMGAnalysisImportWorker(path, list(self.mappings), baselines, asdict(self.collectionSubject), self.filterSettings,)
        self.emgAnalysisImportWorker = worker
        worker.completed.connect(self.emgAnalysisImported)
        worker.failed.connect(self.emgAnalysisImportFailed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.emgAnalysisImportFinished)
        worker.start()

    def emgAnalysisImported(self, session: dict, filename: str) -> None:
        self.latestRideSession = session
        self.angleEmgPath = Path(filename)
        if self.anglePedalPath != pedalOutputPath(self.angleEmgPath):
            self.anglePedalPath = None
            if self.pedalSummarySource == "imported":
                self.summaryRecords = []
                self.refreshSummaryAnalysis()
        self.updateAngleFileStatus()
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.collectedRideSession = None
        self.updateAngleFileStatus()
        self.refreshRideAnalysis()
        self.summaryTabs.setCurrentIndex(0)
        self.queueAngleAnalysis()

    def emgAnalysisImportFailed(self, message: str) -> None:
        QMessageBox.warning(self, "匯入 EMG 資料失敗", message)

    def emgAnalysisImportFinished(self) -> None:
        self.importEmgAnalysisButton.setEnabled(True)
        self.emgAnalysisImportWorker = None

    def _comparisonStore(self) -> SqlServerStorage | None:
        if self.activeDatabaseConfig is None:
            self.comparisonStatusLabel.setText("請先到「設定」頁輸入 SQL Server 帳密並完成測試連線。")
            return None
        return SqlServerStorage(self.activeDatabaseConfig)

    def loadDatabaseComparisonPatients(self) -> None:
        store = self._comparisonStore()
        if store is None:
            return
        try:
            patients = store.list_patients_with_emg()
        except Exception as exc:
            self.comparisonStatusLabel.setText(f"無法讀取資料庫病人：{exc}")
            return
        finally:
            store.close()
        self.databaseComparePatientCombo.blockSignals(True)
        self.databaseComparePatientCombo.clear()
        for patient in patients:
            name = str(patient["name"] or "未填姓名")
            self.databaseComparePatientCombo.addItem(f'{patient["patient_id"]} · {name}（{patient["test_count"]} 次）', patient["patient_id"])
        self.databaseComparePatientCombo.blockSignals(False)
        if not patients:
            self.clearDatabaseComparisonCards()
            self.comparisonStatusLabel.setText("資料庫目前沒有可比較的 EMG RMS 量測紀錄。")
            return
        self.loadDatabaseComparisonSessions()

    @staticmethod
    def _databaseSessionLabel(session: dict) -> str:
        created = session.get("created_at")
        timestamp = created.strftime("%Y-%m-%d %H:%M") if isinstance(created, datetime) else "--"
        duration = session.get("test_time")
        duration_text = f"{int(duration)} 秒" if duration is not None else "時間未記錄"
        resistance = session.get("resistance_level")
        resistance_text = f"阻力 {float(resistance):g}" if resistance is not None else "阻力未記錄"
        return f"{timestamp} ｜ {duration_text} ｜ {resistance_text}"

    def loadDatabaseComparisonSessions(self, *_args) -> None:
        patient_id = self.databaseComparePatientCombo.currentData()
        if not patient_id:
            return
        store = self._comparisonStore()
        if store is None:
            return
        try:
            sessions = store.list_emg_sessions(str(patient_id))
        except Exception as exc:
            self.comparisonStatusLabel.setText(f"無法讀取量測紀錄：{exc}")
            return
        finally:
            store.close()
        for combo in (self.databaseCompareACombo, self.databaseCompareBCombo):
            combo.blockSignals(True)
            combo.clear()
            for session in sessions:
                combo.addItem(self._databaseSessionLabel(session), session)
            combo.blockSignals(False)
        if len(sessions) < 2:
            self.clearDatabaseComparisonCards()
            self.comparisonStatusLabel.setText("此病人需要至少兩次含 RMS 資料的量測，才能比較。")
            return
        self.databaseCompareACombo.setCurrentIndex(0)
        self.databaseCompareBCombo.setCurrentIndex(1)
        self.clearDatabaseComparisonCards()
        self.comparisonStatusLabel.setText("已載入兩次量測選項，請確認 A、B 與前後段秒數，再按「比較 RMS」顯示結果。")

    @staticmethod
    def _comparisonSide(muscle_name: str) -> str:
        name = muscle_name.upper().strip()
        if name.startswith("右") or "-R)" in name or name.endswith("-R"):
            return "R"
        if name.startswith("左") or "-L)" in name or name.endswith("-L"):
            return "L"
        return ""

    @staticmethod
    def _comparisonMean(points: list[dict], start: float, end: float) -> float | None:
        values = [float(point["rms_mv"]) for point in points if point["time_s"] is not None and start <= float(point["time_s"]) <= end and point["rms_mv"] is not None]
        return float(np.mean(values)) if values else None

    def clearDatabaseComparisonCards(self) -> None:
        self.databaseComparisonPlots = []
        for layout in self.databaseComparisonLayouts.values():
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            layout.addStretch()

    def refreshComparison(self) -> None:
        self.refreshDatabaseComparison()

    def refreshDatabaseComparison(self, *_args) -> None:
        first = self.databaseCompareACombo.currentData()
        second = self.databaseCompareBCombo.currentData()
        if not isinstance(first, dict) or not isinstance(second, dict):
            return
        if first["test_id"] == second["test_id"]:
            self.clearDatabaseComparisonCards()
            self.comparisonStatusLabel.setText("請選擇兩次不同的量測紀錄。")
            return
        store = self._comparisonStore()
        if store is None:
            return
        try:
            first_rows = store.load_emg_rms_series(first["test_id"])
            second_rows = store.load_emg_rms_series(second["test_id"])
        except Exception as exc:
            self.comparisonStatusLabel.setText(f"無法讀取 RMS 資料：{exc}")
            return
        finally:
            store.close()
        self.renderDatabaseComparison(first, second, first_rows, second_rows)

    def renderDatabaseComparison(self, first: dict, second: dict, first_rows: list[dict], second_rows: list[dict]) -> None:
        early, late = self.databaseCompareEarlySpin.value(), self.databaseCompareLateSpin.value()
        grouped: list[dict[str, list[dict]]] = []
        for rows in (first_rows, second_rows):
            by_muscle: dict[str, list[dict]] = {}
            for row in rows:
                name = str(row.get("muscle_name") or "").strip() or "未命名肌肉"
                by_muscle.setdefault(name, []).append(row)
            grouped.append(by_muscle)
        names = sorted(set(grouped[0]) | set(grouped[1]), key=lambda value: (self._comparisonSide(value), value))
        durations = [max((float(row["time_s"]) for row in rows if row.get("time_s") is not None), default=-1) for rows in (first_rows, second_rows)]
        invalid_windows = [label for label, duration in zip(("量測 A", "量測 B"), durations, strict=True) if duration + 1 < early or duration + 1 < late]
        self.clearDatabaseComparisonCards()
        if not names:
            self.comparisonStatusLabel.setText("選取的量測沒有可用 RMS 資料。")
            return
        for name in names:
            first_points, second_points = grouped[0].get(name, []), grouped[1].get(name, [])
            side = self._comparisonSide(name)
            if side not in self.databaseComparisonLayouts:
                continue
            first_end = max((float(point["time_s"]) for point in first_points if point.get("time_s") is not None), default=-1)
            second_end = max((float(point["time_s"]) for point in second_points if point.get("time_s") is not None), default=-1)
            first_early = self._comparisonMean(first_points, 0, early - 1) if first_end + 1 >= early else None
            second_early = self._comparisonMean(second_points, 0, early - 1) if second_end + 1 >= early else None
            first_late = self._comparisonMean(first_points, max(0, first_end - late + 1), first_end) if first_end + 1 >= late else None
            second_late = self._comparisonMean(second_points, max(0, second_end - late + 1), second_end) if second_end + 1 >= late else None
            status = []
            if not first_points:
                status.append("量測 A 無資料")
            if not second_points:
                status.append("量測 B 無資料")
            if first_end + 1 < early or second_end + 1 < early:
                status.append("前段時長不足")
            if first_end + 1 < late or second_end + 1 < late:
                status.append("後段時長不足")
            self.addDatabaseComparisonCard(side, name, first, second, first_early, second_early, first_late, second_late, first_points, second_points, "；".join(status) or "可比較")
        note = "；".join(invalid_windows) + " 的指定時段超過量測長度，請縮短秒數。" if invalid_windows else "每條曲線均保留各自完整量測時間，未拉伸或裁切。"
        self.comparisonStatusLabel.setText(f"病人 {self.databaseComparePatientCombo.currentData()} ｜ A：{self._databaseSessionLabel(first)} ｜ B：{self._databaseSessionLabel(second)}。{note}")
        self.comparisonNotes.setText("數值為 AvgRMSmV 的平均值，未除以個人基準值。缺少肌肉或 RMS 時顯示「無資料」，不以 0 代替。資料庫沒有保存取樣率與濾波參數，請確認兩次採用相同設定。")
        self.databaseComparisonTabs.setCurrentIndex(0 if self.databaseComparisonLayouts["R"].count() > 1 else 1)

    def addDatabaseComparisonCard(self, side: str, muscle_name: str, first: dict, second: dict, first_early: float | None, second_early: float | None, first_late: float | None, second_late: float | None, first_points: list[dict], second_points: list[dict], status: str) -> None:
        layout = self.databaseComparisonLayouts[side]
        card = QFrame()
        FluentThemeManager.set_property(card, "comparisonRole", "card")
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 14, 16, 14)
        row.setSpacing(18)

        numbers = QWidget()
        numbers.setMinimumWidth(370)
        numbers.setMaximumWidth(400)
        numbers_layout = QVBoxLayout(numbers)
        numbers_layout.setContentsMargins(0, 0, 0, 0)
        numbers_layout.setSpacing(6)
        numbers_layout.addWidget(QLabel(muscle_name, objectName="sectionTitle"))
        table = QTableWidget(2, 2)
        table.setHorizontalHeaderLabels(("量測 A 平均 RMS (mV)", "量測 B 平均 RMS (mV)"))
        table.setVerticalHeaderLabels((f"前 {self.databaseCompareEarlySpin.value()} 秒", f"後 {self.databaseCompareLateSpin.value()} 秒"))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setMaximumWidth(400)
        table.setMinimumHeight(118)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(True)
        table.verticalHeader().setDefaultSectionSize(30)
        table.verticalHeader().setFixedWidth(66)

        def value_text(value: float | None) -> str:
            return "無資料" if value is None else f"{value:.4f} mV"

        for row_index, values in enumerate(((first_early, second_early), (first_late, second_late))):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value_text(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row_index, column, item)
        numbers_layout.addWidget(table)
        if status:
            numbers_layout.addWidget(QLabel(status, objectName="muted"))
        numbers_layout.addStretch(1)
        row.addWidget(numbers, 2)

        chart = QWidget()
        chart_layout = QVBoxLayout(chart)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        chart_layout.setSpacing(5)
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        series_colors = (tokens["brand_background"], tokens["danger"])

        def legend_item(color: str, text: str) -> QWidget:
            item = QWidget()
            item_layout = QHBoxLayout(item)
            item_layout.setContentsMargins(0, 0, 0, 0)
            item_layout.setSpacing(5)
            swatch = QFrame()
            swatch.setFixedSize(11, 11)
            swatch_palette = swatch.palette()
            swatch_palette.setColor(QPalette.ColorRole.Window, QColor(color))
            swatch.setPalette(swatch_palette)
            swatch.setAutoFillBackground(True)
            item_layout.addWidget(swatch)
            item_layout.addWidget(QLabel(text, objectName="muted"))
            return item

        chart_header = QHBoxLayout()
        chart_header.setContentsMargins(2, 0, 2, 0)
        chart_header.setSpacing(8)
        chart_header.addWidget(QLabel(f"{muscle_name}｜完整 RMS 曲線", objectName="sectionTitle"))
        chart_header.addStretch(1)
        chart_header.addWidget(legend_item(series_colors[0], "量測 A"))
        chart_header.addSpacing(12)
        chart_header.addWidget(legend_item(series_colors[1], "量測 B"))
        chart_layout.addLayout(chart_header)
        plot = pg.PlotWidget()
        plot.setMinimumHeight(270)
        plot.setBackground("#ffffff")
        plot.setLabel("bottom", "量測開始後時間", units="s")
        plot.setLabel("left", "RMS", units="mV")
        plot.showGrid(x=True, y=True, alpha=0.16)
        plot.getViewBox().setDefaultPadding(0.0)
        for axis_name in ("left", "bottom"):
            axis = plot.getAxis(axis_name)
            axis.setPen("#667085")
            axis.setTextPen("#1f2937")
            axis.setStyle(tickTextOffset=8)
        max_x = 1.0
        plotted_values: list[float] = []
        series_specs = (
            (first_points, series_colors[0]),
            (second_points, series_colors[1]),
        )
        for points, color in series_specs:
            series = [(float(point["time_s"]), float(point["rms_mv"])) for point in points if point.get("time_s") is not None and point.get("rms_mv") is not None]
            if series:
                stride = max(1, len(series) // 1500)
                x, y = zip(*series[::stride])
                max_x = max(max_x, max(x))
                plotted_values.extend(y)
                plot.plot(x, y, pen=pg.mkPen(color, width=2.4))

        def fit_plot() -> None:
            plot.setXRange(0, max_x, padding=0.0)
            if not plotted_values:
                return
            low, high = min(plotted_values), max(plotted_values)
            padding = (high - low) * 0.08
            if padding == 0:
                padding = max(abs(high) * 0.08, 0.001)
            plot.setYRange(low - padding, high + padding, padding=0.0)

        self.attachPlotFitButton(plot.getPlotItem(), "完整 RMS 曲線", fit_plot)
        fit_plot()
        chart_layout.addWidget(plot, 1)
        row.addWidget(chart, 5)
        self.databaseComparisonPlots.append(plot)
        layout.insertWidget(max(0, layout.count() - 1), card)

    def openAngleAnalysisSettings(self) -> None:
        """Edit the settings used by the next automatic angle analysis."""
        dialog = QDialog(self)
        dialog.setWindowTitle("踩踏角度分析設定")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(QLabel(
            "設定會套用到下一次自動分析；已有 EMG 與踏板資料時，儲存後會自動重新分析。",
            objectName="muted",
        ))

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        automatic_range = QCheckBox("自動選取前後段比較時間")
        has_legacy_ranges = (self.settings.contains("angle/start0")
                             and not self.settings.contains("angle/automatic_range"))
        automatic_range.setChecked(
            str(self.settings.value("angle/automatic_range", not has_legacy_ranges)).lower() == "true"
        )
        automatic_range.setToolTip("依 EMG 與踏板共同資料長度選取開頭與結尾的分析時段。")
        form.addWidget(automatic_range, 0, 0, 1, 4)

        range_spins: list[QDoubleSpinBox] = []
        for row, (label, start_key, end_key, defaults) in enumerate((
            ("前段比較時間", "angle/start0", "angle/start1", (5.0, 65.0)),
            ("後段比較時間", "angle/end0", "angle/end1", (240.0, 300.0)),
        ), start=1):
            first = QDoubleSpinBox()
            second = QDoubleSpinBox()
            for spin, key, default in ((first, start_key, defaults[0]), (second, end_key, defaults[1])):
                spin.setRange(0.0, 100000.0)
                spin.setDecimals(2)
                spin.setSuffix(" 秒")
                spin.setValue(float(self.settings.value(key, default)))
                range_spins.append(spin)
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(first, row, 1)
            form.addWidget(QLabel("至"), row, 2)
            form.addWidget(second, row, 3)

        auto_scale = QCheckBox("自動校正 EMG 時鐘比例")
        auto_scale.setChecked(str(self.settings.value("angle/auto_scale", "true")).lower() == "true")
        scale = QDoubleSpinBox()
        scale.setRange(0.8, 1.2)
        scale.setDecimals(5)
        scale.setSingleStep(0.001)
        scale.setValue(float(self.settings.value("angle/scale", 1.0)))
        form.addWidget(auto_scale, 3, 0, 1, 2)
        form.addWidget(QLabel("手動比例"), 3, 2)
        form.addWidget(scale, 3, 3)

        auto_offset = QCheckBox("依典型角度估計起點偏移")
        auto_offset.setToolTip("僅適用已校正上死點的角度 CSV，且需要足夠一致的肌肉訊號。")
        auto_offset.setChecked(str(self.settings.value("angle/auto_offset", "false")).lower() == "true")
        offset = QDoubleSpinBox()
        offset.setRange(-30.0, 30.0)
        offset.setDecimals(3)
        offset.setSuffix(" 秒")
        offset.setValue(float(self.settings.value("angle/offset_s", 0.0)))
        form.addWidget(QLabel("起點偏移"), 4, 0)
        form.addWidget(offset, 4, 1)
        form.addWidget(auto_offset, 4, 2, 1, 2)
        layout.addLayout(form)

        def update_enabled() -> None:
            for spin in range_spins:
                spin.setEnabled(not automatic_range.isChecked())
            scale.setEnabled(not auto_scale.isChecked())
            offset.setEnabled(not auto_offset.isChecked())

        automatic_range.toggled.connect(update_enabled)
        auto_scale.toggled.connect(update_enabled)
        auto_offset.toggled.connect(update_enabled)
        update_enabled()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("儲存並套用")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        values = [spin.value() for spin in range_spins]
        try:
            if not automatic_range.isChecked():
                AngleSettings((values[0], values[1]), (values[2], values[3]),
                              scale=None if auto_scale.isChecked() else scale.value(),
                              offset_s=None if auto_offset.isChecked() else offset.value()).validate()
        except ValueError as exc:
            QMessageBox.warning(self, "角度分析設定無效", str(exc))
            return
        for key, value in zip(("start0", "start1", "end0", "end1"), values):
            self.settings.setValue(f"angle/{key}", value)
        self.settings.setValue("angle/automatic_range", automatic_range.isChecked())
        self.settings.setValue("angle/auto_scale", auto_scale.isChecked())
        self.settings.setValue("angle/scale", scale.value())
        self.settings.setValue("angle/auto_offset", auto_offset.isChecked())
        self.settings.setValue("angle/offset_s", offset.value())
        self.queueAngleAnalysis()

    def updateAngleFileStatus(self) -> None:
        emg = getattr(self, "angleEmgPath", None)
        pedal = getattr(self, "anglePedalPath", None)
        emg_name = emg.name if emg else "未匯入"
        pedal_name = pedal.name if pedal else "未匯入"
        self.summarySourceLabel.setText(f"分析資料：EMG {emg_name} ｜ 踏板 {pedal_name}")
        if emg and pedal:
            self.angleStatusLabel.setText("EMG 與踏板資料已備妥，正在準備自動分析。")
        elif emg:
            self.angleStatusLabel.setText("已匯入 EMG CSV；請在右上角匯入同次騎乘的踏板 CSV。")
        elif pedal:
            self.angleStatusLabel.setText("已選擇踏板 CSV；請使用右上角「匯入原始 EMG」。")
        else:
            self.angleStatusLabel.setText("請先用右上角匯入原始 EMG 與同次騎乘的踏板 CSV。")

    def runAngleAnalysis(self) -> None:
        if self.angleAnalysisWorker is not None:
            return
        emg = self.angleEmgPath
        pedal = self.anglePedalPath
        if emg is None or pedal is None:
            self.angleStatusLabel.setText("請先用右上角匯入原始 EMG 與踏板 CSV。")
            return
        try:
            with emg.open("r", encoding="utf-8-sig", newline="") as stream:
                headers = next(csv.reader(stream))
            sides, side_source = recordingChannelSides(emg, headers, self.mappings)
            if not sides:
                raise ValueError("沒有通道符合設定頁的肌肉側別對應；請檢查感測器名稱與 -L/-R 縮寫。")
            with pedal.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                time_column = "session_seconds" if "session_seconds" in (reader.fieldnames or ()) else "t_session_s"
                if time_column not in (reader.fieldnames or ()):
                    raise ValueError("踏板 CSV 沒有可用的騎乘時間欄位。")
                pedal_duration = max((float(row[time_column]) for row in reader), default=0.0)
            emg_rows = (self.collectedRideSession or self.latestRideSession or {}).get("emg", [])
            emg_duration = max((float(row["time_s"]) for row in emg_rows), default=pedal_duration)
            duration = min(pedal_duration, emg_duration)
            if duration < 35:
                raise ValueError("EMG／踏板共同資料不足 35 秒；請收集更多完整踩踏圈。")
            automatic_range = str(self.settings.value("angle/automatic_range", "true")).lower() == "true"
            if automatic_range:
                window = min(60.0, max(10.0, (duration - 5.0) / 3.0))
                start = (5.0, round(5.0 + window, 2))
                end = (round(duration - window, 2), round(duration, 2))
            else:
                start = (float(self.settings.value("angle/start0", 5.0)),
                         float(self.settings.value("angle/start1", 65.0)))
                end = (float(self.settings.value("angle/end0", 240.0)),
                       float(self.settings.value("angle/end1", 300.0)))
            auto_scale = str(self.settings.value("angle/auto_scale", "true")).lower() == "true"
            auto_offset = str(self.settings.value("angle/auto_offset", "false")).lower() == "true"
            settings = AngleSettings(
                start,
                end,
                scale=None if auto_scale else float(self.settings.value("angle/scale", 1.0)),
                offset_s=None if auto_offset else float(self.settings.value("angle/offset_s", 0.0)),
            )
            settings.validate()
        except (OSError, StopIteration, ValueError) as exc:
            self.angleStatusLabel.setText(f"無法分析：{exc}")
            self.angleOverviewLabel.setText(f"本次相位聯合分析未完成：{exc}")
            return
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.angleStatusLabel.setText("正在背景分析原始 EMG 與踏板資料…")
        worker = AngleAnalysisWorker(emg, pedal, sides, settings, side_source)
        self.angleAnalysisWorker = worker
        worker.completed.connect(self.angleAnalysisCompleted)
        worker.failed.connect(self.angleAnalysisFailed)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.angleAnalysisFinished)
        worker.start()

    def angleAnalysisCompleted(self, result: dict) -> None:
        worker = self.angleAnalysisWorker
        if worker is None or worker.emg_path != self.angleEmgPath or worker.pedal_path != self.anglePedalPath:
            return
        self.angleAnalysisResult = result
        sync = result["sync"]
        reference_quality = (f'｜{len(sync["references"])} 個參考通道，最佳殘差 '
                             f'{sync["references"][0]["residual_std_s"] * 1000:.0f} ms'
                             if sync["references"] else "｜無可用時鐘參考通道")
        self.angleStatusLabel.setText(
            f'{sync["state"]}｜時鐘比例 {sync["scale"]:.5f}｜起點偏移 {sync["offset_s"]:+.3f} 秒｜有效圈數 {sync["valid_cycles"]}｜'
            f'{sync["angle_label"]}{reference_quality}｜側別依據：{sync.get("side_source", "輸入設定")}。'
            f'每個通道以自身第 {result["settings"]["percentile"]:g} 百分位為參考；'
            "跨肌肉顏色不可解讀為出力排名。")
        self.angleOverviewLabel.setText(result.get("overview") or analysis_summary_text(result))
        self.angleCurvePlot.setLabel("bottom", sync["angle_label"], units="°")
        self.angleHeatPlot.setLabel("bottom", sync["angle_label"], units="°")
        summary, profiles = result["summary"], result["profiles"]
        self.angleMuscleCombo.blockSignals(True)
        self.angleMuscleCombo.clear()
        for entry in summary.itertuples():
            self.angleMuscleCombo.addItem(
                f"{entry.location_zh} ({entry.muscle}) · {entry.channel_label}", entry.channel
            )
        self.angleMuscleCombo.blockSignals(False)
        self.refreshAngleCurve()
        self.angleComparisonTable.setRowCount(len(summary))
        for row, entry in enumerate(summary.itertuples()):
            cells = (f"{entry.location_zh} · {entry.channel_label}", str(entry.n_start), str(entry.n_end),
                     f"{entry.rms_uv_start:.1f}", f"{entry.rms_uv_end:.1f}",
                     f"{entry.rms_change_percent:+.1f}", f"{entry.peak_angle_start:.0f}",
                     f"{entry.peak_angle_end:.0f}", f"{entry.peak_angle_shift:+.0f}",
                     f"{entry.mdf_change_hz:+.1f}")
            for column, value in enumerate(cells):
                self.angleComparisonTable.setItem(row, column, QTableWidgetItem(value))
        names = list(summary.channel)
        matrix = np.column_stack([profiles[(profiles.channel == name) & (profiles.phase == "all")].mean_percent.to_numpy(float)
                                  for name in names])
        self.angleHeatPlot.clear()
        image = pg.ImageItem(matrix)
        image.setRect(QRectF(0, -.5, 360, len(names)))
        image.setColorMap(pg.colormap.get("CET-L4"))
        image.setLevels((0, 100))
        self.angleHeatPlot.addItem(image)
        self.angleHeatPlot.getAxis("left").setTicks([[(i, f'{summary.iloc[i].location_zh} · {sync["channel_names"][name]}')
                                                      for i, name in enumerate(names)]])
        self.angleHeatPlot.setXRange(0, 360, padding=0)
        self.angleHeatPhaseLine = pg.InfiniteLine(pos=self.anglePhaseSpin.value(), angle=90,pen=pg.mkPen("#555555", width=2, style=Qt.DashLine),)
        self.angleHeatPlot.addItem(self.angleHeatPhaseLine)
        self.refreshAnglePhase()
        self.refreshAngleReport()
        self.angleExportButton.setEnabled(True)

    def refreshAngleReport(self) -> None:
        """Present the reproducible, PPT-style numerical findings in Qt."""
        if self.angleAnalysisResult is None:
            self.clearAngleReportTables()
            return
        result = self.angleAnalysisResult
        summary = result["summary"].copy()
        sync, settings = result["sync"], result["settings"]
        if summary.empty:
            self.clearAngleReportTables()
            return

        ordered = summary.sort_values(["side", "location_zh", "channel_label"])
        top_rms = summary.loc[summary.rms_change_percent.idxmax()]
        lowest_mdf = summary.loc[summary.mdf_change_hz.idxmin()]
        rms_up = int((summary.rms_change_percent > 0).sum())
        mdf_down = int((summary.mdf_change_hz < 0).sum())
        phase_name = sync["angle_label"]
        calibration = "已校正角度" if sync["angle_calibrated"] else "相對踩踏相位"
        lines = [
            "踩踏角度 EMG 完整分析報告",
            "=" * 38,
            "",
            "【測試與同步】",
            f"角度基準：{phase_name}（{calibration}）",
            f"有效踩踏圈：{sync['valid_cycles']} 圈",
            f"前段：{settings['start'][0]:g}–{settings['start'][1]:g} 秒；後段：{settings['end'][0]:g}–{settings['end'][1]:g} 秒",
            f"EMG 時鐘比例：{sync['scale']:.5f}；起點偏移：{sync['offset_s']:+.3f} 秒",
            f"側別來源：{sync.get('side_source', '目前設定')}",
            "",
            "【通道與貼片對應】",
        ]
        for entry in ordered.itertuples():
            lines.append(f"{entry.location_zh}：{entry.channel_label}")

        lines.extend([
            "",
            "【重點結果】",
            f"RMS 上升通道：{rms_up}/{len(summary)}；MDF 下降通道：{mdf_down}/{len(summary)}。",
            f"最大 RMS 變化：{top_rms.location_zh} ({top_rms.channel_label}) "
            f"{top_rms.rms_change_percent:+.1f}% ({top_rms.rms_uv_start:.1f} → {top_rms.rms_uv_end:.1f} µV)。",
            f"最大 MDF 下降：{lowest_mdf.location_zh} ({lowest_mdf.channel_label}) "
            f"{lowest_mdf.mdf_change_hz:+.1f} Hz ({lowest_mdf.mdf_start:.1f} → {lowest_mdf.mdf_end:.1f} Hz)。",
            "",
            "【通道前後段數值】",
            "請參照下方表格。",
        ])

        weak = ordered[ordered.norm_uv < 10]
        lines.extend(["", "【判讀限制】"])
        if not sync["angle_calibrated"]:
            lines.append("目前為相對踩踏相位；若要解釋 TDC 絕對角度，需加入可重現的影片或曲柄同步校正。")
        lines.append("每條肌肉以自身 99 百分位正規化；可比較同一肌肉的時間變化，不可跨肌肉排名出力。")
        lines.append("EMG 振幅不等於肌力、力矩或踩踏效率；必須與踏板功率、動作影片一起解釋。")
        if not weak.empty:
            labels = "、".join(f"{row.location_zh} ({row.channel_label})" for row in weak.itertuples())
            lines.append(f"訊號偏弱、需謹慎解讀：{labels}。")
        self.setAngleReportTableRows(self.angleReportSyncTable, (
            ("角度基準", f"{phase_name}（{calibration}）"),
            ("有效踩踏圈", f"{sync['valid_cycles']} 圈"),
            ("分析區間", f"前段 {settings['start'][0]:g}–{settings['start'][1]:g} 秒；後段 {settings['end'][0]:g}–{settings['end'][1]:g} 秒"),
            ("EMG 時鐘同步", f"比例 {sync['scale']:.5f}；起點偏移 {sync['offset_s']:+.3f} 秒"),
            ("側別來源", sync.get("side_source", "目前設定")),
        ))
        self.setAngleReportTableRows(
            self.angleReportMappingTable,
            tuple((entry.location_zh, entry.channel_label) for entry in ordered.itertuples()),
        )
        self.setAngleReportTableRows(self.angleReportHighlightsTable, (
            ("RMS 上升通道", f"{rms_up}/{len(summary)}"),
            ("MDF 下降通道", f"{mdf_down}/{len(summary)}"),
            ("最大 RMS 變化",
             f"{top_rms.location_zh} ({top_rms.channel_label}) {top_rms.rms_change_percent:+.1f}% "
             f"({top_rms.rms_uv_start:.1f} → {top_rms.rms_uv_end:.1f} µV)"),
            ("最大 MDF 下降",
             f"{lowest_mdf.location_zh} ({lowest_mdf.channel_label}) {lowest_mdf.mdf_change_hz:+.1f} Hz "
             f"({lowest_mdf.mdf_start:.1f} → {lowest_mdf.mdf_end:.1f} Hz)"),
        ))
        limitations = []
        if not sync["angle_calibrated"]:
            limitations.append(("目前為相對踩踏相位；若要解釋 TDC 絕對角度，需加入可重現的影片或曲柄同步校正。",))
        limitations.append(("每條肌肉以自身 99 百分位正規化；可比較同一肌肉的時間變化，不可跨肌肉排名出力。",))
        limitations.append(("EMG 振幅不等於肌力、力矩或踩踏效率；必須與踩板功率、動作影片一起解釋。",))
        if not weak.empty:
            labels = "、".join(f"{row.location_zh} ({row.channel_label})" for row in weak.itertuples())
            limitations.append((f"訊號偏弱、需謹慎解讀：{labels}。",))
        self.setAngleReportTableRows(self.angleReportLimitationsTable, limitations)
        self.refreshAngleReportChannelTable(ordered)
        self.refreshAngleReportCharts(ordered)
        self.refreshAngleReportProfiles()
        self.refreshAngleReportPolarProfiles()

    def clearAngleReportTables(self) -> None:
        for table in (self.angleReportSyncTable, self.angleReportMappingTable,
                      self.angleReportHighlightsTable, self.angleReportChannelTable,
                      self.angleReportLimitationsTable):
            table.clearContents()
            table.setRowCount(0)
        if hasattr(self, "angleReportPolarGrid"):
            self.clearAngleReportPolarProfiles()

    @staticmethod
    def setAngleReportTableRows(table: QTableWidget, rows) -> None:
        table.clearContents()
        table.setRowCount(len(rows))
        for row_index, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.resizeRowsToContents()
        content_height = table.horizontalHeader().height() + sum(
            table.rowHeight(row) for row in range(table.rowCount())
        ) + 6
        table.setMinimumHeight(max(110, content_height))

    def refreshAngleReportChannelTable(self, ordered) -> None:
        """Present each channel's early/late measurements in readable columns."""
        table = self.angleReportChannelTable
        table.clearContents()
        table.setRowCount(len(ordered))
        for row_index, entry in enumerate(ordered.itertuples()):
            rms_up = entry.rms_change_percent > 0
            mdf_down = entry.mdf_change_hz < 0
            if rms_up and mdf_down:
                interpretation = "RMS↑、MDF↓：疲勞相關趨勢"
            elif rms_up:
                interpretation = "RMS↑；MDF未下降"
            elif mdf_down:
                interpretation = "MDF↓；RMS未上升"
            else:
                interpretation = "前後變化不一致"
            values = (
                f"{entry.location_zh} / {entry.channel_label}",
                f"{entry.rms_uv_start:.1f} → {entry.rms_uv_end:.1f}",
                f"{entry.rms_change_percent:+.1f}%",
                f"{entry.mdf_start:.1f} → {entry.mdf_end:.1f}",
                f"{entry.peak_angle_shift:+.0f}°",
                interpretation,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if column else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.resizeRowsToContents()
        content_height = table.horizontalHeader().height() + sum(
            table.rowHeight(row) for row in range(table.rowCount())
        ) + 6
        table.setMinimumHeight(max(110, content_height))

    def refreshAngleReportCharts(self, ordered) -> None:
        """Draw PPT-style grouped columns: one muscle, left/right side by side."""
        muscle_order = ("RF", "VM", "VL", "BF", "GAS", "SOL", "TA")
        groups = [muscle for muscle in muscle_order if muscle in set(ordered.muscle)]
        groups.extend(muscle for muscle in ordered.muscle.dropna().unique() if muscle not in groups)
        if not groups:
            return
        labels, left_rows, right_rows = [], [], []
        for muscle in groups:
            pair = ordered[ordered.muscle == muscle]
            exemplar = pair.iloc[0]
            muscle_name = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            # The x axis groups left/right bars by anatomy; retain the
            # conventional EMG abbreviation so GAS/SOL/etc. are explicit.
            labels.append(f"{muscle_name}\n({muscle})")
            left_rows.append(pair[pair.side == "L"].iloc[0] if not pair[pair.side == "L"].empty else None)
            right_rows.append(pair[pair.side == "R"].iloc[0] if not pair[pair.side == "R"].empty else None)
        positions = np.arange(len(groups), dtype=float)
        chart_specs = (
            (self.angleReportRmsPlot, "rms_change_percent", "RMS 變化（%）", "%"),
            (self.angleReportMdfPlot, "mdf_change_hz", "MDF 變化（Hz）", " Hz"),
        )
        for plot, column, axis_label, unit in chart_specs:
            plot.clear()
            left_values = np.array([float(getattr(entry, column)) if entry is not None else np.nan for entry in left_rows])
            right_values = np.array([float(getattr(entry, column)) if entry is not None else np.nan for entry in right_rows])
            plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen("#6b7280", width=1)))
            plot.addItem(pg.BarGraphItem(x=positions - .19, height=left_values, width=.34,
                                         brush=pg.mkBrush("#1684d7"), pen=pg.mkPen("#1684d7")))
            plot.addItem(pg.BarGraphItem(x=positions + .19, height=right_values, width=.34,
                                         brush=pg.mkBrush("#d97706"), pen=pg.mkPen("#d97706")))
            # Write the signed change directly above/below every bar.  The
            # colours retain the left/right convention used by the chart.
            for x_values, values, color in ((positions - .19, left_values, "#0969ad"),
                                            (positions + .19, right_values, "#a45100")):
                for x, value in zip(x_values, values, strict=True):
                    if not np.isfinite(value):
                        continue
                    label = pg.TextItem(f"{value:+.1f}{unit}", color=color, anchor=(.5, 1.15 if value >= 0 else -.15))
                    label.setFont(QFont("Segoe UI", 8))
                    label.setPos(float(x), float(value))
                    plot.addItem(label)
            plot.setLabel("left", axis_label)
            plot.getAxis("bottom").setTicks([list(zip(positions, labels, strict=True))])
            plot.getAxis("bottom").setHeight(42)
            all_values = np.r_[left_values, right_values]
            lower, upper = float(np.nanmin(all_values)), float(np.nanmax(all_values))
            # Extra space keeps the signed labels inside the plot area.
            padding = max(3.0, (upper - lower) * .22)
            plot.setYRange(min(0, lower - padding), max(0, upper + padding), padding=0)
            plot.setXRange(-.7, len(groups) - .3, padding=0)
            plot.showGrid(x=False, y=True, alpha=.18)

    def refreshAngleReportFatigueTrends(self) -> None:
        """Match PPT slides 26/27 with separate left/right cycle trends."""
        plots = self.angleReportFatigueTrendPlots
        for plot in plots:
            plot.clear()
            if plot.getPlotItem().legend is None:
                plot.addLegend(offset=(8, 8), colCount=2)
            else:
                plot.getPlotItem().legend.clear()
        if self.angleAnalysisResult is None:
            return
        cycles = self.angleAnalysisResult["cycles"]
        if cycles.empty:
            return
        selected_muscle = self.angleReportFatigueMuscleCombo.currentData()
        metric_plots = (("rms_uv", plots[0]), ("mdf_hz", plots[1]))
        for metric, plot in metric_plots:
            metric_zh = "RMS" if metric == "rms_uv" else "MDF"
            for side, side_zh, color in (("L", "左腳", "#1684d7"), ("R", "右腳", "#d97706")):
                subset = cycles[cycles.side == side]
                if selected_muscle:
                    subset = subset[subset.muscle == selected_muscle]
                if subset.empty:
                    continue
                row = subset.sort_values("time_s")
                values = row[metric].to_numpy(float)
                # Same smoothing window as the PPT trends: 9 pedal cycles.
                smoothed = row[metric].rolling(9, min_periods=3, center=True).mean().to_numpy(float)
                x = np.arange(1, len(values) + 1, dtype=float)
                plot.plot(x, smoothed, pen=pg.mkPen(color, width=2), name=side_zh)
            muscle_label = selected_muscle or "全部肌肉"
            plot.setTitle(f"{muscle_label}：逐圈 {metric_zh} 趨勢（藍：左腳；橘：右腳；9 圈移動平均）")

    def refreshAngleReportProfiles(self) -> None:
        """Create PPT-like early/late angle profiles, paired by muscle and side."""
        if self.angleAnalysisResult is None:
            return
        result = self.angleAnalysisResult
        summary, profiles = result["summary"], result["profiles"]
        self.angleReportProfiles.clear()
        muscle_order = ("RF", "VM", "VL", "BF", "GAS", "SOL", "TA")
        available = [muscle for muscle in muscle_order if muscle in set(summary.muscle)]
        available.extend(muscle for muscle in summary.muscle.dropna().unique() if muscle not in available)
        selected_profile = self.angleReportProfileMuscleCombo.currentData()
        self.angleReportProfileMuscleCombo.blockSignals(True)
        self.angleReportProfileMuscleCombo.clear()
        for muscle in available:
            exemplar = summary[summary.muscle == muscle].iloc[0]
            label = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            self.angleReportProfileMuscleCombo.addItem(f"{label} ({muscle})", muscle)
        if selected_profile is not None:
            index = self.angleReportProfileMuscleCombo.findData(selected_profile)
            if index >= 0:
                self.angleReportProfileMuscleCombo.setCurrentIndex(index)
        self.angleReportProfileMuscleCombo.blockSignals(False)
        selected_profile = self.angleReportProfileMuscleCombo.currentData()
        shown_muscles = [selected_profile] if selected_profile else available[:1]
        for row, muscle in enumerate(shown_muscles):
            for col, side in enumerate(("L", "R")):
                matches = summary[(summary.muscle == muscle) & (summary.side == side)]
                if matches.empty:
                    continue
                entry = matches.iloc[0]
                plot = self.angleReportProfiles.addPlot(row=row, col=col)
                plot.setTitle(
                    f"{entry.location_zh} {entry.channel_label}  |  RMS {entry.rms_change_percent:+.1f}%"
                )
                for phase, color, label in (("start", "#1684d7", "前段"),
                                            ("end", "#d13438", "後段")):
                    curve = profiles[(profiles.channel == entry.channel) & (profiles.phase == phase)]
                    plot.plot(curve.angle_bin_center.to_numpy(float), curve.mean_percent.to_numpy(float),
                              pen=pg.mkPen(color, width=2), name=label if row == 0 and col == 0 else None)
                plot.setXRange(0, 360, padding=0)
                plot.setYRange(0, 105, padding=0)
                plot.setLabel("bottom", "踩踏角度", units="°")
                if col == 0:
                    plot.setLabel("left", "相對活化", units="%")
                plot.showGrid(x=True, y=True, alpha=.16)
                self.lockAnalysisPlots(plot)
        self.angleReportProfiles.setMinimumHeight(420)
        self.applySummaryPlotTheme()
        selected = self.angleReportCycleMuscleCombo.currentData()
        selected_table = self.angleReportPhaseMuscleCombo.currentData()
        selected_fatigue = self.angleReportFatigueMuscleCombo.currentData()
        self.angleReportCycleMuscleCombo.blockSignals(True)
        self.angleReportPhaseMuscleCombo.blockSignals(True)
        self.angleReportFatigueMuscleCombo.blockSignals(True)
        self.angleReportCycleMuscleCombo.clear()
        self.angleReportPhaseMuscleCombo.clear()
        self.angleReportFatigueMuscleCombo.clear()
        for muscle in available:
            exemplar = summary[summary.muscle == muscle].iloc[0]
            label = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            self.angleReportCycleMuscleCombo.addItem(f"{label} ({muscle})", muscle)
            self.angleReportPhaseMuscleCombo.addItem(f"{label} ({muscle})", muscle)
            self.angleReportFatigueMuscleCombo.addItem(f"{label} ({muscle})", muscle)
        if selected is not None:
            index = self.angleReportCycleMuscleCombo.findData(selected)
            if index >= 0:
                self.angleReportCycleMuscleCombo.setCurrentIndex(index)
        if selected_table is not None:
            index = self.angleReportPhaseMuscleCombo.findData(selected_table)
            if index >= 0:
                self.angleReportPhaseMuscleCombo.setCurrentIndex(index)
        if selected_fatigue is not None:
            index = self.angleReportFatigueMuscleCombo.findData(selected_fatigue)
            if index >= 0:
                self.angleReportFatigueMuscleCombo.setCurrentIndex(index)
        self.angleReportCycleMuscleCombo.blockSignals(False)
        self.angleReportPhaseMuscleCombo.blockSignals(False)
        self.angleReportFatigueMuscleCombo.blockSignals(False)
        self.refreshAngleReportCycleTrends()
        self.refreshAngleReportPhaseTable()
        self.refreshAngleReportFatigueTrends()

    def clearAngleReportPolarProfiles(self) -> None:
        while self.angleReportPolarGrid.count():
            item = self.angleReportPolarGrid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def refreshAngleReportPolarProfiles(self) -> None:
        """Show the export's polar angle view directly in the Qt report."""
        self.clearAngleReportPolarProfiles()
        if self.angleAnalysisResult is None:
            return
        summary = self.angleAnalysisResult["summary"]
        profiles = self.angleAnalysisResult["profiles"]
        if summary.empty:
            return
        muscle_order = ("RF", "VM", "VL", "BF", "GAS", "SOL", "TA")
        available = [muscle for muscle in muscle_order if muscle in set(summary.muscle)]
        available.extend(muscle for muscle in summary.muscle.dropna().unique() if muscle not in available)

        selected = self.angleReportPolarMuscleCombo.currentData()
        self.angleReportPolarMuscleCombo.blockSignals(True)
        self.angleReportPolarMuscleCombo.clear()
        self.angleReportPolarMuscleCombo.addItem("所有肌肉", None)
        for muscle in available:
            exemplar = summary[summary.muscle == muscle].iloc[0]
            label = exemplar.location_zh[1:] if len(exemplar.location_zh) > 1 else exemplar.location_zh
            self.angleReportPolarMuscleCombo.addItem(f"{label} ({muscle})", muscle)
        if selected is not None:
            index = self.angleReportPolarMuscleCombo.findData(selected)
            if index >= 0:
                self.angleReportPolarMuscleCombo.setCurrentIndex(index)
        self.angleReportPolarMuscleCombo.blockSignals(False)

        selected = self.angleReportPolarMuscleCombo.currentData()
        entries = summary[summary.muscle == selected] if selected else summary
        entries = entries.sort_values(["side", "location_zh", "channel_label"])
        for index, entry in enumerate(entries.itertuples()):
            start = profiles[(profiles.channel == entry.channel) & (profiles.phase == "start")]
            end = profiles[(profiles.channel == entry.channel) & (profiles.phase == "end")]
            if start.empty or end.empty:
                continue
            chart = PolarAngleProfileWidget(
                f"{entry.location_zh} · {entry.channel_label}",
                start.angle_bin_center.to_numpy(float), start.mean_percent.to_numpy(float),
                end.angle_bin_center.to_numpy(float), end.mean_percent.to_numpy(float),
            )
            self.angleReportPolarGrid.addWidget(chart, index // 3, index % 3)

    def refreshAngleReportCycleTrends(self) -> None:
        """Match PPT slides 6/8/10/12/14/16/18: eight phase trends per side."""
        for plot in (self.angleReportCycleLeftPlot, self.angleReportCycleRightPlot):
            plot.clear()
            if plot.getPlotItem().legend is None:
                plot.addLegend(offset=(8, 8), colCount=4)
            else:
                plot.getPlotItem().legend.clear()
        if self.angleAnalysisResult is None:
            return
        muscle = self.angleReportCycleMuscleCombo.currentData()
        if not muscle:
            return
        result = self.angleAnalysisResult
        cycles, summary = result["cycles"], result["summary"]
        settings = result["settings"]
        bins = int(settings["bins"])
        bin_centers = (np.arange(bins) + .5) * 360 / bins
        bin_columns = [f"bin_{index}" for index in range(bins)]
        target_angles = tuple(range(0, 360, 45))
        selected_angle = self.angleReportCycleAngleCombo.currentData()
        displayed_angles = target_angles if selected_angle is None else (int(selected_angle),)
        line_colors = ("#1684d7", "#e95f54", "#7e57c2", "#00897b",
                       "#f39c12", "#5c6bc0", "#43a047", "#c2185b")
        for side, plot, side_zh in (("L", self.angleReportCycleLeftPlot, "左腳"),
                                    ("R", self.angleReportCycleRightPlot, "右腳")):
            matches = summary[(summary.muscle == muscle) & (summary.side == side)]
            if matches.empty:
                plot.setTitle(f"{side_zh}：無此肌肉資料")
                continue
            entry = matches.iloc[0]
            rows = cycles[cycles.channel == entry.channel].sort_values("cycle").reset_index(drop=True)
            angle_title = "逐圈 8 角度活化" if selected_angle is None else f"{int(selected_angle)}°逐圈活化"
            plot.setTitle(f"{entry.location_zh} {entry.channel_label} — {angle_title}（5 圈移動平均）")
            x = np.arange(1, len(rows) + 1, dtype=float)
            start_count = int((rows.phase == "start").sum())
            end_indices = np.flatnonzero(rows.phase.to_numpy() == "end")
            if start_count:
                start_region = pg.LinearRegionItem((1, start_count), orientation="vertical", movable=False,
                                                   brush=pg.mkBrush(22, 132, 215, 24), pen=None)
                start_region.setZValue(-10)
                plot.addItem(start_region)
            if len(end_indices):
                end_region = pg.LinearRegionItem((int(end_indices[0]) + 1, len(rows)), orientation="vertical", movable=False,
                                                 brush=pg.mkBrush(209, 52, 56, 24), pen=None)
                end_region.setZValue(-10)
                plot.addItem(end_region)
            for angle, color in zip(target_angles, line_colors, strict=True):
                if angle not in displayed_angles:
                    continue
                distance = np.abs((bin_centers - angle + 180) % 360 - 180)
                columns = [column for column, keep in zip(bin_columns, distance <= 15 + 1e-9, strict=True) if keep]
                values = rows[columns].mean(axis=1).to_numpy(float)
                weights = np.convolve(np.ones(len(values)), np.ones(5), mode="same")
                smoothed = np.convolve(values, np.ones(5), mode="same") / weights
                plot.plot(x, smoothed, pen=pg.mkPen(color, width=2), name=f"{angle}°")
            plot.setXRange(1, max(2, len(rows)), padding=.02)
            plot.setYRange(0, 105, padding=0)
            plot.showGrid(x=True, y=True, alpha=.16)
            self.lockAnalysisPlots(plot)

    def refreshAngleReportPhaseTable(self) -> None:
        """Match PPT slides 7/9/11/13/15/17/19 with an eight-angle table."""
        table = self.angleReportPhaseTable
        table.clearContents()
        if self.angleAnalysisResult is None:
            table.setRowCount(0)
            return
        muscle = self.angleReportPhaseMuscleCombo.currentData()
        if not muscle:
            table.setRowCount(0)
            return
        result = self.angleAnalysisResult
        summary, profiles = result["summary"], result["profiles"]
        bins = int(result["settings"]["bins"])
        centers = (np.arange(bins) + .5) * 360 / bins
        target_angles = tuple(range(0, 360, 45))
        by_side = {
            side: summary[(summary.muscle == muscle) & (summary.side == side)].iloc[0]
            if not summary[(summary.muscle == muscle) & (summary.side == side)].empty else None
            for side in ("L", "R")
        }

        def phase_value(entry, phase: str, angle: int) -> float | None:
            if entry is None:
                return None
            curve = profiles[(profiles.channel == entry.channel) & (profiles.phase == phase)]
            if curve.empty:
                return None
            distance = np.abs((curve.angle_bin_center.to_numpy(float) - angle + 180) % 360 - 180)
            values = curve.mean_percent.to_numpy(float)[distance <= 15 + 1e-9]
            return float(np.mean(values)) if len(values) else None

        table.setRowCount(len(target_angles) + 2)
        for row, angle in enumerate(target_angles):
            left_start = phase_value(by_side["L"], "start", angle)
            left_end = phase_value(by_side["L"], "end", angle)
            right_start = phase_value(by_side["R"], "start", angle)
            right_end = phase_value(by_side["R"], "end", angle)
            values = (
                f"{angle}° (±15°)",
                f"{left_start:.1f}%" if left_start is not None else "—",
                f"{left_end:.1f}%" if left_end is not None else "—",
                f"{left_end - left_start:+.1f}%" if left_start is not None and left_end is not None else "—",
                f"{right_start:.1f}%" if right_start is not None else "—",
                f"{right_end:.1f}%" if right_end is not None else "—",
                f"{right_end - right_start:+.1f}%" if right_start is not None and right_end is not None else "—",
            )
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))

        for row, label, start_key, end_key, unit in (
            (len(target_angles), "RMS", "rms_uv_start", "rms_uv_end", " µV"),
            (len(target_angles) + 1, "MDF", "mdf_start", "mdf_end", " Hz"),
        ):
            values = [label]
            for side in ("L", "R"):
                entry = by_side[side]
                if entry is None:
                    values.extend(("—", "—", "—"))
                    continue
                start, end = float(getattr(entry, start_key)), float(getattr(entry, end_key))
                values.extend((f"{start:.1f}{unit}", f"{end:.1f}{unit}", f"{end - start:+.1f}{unit}"))
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(value))

    def refreshAnglePhase(self) -> None:
        """Show the direct, phase-specific answer before the detailed plots."""
        if self.angleAnalysisResult is None:
            return
        angle = self.anglePhaseSpin.value()
        values = phase_intensities(self.angleAnalysisResult, angle)
        if values.empty:
            return
        strongest_end = values.iloc[0]
        strongest_start = values.sort_values("start_percent", ascending=False).iloc[0]
        angle_label = self.angleAnalysisResult["sync"]["angle_label"]
        self.anglePhaseLabel.setText(
            f"{angle_label} {angle}°｜後段相對活化最高：{strongest_end.location_zh}"
            f"（{strongest_end.channel_label}）{strongest_end.end_percent:.0f}%；"
            f"前段相對活化最高：{strongest_start.location_zh}（{strongest_start.channel_label}）"
            f"{strongest_start.start_percent:.0f}%。藍色為前段、紅色為後段。"
            "此為各肌肉內的相對 EMG 活化，不代表哪條肌肉實際最費力。"
        )
        self.anglePhasePlot.clear()
        self.anglePhasePlot.setTitle(f"{angle}° 肌肉相對 EMG 活化｜藍色：前段；紅色：後段")
        axis = self.anglePhasePlot.getAxis("left")
        ticks = []
        tokens = (FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark"
                  else FluentThemeManager.LIGHT)
        for row, entry in values.iterrows():
            label = f"{entry.location_zh} · {entry.channel_label}"
            ticks.append((row, label))
            self.anglePhasePlot.plot([0, entry.start_percent], [row - .18, row - .18],pen=pg.mkPen(tokens["brand_background"], width=8))
            self.anglePhasePlot.plot([0, entry.end_percent], [row + .18, row + .18],pen=pg.mkPen(tokens["danger"], width=8))
        axis.setTicks([ticks])
        self.anglePhasePlot.setYRange(-.7, len(values) - .3, padding=0)
        self.anglePhasePlot.setXRange(0, max(100, float(values[["start_percent", "end_percent"]].max().max()) * 1.08), padding=0)
        self.anglePhasePlot.showGrid(x=True, y=False, alpha=.18)
        if hasattr(self, "angleHeatPhaseLine"):
            self.angleHeatPhaseLine.setPos(angle)
        self.refreshAngleCurve()

    def refreshAngleCurve(self) -> None:
        self.angleCurvePlot.clear()
        if self.angleAnalysisResult is None:
            return
        name = self.angleMuscleCombo.currentData()
        if not name:
            return
        profiles = self.angleAnalysisResult["profiles"]
        summary = self.angleAnalysisResult["summary"]
        entry = summary[summary.channel == name].iloc[0]
        self.angleCurvePlot.setTitle(
            f"{entry.location_zh}｜RMS {entry.rms_change_percent:+.1f}%｜"
            f"峰值相位 {entry.peak_angle_start:.0f}° → {entry.peak_angle_end:.0f}°（{entry.peak_angle_shift:+.0f}°）")
        self.angleDetailLabel.setText(
            f"{entry.location_zh}｜前段 {entry.n_start} 圈、後段 {entry.n_end} 圈。"
            f"RMS：{entry.rms_uv_start:.1f} → {entry.rms_uv_end:.1f} µV（{entry.rms_change_percent:+.1f}%）；"
            f"峰值：{entry.peak_angle_start:.0f}° → {entry.peak_angle_end:.0f}°（{entry.peak_angle_shift:+.0f}°）。")
        for phase, color, peak in (("start", "#1684d7", entry.peak_angle_start),
                                   ("end", "#d13438", entry.peak_angle_end)):
            sub = profiles[(profiles.channel == name) & (profiles.phase == phase)]
            self.angleCurvePlot.plot(sub.angle_bin_center.to_numpy(float), sub.mean_percent.to_numpy(float),
                                     pen=pg.mkPen(color, width=2),
                                     name=f'{"前段" if phase == "start" else "後段"} {entry.n_start if phase == "start" else entry.n_end} 圈，峰值 {peak:.0f}°')
            self.angleCurvePlot.addItem(pg.InfiniteLine(pos=peak, angle=90, pen=pg.mkPen(color, style=Qt.DashLine)))
        selected_angle = self.anglePhaseSpin.value()
        self.angleCurvePlot.addItem(pg.InfiniteLine(pos=selected_angle, angle=90, pen=pg.mkPen("#555555", width=2, style=Qt.DotLine),label=f"查看 {selected_angle}°", labelOpts={"position": .9, "color": "#555555"},))
        self.angleCurvePlot.setXRange(0, 360, padding=0)

    def showSelectedAngleMuscle(self) -> None:
        row = self.angleComparisonTable.currentRow()
        if row < 0 or self.angleAnalysisResult is None:
            return
        summary = self.angleAnalysisResult["summary"]
        if row >= len(summary):
            return
        channel = summary.iloc[row].channel
        index = self.angleMuscleCombo.findData(channel)
        if index >= 0:
            self.angleMuscleCombo.setCurrentIndex(index)
            self.angleViews.setCurrentWidget(self.angleDetailPage)

    def angleAnalysisFailed(self, message: str) -> None:
        worker = self.angleAnalysisWorker
        if worker is None or worker.emg_path != self.angleEmgPath or worker.pedal_path != self.anglePedalPath:
            return
        self.angleStatusLabel.setText(f"分析失敗：{message}")
        self.angleOverviewLabel.setText(f"本次相位聯合分析未完成：{message}")

    def angleAnalysisFinished(self) -> None:
        self.angleAnalysisWorker = None
        if self.pendingAutoAngleAnalysis:
            QTimer.singleShot(0, self.startAutomaticAngleAnalysis)

    def exportAngleAnalysis(self) -> None:
        if self.angleAnalysisResult is None or self.angleExportWorker is not None:
            return
        self.tabs.setCurrentWidget(self.summaryTab)
        self.summaryTabs.setCurrentIndex(2)
        folder = QFileDialog.getExistingDirectory(self, "選擇角度分析匯出資料夾", self.pathEdit.text().strip())
        if not folder:
            return
        base = Path(folder) / f"angle_analysis_{datetime.now():%Y%m%d_%H%M%S}"
        target = base
        suffix = 2
        while target.exists():
            target = Path(f"{base}_{suffix:02d}")
            suffix += 1
        self.angleExportButton.setEnabled(False)
        self.angleStatusLabel.setText("正在背景匯出同步 CSV、分析表與圖片…")
        worker = AngleExportWorker(self.angleAnalysisResult, target, True)
        self.angleExportWorker = worker
        worker.completed.connect(lambda path: self.angleStatusLabel.setText(f"角度分析已匯出至 {path}"))
        worker.failed.connect(lambda message: self.angleStatusLabel.setText(f"匯出失敗：{message}"))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(self.angleExportFinished)
        worker.start()

    def angleExportFinished(self) -> None:
        self.angleExportWorker = None
        self.angleExportButton.setEnabled(self.angleAnalysisResult is not None)

    def exportRideReport(self) -> None:
        session = self.rideSession or self.latestRideSession
        if session is None:
            QMessageBox.information(
                self, "匯出 HTML 報表",
                "請先完成或載入一次測試資料。",
            )
            return
        stem = (self.collectionOutput or Path(self.pathEdit.text()) / "cycling").with_suffix("")
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出 HTML 報表", str(stem) + "_report.html", "HTML 網頁 (*.html)"
        )
        if not path:
            return
        try:
            output = export_report(
                Path(path).with_suffix(".html"), session,
                pedal_records=self.summaryRecords,
                ftp_w=self.analysisFtpSpin.value(),
                angle_result=self.angleAnalysisResult,
                reference_session=self.referenceRideSession,
            )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "報表匯出失敗", str(exc))
            return
        self.appendLog(f"HTML 報表已匯出：{output.name}")

    def startStream(self) -> None:
        if self.calibrationOnly:
            self.startCalibrationStream()
            return
        if self.useGoProCheck.isChecked() and not self.goproAddress:
            # GoPro is optional. Do not prevent EMG/trainer/pedal collection
            # just because the camera was left checked without configuration.
            self.useGoProCheck.setChecked(False)
            self.goproStatusLabel.setText("尚未選擇 攝影機；本次將不錄影，其他資料照常收集")
            self.appendLog("攝影機 | no camera selected; collection continues without video")
        if not any((self.useEmgCheck.isChecked(), self.useTrainerCheck.isChecked(), self.usePedalCheck.isChecked(), self.useGoProCheck.isChecked())):
            QMessageBox.warning(self, "尚未選擇設備", "請在第一頁至少勾選一個本次要測試的設備。")
            self.tabs.setCurrentWidget(self.settingsTab)
            return
        if self.useGoProCheck.isChecked() and not self.goproAddress:
            QMessageBox.warning(self, "攝影機 尚未選擇", "請先在設定頁掃描 攝影機、點選它，並按「設定為 攝影機」。")
            self.tabs.setCurrentWidget(self.settingsTab)
            return
        if not self.useEmgCheck.isChecked():
            self.startBikeOnlySession()
            return
        if self.useTrainerCheck.isChecked() and not self.trainerIsConnected:
            QMessageBox.warning(self, "阻力計未連線", "請先連線阻力計，或取消勾選阻力計。")
            return
        if self.usePedalCheck.isChecked() and not self.pedalIsConnected:
            QMessageBox.warning(self, "踏板未連線", "請先連線踏板，或取消勾選踏板。")
            return
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "缺少位址", "請輸入 SDK Server IP 位址。")
            return
        if not self.collectionSetupIsValid():
            return
        directoryText = self.pathEdit.text().strip()
        if not directoryText:
            QMessageBox.warning(self, "缺少輸出資料夾", "請選擇 CSV 輸出資料夾。")
            return
        directory = Path(directoryText).expanduser()
        if directory.exists() and not directory.is_dir():
            QMessageBox.warning(self, "輸出位置無效", "請選擇資料夾，而不是檔案。")
            return
        output = self.outputPathForCollection(directory)
        trainerOutput = trainerOutputPath(output)
        infoOutput = infoOutputPath(output)
        self.collectionSummary = None
        self.collectionError = None
        self.latestPedalRecords = []
        self.currentSessionSummaryButton.setEnabled(False)
        self.collectionOutput = output
        self.collectionSubject = self.subjectInfo()
        self.infoOutput = infoOutput
        self.trainerRecorder = None
        self.trainerOutput = trainerOutput if self.useTrainerCheck.isChecked() and self.trainerIsConnected else None
        self.pedalRecorder = None
        self.pedalOutput = pedalOutputPath(output) if self.usePedalCheck.isChecked() and self.pedalIsConnected else None
        self.infoRecorder = CollectionInfoRecorder(infoOutput, self.collectionSubject, output, self.trainerOutput)
        try:
            self.infoRecorder.start()
        except OSError as exc:
            self.infoRecorder = None
            QMessageBox.critical(self, "資訊檔輸出錯誤", str(exc))
            return
        self.beginRideAnalysis()
        self.collectionStoppedEarly = False
        self.startDatabaseRecording()
        self.emgRateLabel.setText("EMG 頻率：正在向 SDK 查詢…")
        for history in self.histories:
            history.clear()
        self.streamWorker = StreamWorker(host, output, self.mappings)
        self.streamWorker.status_changed.connect(self.streamStatusChanged)
        self.streamWorker.connected.connect(lambda banner: self.emgStatus.setToolTip(banner))
        self.streamWorker.base_ready.connect(lambda serial: self.baseLabel.setText(f"Base: {serial}"))
        self.streamWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.streamWorker.sample_rate_ready.connect(self.emgRateReady)
        self.streamWorker.collection_metadata_ready.connect(self.collectionMetadataReady)
        self.streamWorker.frames_ready.connect(self.framesReceived)
        self.streamWorker.collection_completed.connect(self.collectionCompleted)
        self.streamWorker.failed.connect(self.streamFailed)
        self.streamWorker.finished.connect(self.streamFinished)
        self.setStreaming(True)
        self.streamWorker.start()
        # Start the camera as collection begins.  Waiting for a later EMG
        # status callback made the GoPro appear unresponsive after this button
        # was pressed; beginGoProRecording guards against duplicate requests.
        self.beginGoProRecording()

    def startCalibrationStream(self) -> None:
        host = self.hostEdit.text().strip()
        if not host:
            QMessageBox.warning(self, "基準測量", "請輸入 Trigno SDK Server IP。")
            self.calibrationOnly = False
            return
        self.collectionError = None
        self.collectionSummary = None
        self.calibrationRecording = self.trainerIsConnected or self.pedalIsConnected
        output = None
        self.trainerRecorder = None
        self.pedalRecorder = None
        self.infoRecorder = None
        self.trainerOutput = None
        self.pedalOutput = None
        if self.calibrationRecording:
            if not self.collectionSetupIsValid():
                self.calibrationOnly = False
                return
            directory_text = self.pathEdit.text().strip()
            directory = Path(directory_text).expanduser()
            if not directory_text or (directory.exists() and not directory.is_dir()):
                QMessageBox.warning(self, "基準測量", "請選擇有效的 CSV 輸出資料夾。")
                self.calibrationOnly = False
                return
            output = self.outputPathForCollection(directory)
            self.collectionOutput = output
            self.collectionSubject = self.subjectInfo()
            self.trainerOutput = trainerOutputPath(output) if self.trainerIsConnected else None
            self.pedalOutput = pedalOutputPath(output) if self.pedalIsConnected else None
            self.infoOutput = infoOutputPath(output)
            self.infoRecorder = CollectionInfoRecorder(self.infoOutput, self.collectionSubject, output, self.trainerOutput)
            try:
                self.infoRecorder.start()
            except OSError as exc:
                self.infoRecorder = None
                self.calibrationOnly = False
                QMessageBox.critical(self, "基準測量", str(exc))
                return
        self.preprocessor = None
        self.rmsExtractor = None
        self.streamWorker = StreamWorker(host, output, self.mappings)
        self.streamWorker.status_changed.connect(self.streamStatusChanged)
        self.streamWorker.sensor_states_ready.connect(self.updateSensorStates)
        self.streamWorker.sample_rate_ready.connect(self.emgRateReady)
        self.streamWorker.frames_ready.connect(self.framesReceived)
        self.streamWorker.collection_completed.connect(self.collectionCompleted)
        self.streamWorker.failed.connect(self.streamFailed)
        self.streamWorker.finished.connect(self.streamFinished)
        self.calibrationLabel.setText("基準測量：0 / 20 秒（前 10 秒捨棄，後 10 秒計算 RMS）")
        self.calibrationLabel.show()
        self.setStreaming(True)
        self.streamWorker.start()

    def startBikeOnlySession(self) -> None:
        """Record selected trainer/pedal data without starting the EMG SDK."""
        if not self.collectionSetupIsValid():
            return
        if self.useTrainerCheck.isChecked() and not self.trainerIsConnected:
            QMessageBox.warning(self, "阻力計未連線", "請先連線阻力計，或取消勾選阻力計。")
            return
        if self.usePedalCheck.isChecked() and not self.pedalIsConnected:
            QMessageBox.warning(self, "踏板未連線", "請先連線踏板，或取消勾選踏板。")
            return
        directoryText = self.pathEdit.text().strip()
        if not directoryText:
            QMessageBox.warning(self, "缺少輸出位置", "請選擇 CSV 輸出資料夾。")
            return
        directory = Path(directoryText).expanduser()
        if directory.exists() and not directory.is_dir():
            QMessageBox.warning(self, "輸出位置無效", "請選擇資料夾，而不是檔案。")
            return
        output = self.outputPathForCollection(directory)
        self.latestPedalRecords = []
        self.currentSessionSummaryButton.setEnabled(False)
        self.collectionOutput = output
        self.collectionSubject = self.subjectInfo()
        self.trainerOutput = trainerOutputPath(output) if self.useTrainerCheck.isChecked() else None
        self.pedalOutput = pedalOutputPath(output) if self.usePedalCheck.isChecked() else None
        self.infoOutput = infoOutputPath(output)
        self.infoRecorder = CollectionInfoRecorder(self.infoOutput, self.collectionSubject, output, self.trainerOutput)
        try:
            self.infoRecorder.start()
        except OSError as exc:
            self.infoRecorder = None
            QMessageBox.critical(self, "資料檔建立失敗", str(exc))
            return
        self.beginRideAnalysis()
        self.collectionStoppedEarly = False
        self.startDatabaseRecording()
        self.bikeOnlySession = True
        self.beginTrainerRecording()
        self.beginPedalRecording()
        self.beginGoProRecording()
        self.setStreaming(True)
        self.setEmgStatus("僅收集騎乘裝置資料", "streaming")
        self.startCountdown()

    def stopStream(self) -> None:
        if self.remainingSeconds > 0:
            self.collectionStoppedEarly = True
        self.stopGoProRecording()
        if self.bikeOnlySession:
            self.finishBikeOnlySession()
            return
        if self.streamWorker:
            self.setEmgStatus("正在停止…", "working")
            self.stopButton.setEnabled(False)
            self.streamWorker.request_stop()

    def finishBikeOnlySession(self) -> None:
        self.countdownTimer.stop()
        self.stopGoProRecording()
        self.closeTrainerRecording()
        self.closePedalRecording()
        self.finishRideAnalysis()
        self.bikeOnlySession = False
        self.setStreaming(False)
        self.setEmgStatus("騎乘裝置資料收集完成", "connected")

    def streamStatusChanged(self, text: str) -> None:
        self.setEmgStatus(text, "streaming" if text == "串流中" else "working")
        if text == "串流中":
            # A live stream is also a verified SDK connection.  Retain this
            # state when collection stops normally.
            self.emgTested = True
            self.updateEmgConnectionIndicator()
        if text == "串流中" and (not self.calibrationOnly or self.calibrationRecording):
            self.beginTrainerRecording()
            self.beginPedalRecording()
            self.beginGoProRecording()
        if text == "串流中":
            if self.calibrationOnly:
                self.startCalibrationCountdown()
            else:
                self.startCountdown()

    def beginGoProRecording(self) -> None:
        """Start 攝影機 asynchronously once the data collection is live."""
        if not self.useGoProCheck.isChecked():
            return
        if self.goproRecordingStarted:
            self.appendLog("攝影機 | start skipped: app already marked the camera as recording")
            return
        if self.goproWorker and self.goproWorker.isRunning():
            if self.goproWorker.action == "connect":
                self.appendLog("攝影機 | sending start through the pre-connected Bluetooth session")
                self.goproStartConfirmed = False
                self.goproAwaitingStartConfirmation = True
                self.goproConnectionLabel.setText("● 攝影機：正在啟動錄影")
                FluentThemeManager.set_property(self.goproConnectionLabel, "fluentSeverity", "warning")
                self.goproWorker.request_start()
            else:
                self.appendLog("攝影機 | previous command is still running")
            return
        self.appendLog("攝影機 | no pre-connected session; connect and test 攝影機 first")
        self.goproStatusLabel.setText("請先按「配對並測試 攝影機」，保持連線後再開始收集。")

    def stopGoProRecording(self) -> None:
        """Request camera stop without blocking the EMG/CSV shutdown path."""
        # A start worker now owns one persistent BLE session.  Do not open a
        # competing second connection: ask that same worker to send Stop.
        if self.goproWorker and self.goproWorker.isRunning() and self.goproWorker.action == "connect":
            if self.goproStopRequested:
                return
            self.goproStopRequested = True
            self.goproAwaitingStartConfirmation = False
            self.goproRecordingStarted = False
            self.appendLog("攝影機 | stop requested on the active Bluetooth session")
            self.goproConnectionLabel.setText("● 攝影機：正在停止錄影")
            FluentThemeManager.set_property(self.goproConnectionLabel, "fluentSeverity", "warning")
            self.goproWorker.request_stop()
            return
        if not self.goproRecordingStarted:
            return
        self.goproAwaitingStartConfirmation = False
        self.goproRecordingStarted = False
        self.appendLog("攝影機 | stopping recording…")
        self.goproConnectionLabel.setText("● 攝影機：正在停止錄影")
        FluentThemeManager.set_property(self.goproConnectionLabel, "fluentSeverity", "warning")
        worker = GoProCommandWorker("stop", self.goproAddress or None, self.goproIdentifier or None)
        self.goproWorker = worker
        worker.completed.connect(self.goProCommandCompleted)
        worker.failed.connect(lambda message: self.goProCommandFailed("stop", message))
        worker.finished.connect(self.goProCommandFinished)
        worker.start()

    def goProCommandCompleted(self, result: GoProResult) -> None:
        self.goproAddress = result.address
        self.settings.setValue("gopro/address", result.address)
        if result.wifi_ssid and result.wifi_password:
            self.goproWifiSsid = result.wifi_ssid
            self.goproWifiPassword = result.wifi_password
        timestamp = result.timestamp.isoformat(timespec="milliseconds")
        if result.action == "start":
            self.goproRecordingStarted = True
            self.setGoProConnectionState(True, recording=True)
            if self.goproStartConfirmed:
                self.setGoProRecordingConfirmed()
        elif result.action == "stop":
            self.goproRecordingStarted = False
            self.goproAwaitingStartConfirmation = False
            self.goproStopRequested = False
            self.setGoProConnectionState(True)
        else:
            self.setGoProConnectionState(True)
        if result.action == "verify" and hasattr(self, "goproStatusLabel"):
            self.goproStatusLabel.setText(f"已連線：{result.name}（{result.address}）；未開始錄影")
            self.goproDisconnectButton.setEnabled(True)
        elif result.action == "start" and hasattr(self, "goproStatusLabel"):
            self.goproStatusLabel.setText("錄影指令已送出；請確認 攝影機 螢幕上的紅點／計時器")
        elif result.action == "stop" and hasattr(self, "goproStatusLabel"):
            self.goproStatusLabel.setText("已連線；攝影機 已停止錄影")
        if self.rideSession is not None:
            video = self.rideSession.setdefault("gopro", {})
            video[f"{result.action}_timestamp"] = timestamp
            video["camera_name"] = result.name
            video["bluetooth_address"] = result.address
            for detail in result.details:
                if detail.startswith("Camera model: "):
                    video["camera_model"] = detail.removeprefix("Camera model: ")
                elif detail.startswith("Firmware revision: "):
                    video["firmware_revision"] = detail.removeprefix("Firmware revision: ")
        self.appendLog(f"攝影機 | {result.action} 成功：{result.name}，時間 {timestamp}")
        for detail in result.details:
            self.appendLog(f"攝影機 | {detail}")

    def goProDiagnostic(self, message: str) -> None:
        """Log BLE notifications and promote successful shutter responses to UI state."""
        self.appendLog(f"攝影機 | {message}")
        # Open GoPro command response format is: length, command id, status.
        # 02 01 00 therefore confirms shutter (01) succeeded (00).
        if message.strip().endswith("02 01 00") and self.goproAwaitingStartConfirmation:
            self.goproStartConfirmed = True
            if self.goproRecordingStarted:
                self.setGoProRecordingConfirmed()

    def setGoProRecordingConfirmed(self) -> None:
        """Show a camera-confirmed recording state in the connection badge."""
        self.goproConnectionLabel.setText("● 攝影機：錄影中（已確認）")
        self.goproConnectionLabel.setToolTip("攝影機 已回覆快門指令成功，正在錄影。")
        FluentThemeManager.set_property(self.goproConnectionLabel, "fluentSeverity", "success")
        if hasattr(self, "goproStatusLabel"):
            self.goproStatusLabel.setText("攝影機 錄影中；已收到快門成功回覆")

    def goProCommandFailed(self, action: str, message: str) -> None:
        # A camera is optional: data acquisition remains valid when it is absent.
        self.goproRecordingStarted = False
        self.goproAwaitingStartConfirmation = False
        self.setGoProConnectionState(False)
        if hasattr(self, "goproDisconnectButton"):
            self.goproDisconnectButton.setEnabled(False)
        if action == "verify" and hasattr(self, "goproStatusLabel"):
            self.goproStatusLabel.setText(f"連線失敗：{message}")
        elif hasattr(self, "goproStatusLabel"):
            self.goproStatusLabel.setText(f"攝影機 {action} 失敗：{message}")
        self.appendLog(f"攝影機 | {action} 失敗，EMG 收集持續進行：{message}")

    def goProCommandFinished(self) -> None:
        worker, self.goproWorker = self.goproWorker, None
        self.goproStopRequested = False
        if worker:
            worker.deleteLater()
            if worker.action == "connect" and not self.goproRecordingStarted:
                self.setGoProConnectionState(False)
                if hasattr(self, "goproDisconnectButton"):
                    self.goproDisconnectButton.setEnabled(False)
        if hasattr(self, "goproTestButton"):
            self.goproTestButton.setEnabled(not self.streamWorker and not self.bikeOnlySession and not self.goproRecordingStarted)
        if self.goproStopPending:
            self.goproStopPending = False
            QTimer.singleShot(0, self.stopGoProRecording)

    def beginTrainerRecording(self) -> None:
        self.beginCollectionDistance()
        if self.trainerOutput is None:
            self.appendLog("阻力計未連線；本次不輸出阻力計 CSV")
            return
        try:
            self.trainerRecorder = TrainerCsvRecorder(self.trainerOutput)
            self.trainerRecorder.start()
            if self.infoRecorder:
                native = (
                    f"{self.commandedNativeResistance:g}"
                    if self.commandedNativeResistance is not None else "設備未回報"
                )
                self.infoRecorder.setSettings("阻力計", [
                    f"固定阻力等級：{self.resistanceSpin.value()} / 30",
                    f"設備原生阻力：{native}",
                ])
            self.appendLog(f"阻力計 CSV 開始輸出：{self.trainerOutput.name}")
        except OSError as exc:
            self.trainerRecorder = None
            self.appendLog(f"阻力計 CSV 輸出失敗：{exc}")
            QMessageBox.warning(self, "阻力計 CSV 輸出錯誤", str(exc))

    def closeTrainerRecording(self) -> None:
        if self.trainerRecorder is None:
            self.distanceSessionActive = False
            return
        recorder, self.trainerRecorder = self.trainerRecorder, None
        try:
            elapsed = recorder.close()
        except OSError as exc:
            self.appendLog(f"阻力計 CSV 結束寫入失敗：{exc}")
            return
        self.appendLog(f"阻力計 CSV 已輸出 {recorder.rows} 筆 telemetry")
        if self.infoRecorder:
            self.infoRecorder.append("阻力計資料", [
                f"阻力計記錄筆數：{recorder.rows}",
                f"阻力計收集時間：{elapsed:.1f} 秒",
                f"本次騎乘距離：{self.collectionDistanceMeters or 0} m",
            ])
        self.distanceSessionActive = False

    def beginPedalRecording(self) -> None:
        if self.pedalOutput is None:
            self.appendLog("踏板未連線，本次不輸出踏板 CSV")
            return
        try:
            self.pedalRecorder = PedalCsvRecorder(self.pedalOutput)
            self.pedalRecorder.start()
            if self.infoRecorder:
                device = self.pedalDevices.currentText().strip() or "已連線踏板"
                self.infoRecorder.setSettings("踏板", [f"功率計裝置：{device}"])
            self.pedalSummarySource = "current"
            self.summaryRecords = []
            self.currentSessionSummaryButton.setEnabled(True)
            self.appendLog(f"踏板 CSV 開始輸出：{self.pedalOutput.name}")
        except OSError as exc:
            self.pedalRecorder = None
            self.appendLog(f"踏板 CSV 建立失敗：{exc}")
            QMessageBox.warning(self, "踏板 CSV 建立失敗", str(exc))

    def closePedalRecording(self) -> None:
        if self.pedalRecorder is None:
            return
        recorder, self.pedalRecorder = self.pedalRecorder, None
        elapsed = recorder.close()
        self.latestPedalRecords = list(recorder.records)
        summary = summarize_pedal_records(self.latestPedalRecords)
        self.showCurrentSessionSummary()
        self.appendLog(f"踏板 CSV 已完成：{recorder.rows} 筆資料")
        if recorder.records:
            try:
                fitPath = exportPedalFit(recorder.records, recorder.outputPath.with_suffix(".fit"))
                self.appendLog(f"踏板 FIT 已完成：{fitPath.name}")
            except Exception as exc:
                self.appendLog(f"踏板 FIT 輸出失敗：{exc}")
        if self.infoRecorder:
            self.infoRecorder.append("踏板資料", [
                *pedal_summary_lines(summary),
            ])

    def setSummaryRecords(self, records: list[dict], source: str) -> None:
        """Render one standardized summary on the persistent summary page."""
        self.summaryRecords = list(records)
        self.pedalSummarySource = "imported" if "匯入踏板 CSV" in source else "current"
        if self.angleEmgPath is not None or self.anglePedalPath is not None:
            self.updateAngleFileStatus()
        else:
            self.summarySourceLabel.setText(source)
        self.refreshSummaryAnalysis()

    def refreshSummaryAnalysis(self) -> None:
        """Refresh the numerical report and time-series plots for the selected source."""
        if not self.summaryRecords:
            self.summaryDetails.setText("完成踏板收集，或匯入踏板 CSV 後，摘要會顯示在這裡")
            for label in self.pedalMetricCards.values():
                label.setText("--")
            if self.pedalZoneTable is not None:
                self.pedalZoneTable.clearContents()
            for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot):
                plot.clear()
            self.leftPowerBalanceRing.setValues(None, None)
            self.rightPowerBalanceRing.setValues(None, None)
            return
        ftp = self.analysisFtpSpin.value()
        self.settings.setValue("analysis/ftp_w", ftp)
        summary = summarize_pedal_records(self.summaryRecords)
        analysis = analyze_pedal_records(self.summaryRecords, ftp)
        self.leftPowerBalanceRing.setValues(
            summary.get("average_left_balance_percent"), summary.get("average_left_power_w"),
        )
        self.rightPowerBalanceRing.setValues(
            summary.get("average_right_balance_percent"), summary.get("average_right_power_w"),
        )
        self.summaryDetails.setText("\n".join([
            *pedal_summary_lines(summary), "", *pedal_analysis_lines(self.summaryRecords, ftp),
        ]))
        for key, label in self.pedalMetricCards.items():
            value = analysis[key]
            label.setText("--" if value is None else f"{value:.0f}" if key in ("peak_5s_w", "coverage_percent") else f"{value:.2f}" if key == "intensity_factor" else f"{value:.0f}")
        if self.pedalZoneTable is not None:
            self.pedalZoneTable.setRowCount(len(analysis["zones"]))
            for row, zone in enumerate(analysis["zones"]):
                for column, value in enumerate((zone["name"], zone["bounds"], f"{zone['seconds']} 秒", f"{zone['percent']:.1f}%")):
                    self.pedalZoneTable.setItem(row, column, QTableWidgetItem(value))
        series = analysis["series"]
        x = np.array([point["elapsed_s"] for point in series], dtype=float)
        power = np.array([np.nan if point["power_w"] is None else point["power_w"] for point in series], dtype=float)
        cadence = np.array([np.nan if point["cadence_rpm"] is None else point["cadence_rpm"] for point in series], dtype=float)
        left = np.array([np.nan if point["left_balance_percent"] is None else point["left_balance_percent"] for point in series], dtype=float)
        for plot in (self.powerAnalysisPlot, self.cadenceAnalysisPlot, self.balanceAnalysisPlot):
            plot.clear()
        if not len(x):
            return
        tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
        power_color = QColor(tokens["danger"])
        power_fill = QColor(power_color)
        power_fill.setAlpha(64)
        self.powerAnalysisPlot.plot(
            x, power, pen=pg.mkPen(power_color, width=2), fillLevel=0,
            brush=pg.mkBrush(power_fill), connect="finite",
        )
        average_power = summary.get("average_power_w")
        if average_power is not None:
            self.powerAnalysisPlot.addItem(pg.InfiniteLine(pos=float(average_power), angle=0,
                                                           pen=pg.mkPen(tokens["text_secondary"], style=Qt.DashLine)))
        maximum_power = summary.get("maximum_power_w")
        if maximum_power is not None:
            self.powerAnalysisPlot.addItem(pg.InfiniteLine(pos=float(maximum_power), angle=0,
                                                           pen=pg.mkPen(tokens["danger"], style=Qt.DotLine)))
        if average_power is not None and maximum_power is not None:
            self.powerAnalysisPlot.setTitle(
                f"功率 Power ｜ 平均 {average_power:.0f} W ｜ 最大 {maximum_power:.0f} W"
            )
        else:
            self.powerAnalysisPlot.setTitle("功率 Power ｜ 無足夠的功率資料")
        cadence_color = QColor(tokens["warning"])
        cadence_fill = QColor(cadence_color)
        cadence_fill.setAlpha(64)
        self.cadenceAnalysisPlot.plot(
            x, cadence, pen=pg.mkPen(cadence_color, width=2), fillLevel=0,
            brush=pg.mkBrush(cadence_fill), connect="finite",
        )
        average_cadence = summary.get("average_cadence_rpm")
        if average_cadence is not None:
            self.cadenceAnalysisPlot.addItem(pg.InfiniteLine(pos=float(average_cadence), angle=0,
                                                             pen=pg.mkPen(tokens["text_secondary"], style=Qt.DashLine)))
        maximum_cadence = summary.get("maximum_cadence_rpm")
        if maximum_cadence is not None:
            self.cadenceAnalysisPlot.addItem(pg.InfiniteLine(pos=float(maximum_cadence), angle=0,
                                                             pen=pg.mkPen(tokens["danger"], style=Qt.DotLine)))
        if average_cadence is not None and maximum_cadence is not None:
            self.cadenceAnalysisPlot.setTitle(
                f"踏頻 Cadence ｜ 平均 {average_cadence:.0f} rpm ｜ 最大 {maximum_cadence:.0f} rpm"
            )
        else:
            self.cadenceAnalysisPlot.setTitle("踏頻 Cadence ｜ 無足夠的踏頻資料")
        valid = np.isfinite(left)
        if valid.any():
            self.balanceAnalysisPlot.plot(x[valid], left[valid], pen=None, symbol="o", symbolSize=6,
                                          symbolBrush="#43a047", name="左")
            self.balanceAnalysisPlot.plot(x[valid], 100 - left[valid], pen=None, symbol="o", symbolSize=6,
                                          symbolBrush="#1684d7", name="右")

    def showCurrentSessionSummary(self) -> None:
        if self.rideSession is not None and self.pedalRecorder is not None:
            self.setSummaryRecords(self.pedalRecorder.records, "資料來源：本次騎乘踏板即時資料")
            self.summaryTabs.setCurrentIndex(1)
            return
        if self.collectedRideSession is not None:
            if self.latestPedalRecords:
                self.summaryRecords = list(self.latestPedalRecords)
                self.pedalSummarySource = "current"
                self.refreshSummaryAnalysis()
            self.latestRideSession = self.collectedRideSession
            self.updateAngleFileStatus()
            self.refreshRideAnalysis()
            self.refreshComparison()
            self.summaryTabs.setCurrentIndex(0)
            return
        if not self.latestPedalRecords:
            QMessageBox.information(self, "本次摘要", "本次尚未收集到騎乘資料。")
            return
        self.currentSessionSummaryButton.setEnabled(True)
        self.setSummaryRecords(self.latestPedalRecords, "資料來源：本程式最新收集的踏板資料")

    def importPedalCsvSummary(self) -> None:
        start_dir = str(self.angleEmgPath.parent) if self.angleEmgPath else self.pathEdit.text().strip()
        path, _ = QFileDialog.getOpenFileName(self, "匯入踏板或校正角度 CSV", start_dir, "CSV (*.csv)")
        if not path:
            return
        selected = Path(path)
        try:
            calibrated = is_calibrated_angle_csv(selected)
            if calibrated:
                _angles(selected)
                records = None
            else:
                records = load_pedal_csv(selected)
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            QMessageBox.warning(self, "無法匯入踏板 CSV", str(exc))
            return
        self.anglePedalPath = selected
        self.angleAnalysisResult = None
        self.angleExportButton.setEnabled(False)
        self.updateAngleFileStatus()
        if calibrated:
            self.summaryRecords = []
            self.pedalSummarySource = "imported"
            self.refreshSummaryAnalysis()
            self.summaryDetails.setText("已匯入校正角度 CSV；此檔沒有功率摘要。")
            self.updateAngleFileStatus()
            self.summaryTabs.setCurrentIndex(2)
            self.appendLog(f"已匯入校正角度 CSV：{selected.name}")
        else:
            self.setSummaryRecords(records, f"資料來源：匯入踏板 CSV — {selected.name}")
            self.summaryTabs.setCurrentIndex(1)
            self.appendLog(f"已匯入踏板 CSV：{selected.name}（{len(records)} 筆資料）")
        self.queueAngleAnalysis()

    def collectionMetadataReady(self, metadata: dict) -> None:
        if self.infoRecorder is None:
            return
        duration = self.configuredDurationSeconds()
        lines = [
            f"收集時間：{duration} 秒" if duration else "收集時間：手動停止",
            "測試肌肉：",
        ]
        lines.extend(
            "  {chinese_name}（{english_name}）".format(**channel)
            for channel in metadata["channels"]
        )
        lines.append("基準 RMS：")
        for channel in metadata["channels"]:
            record = self.baselineManager.records.get(channel["slot"])
            if record is None:
                value = "未設定"
            else:
                value = f"{record.rms_mv:.6f} mV"
            lines.append(f"  {channel['chinese_name']}：{value}")
        self.infoRecorder.append("EMG 資料", lines)

    def configuredDurationSeconds(self) -> int:
        return self.durationMinutesSpin.value() * 60 + self.durationSecondsSpin.value()

    def updateCountdownLabel(self) -> None:
        if self.remainingSeconds <= 0:
            if self.countdownMode != "calibration" and self.configuredDurationSeconds() <= 0:
                self.countdownLabel.hide()
                return
        self.countdownLabel.setText(f"剩餘 {max(0, self.remainingSeconds)} 秒")
        self.countdownLabel.show()

    def resetCountdownDisplay(self) -> None:
        if not self.countdownTimer.isActive():
            self.remainingSeconds = self.configuredDurationSeconds()
            self.updateCountdownLabel()

    def startCountdown(self) -> None:
        self.countdownMode = "collection"
        self.countdownStopsStream = True
        self.remainingSeconds = self.configuredDurationSeconds()
        self.updateCountdownLabel()
        if self.remainingSeconds > 0:
            self.countdownTimer.start()

    def startCalibrationCountdown(self) -> None:
        """Show the fixed 20-second countdown without controlling capture."""
        self.countdownMode = "calibration"
        self.countdownStopsStream = False
        self.remainingSeconds = 20
        self.updateCountdownLabel()
        self.countdownTimer.start()

    def updateCountdown(self) -> None:
        self.remainingSeconds -= 1
        self.updateCountdownLabel()
        if self.remainingSeconds <= 0:
            self.countdownTimer.stop()
            if not self.countdownStopsStream:
                return
            self.appendLog("收集時間到，自動停止串流")
            self.stopStream()

    def framesReceived(self, frames: np.ndarray) -> None:
        if frames.ndim != 2 or frames.shape[1] != CHANNEL_COUNT:
            return
        activeChannels = [channel for channel, active in enumerate(self.sensorActive) if active]
        if not activeChannels:
            return
        for channel in activeChannels:
            self.histories[channel].extend(frames[:, channel].tolist())
        if self.preprocessor is not None and self.rmsExtractor is not None:
            filtered = self.preprocessor.process(frames)
            if self.calibrationSession is not None:
                self.calibrationSession.add(filtered)
                self.calibrationLabel.setText(f"基準測量：{self.calibrationSession.progress_seconds:.1f} / 20 秒（前 10 秒捨棄）")
                if self.calibrationSession.complete:
                    self.finishBaselineCalibration()
            else:
                rms_values = self.rmsExtractor.add(filtered)
                for channel, current in rms_values.items():
                    if channel in activeChannels:
                        self.updateBaselineRow(channel, current)
                if self.rideSession is not None and self.rideTime() - self.lastEmgAnalysisAt >= 0.2:
                    stamp = self.rideTime()
                    self.lastEmgAnalysisAt = stamp
                    self.rideSession["sample_rate_hz"] = self.currentSampleRate
                    for channel in activeChannels:
                        current = rms_values.get(channel)
                        record = self.baselineManager.records.get(channel + 1)
                        compatible = record is not None and self.baselineManager.compatible(
                            record, self.mappingIdentity(channel), self.sensorSerials.get(channel + 1, "Unknown SID"),
                            self.currentSampleRate, self.filterSettings)
                        comparison = compare_rms(current, record.rms_mv if compatible else None)
                        if record is not None and not compatible:
                            comparison["quality"] = "基準不相容"
                        add_point(self.rideSession, "emg", {"time_s": stamp, "slot": channel + 1,
                            "rms_mv": current, **comparison})
                        mapping = self.mappings[channel]
                        percent = comparison.get("percent")
                        alert = "NONE"
                        if isinstance(percent, (int, float)):
                            if abs(percent) >= self.baselineManager.focus_percent:
                                alert = "RED"
                            elif abs(percent) >= self.baselineManager.warning_percent:
                                alert = "YELLOW"
                            else:
                                alert = "NORMAL"
                        self.databaseAddSample("emg", stamp, {
                            "muscle_name": f"{mapping.chinese_name} ({mapping.abbreviation})",
                            "rms_mv": current, "base_rms_mv": record.rms_mv if compatible and record else None,
                            "deviation_pct": percent, "alert_status": alert,
                            "quality_status": comparison.get("quality", "VALID")})
        self.sampleLabel.setText(f"{len(self.histories[activeChannels[0]]):,} samples in view")

    def emgRateReady(self, sampleRate: float, samplesPerFrame: int, frameInterval: float) -> None:
        self.currentSampleRate = sampleRate
        try:
            self.preprocessor = EMGPreprocessor(CHANNEL_COUNT, sampleRate, self.filterSettings)
            self.rmsExtractor = RMSFeatureExtractor(CHANNEL_COUNT, sampleRate)
        except ValueError as exc:
            self.preprocessor = None
            self.rmsExtractor = None
            self.calibrationLabel.setText(f"RMS 未啟用：{exc}")
        self.emgRateLabel.setText(f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samplesPerFrame} samples/frame")

    def finishBaselineCalibration(self) -> None:
        session = self.calibrationSession
        if session is None:
            return
        saved, invalid = [], []
        for channel in session.channels:
            rms = session.rms_mv(channel)
            if rms is None or rms <= 1e-9:
                invalid.append(f"S{channel + 1:02d}")
                continue
            self.baselineManager.set_measured(channel + 1, rms, self.mappingIdentity(channel), self.sensorSerials.get(channel + 1, "Unknown SID"), self.currentSampleRate, self.filterSettings)
            saved.append(f"S{channel + 1:02d}")
        try:
            self.baselineManager.save()
        except OSError as exc:
            QMessageBox.warning(self, "無法保存基準", str(exc))
        self.calibrationLabel.setText(f"基準完成：{', '.join(saved) or '無有效通道'}" + (f"；無效：{', '.join(invalid)}" if invalid else ""))
        self.calibrationSession = None
        self.updateSensorRows()
        self.stopStream()

    def collectionCompleted(self, samples: int, elapsed: float, sampleRate: float) -> None:
        self.collectionSummary = (samples, elapsed, sampleRate)
        self.emgRateLabel.setText(f"EMG SDK 頻率：{sampleRate:.3f} Hz · {samples:,} samples")
        output = self.collectionOutput.name if self.collectionOutput else "CSV"
        self.appendLog(f"EMG | 量測完成：{samples:,} samples，SDK 頻率 {sampleRate:.3f} Hz，已輸出 {output}")
        if self.infoRecorder:
            rms_values: dict[int, list[float]] = {}
            for point in (self.rideSession or {}).get("emg", []):
                slot, rms = point.get("slot"), point.get("rms_mv")
                if isinstance(slot, int) and isinstance(rms, (int, float)) and np.isfinite(rms):
                    rms_values.setdefault(slot, []).append(float(rms))
            rms_lines = ["EMG 肌肉 RMS 平均值："]
            for slot in sorted(rms_values):
                mapping = self.mappings[slot - 1]
                values = rms_values[slot]
                rms_lines.append(f"  {mapping.chinese_name}：{np.mean(values):.6f} mV")
            if len(rms_lines) == 1:
                rms_lines.append("  無足夠資料")
            self.infoRecorder.append("EMG 測試摘要", [
                f"EMG 樣本數：{samples}",
                f"EMG 收集時間：{elapsed:.1f} 秒",
                f"EMG RMS 記錄筆數：{sum(map(len, rms_values.values()))}",
                *rms_lines,
            ])

    def streamFailed(self, message: str) -> None:
        self.collectionError = message
        title = "CSV 輸出錯誤" if message.startswith("CSV 輸出失敗：") else "Trigno 串流錯誤"
        self.setEmgStatus(message, "error")
        if not message.startswith("CSV 輸出失敗："):
            self.setEmgConnectionState(False)
        QMessageBox.critical(self, title, message)

    def streamFinished(self) -> None:
        worker, self.streamWorker = self.streamWorker, None
        if worker:
            worker.deleteLater()
        self.countdownTimer.stop()
        self.stopGoProRecording()
        self.closeTrainerRecording()
        self.closePedalRecording()
        if not self.calibrationOnly:
            self.finishRideAnalysis()
        self.setStreaming(False)
        if self.calibrationOnly:
            self.calibrationOnly = False
            self.calibrationRecording = False
            if self.collectionError:
                self.calibrationLabel.setText("基準測量失敗；既有基準未變更")
            return
        if self.collectionError:
            return
        if self.collectionSummary:
            samples, elapsed, sampleRate = self.collectionSummary
            output = self.collectionOutput.name if self.collectionOutput else "CSV"
            self.setEmgStatus(f"量測完成：{samples:,} samples · {sampleRate:.3f} Hz · 已輸出 {output}","connected",)
            return
        self.setEmgStatus("已停止", "idle")

    def rebuildPlots(self) -> None:
        if not hasattr(self, "plotWidget"):
            return
        selected = [
            index for index, check in enumerate(self.sensorChecks)
            if check.isChecked() and self.sensorActive[index]
        ]
        self.plotWidget.clear()
        self.curves.clear()
        if not selected:
            # Keep the coordinate system visible before EMG sensors have been
            # connected or selected, so the live-waveform area is not blank.
            plot = self.plotWidget.addPlot(row=0, col=0)
            plot.setLabel("left", "Amplitude", units="mV")
            plot.setLabel("bottom", "Time", units="s")
            plot.showGrid(x=True, y=True, alpha=0.16)
            plot.setXRange(-10, 0, padding=0)
            plot.setYRange(-1, 1, padding=0)
            plot.setMenuEnabled(False)
            self.attachPlotFitButton(plot, "即時 EMG 波形", self.resetLivePlotView)
            return
        for row, channel in enumerate(selected):
            mapping = self.mappings[channel]
            plot = self.plotWidget.addPlot(row=row, col=0)
            plot.setLabel("left", mapping.abbreviation or f"S{channel + 1:02d}", units="mV")
            if row == len(selected) - 1:
                plot.setLabel("bottom", "時間", units="s")
            else:
                plot.hideAxis("bottom")
            plot.showGrid(x=True, y=True, alpha=0.16)
            plot.setMenuEnabled(False)
            self.attachPlotFitButton(plot, f"{mapping.abbreviation or f'S{channel + 1:02d}'} EMG 波形", self.resetLivePlotView)
            color = QColor(COLORS[channel % len(COLORS)])
            fill = QColor(color)
            fill.setAlpha(56)
            curve = plot.plot(
                pen=pg.mkPen(color, width=1), fillLevel=0, brush=pg.mkBrush(fill),
            )
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            tokens = FluentThemeManager.DARK if QApplication.instance().styleHints().colorScheme().name == "Dark" else FluentThemeManager.LIGHT
            stats = pg.TextItem(color=tokens["text_secondary"], anchor=(1, 1))
            stats.setZValue(10)
            plot.addItem(stats)
            self.curves[channel] = (plot, curve, stats)
        self.refreshPlots()

    def refreshPlots(self) -> None:
        if not hasattr(self, "plotWidget"):
            return
        duration, count = 10, int(10 * self.currentSampleRate)
        for channel, (plot, curve, stats) in self.curves.items():
            values = np.asarray(list(self.histories[channel])[-count:], dtype=np.float32) * 1000
            xAxis = (np.arange(len(values)) - len(values) + 1) / self.currentSampleRate
            limit = 1.0
            if len(values):
                limit = max(0.05, min(20, float(np.percentile(np.abs(values), 99.5)) * 1.25))
            plot.setXRange(-duration, 0, padding=0)
            plot.setYRange(-limit, limit, padding=0)
            curve.setData(xAxis, values)
            if len(values):
                amplitudes = np.abs(values)
                mean_amplitude = float(np.mean(amplitudes))
                max_amplitude = float(np.max(amplitudes))
                stats.setText(f"平均絕對振幅：{mean_amplitude:.3f} mV\n最大絕對振幅：{max_amplitude:.3f} mV")
                stats.setPos(-0.15, limit * 0.94)
            else:
                stats.setText("等待 EMG 樣本")
                stats.setPos(-0.15, limit * 0.94)

    def pedalReady(self) -> None:
        self.pedalBluetoothReady = True
        self.startInitialBluetoothScan()

    def trainerReady(self) -> None:
        self.trainerBluetoothReady = True
        self.startInitialBluetoothScan()

    # Final scan-assignment flow: scan feedback belongs in the event log, while
    # the connection panel only presents connected/disconnected state.
    def scanCyclingDevices(self) -> None:
        if self.bluetoothScanWorker and self.bluetoothScanWorker.isRunning():
            return
        self.pedalScanInProgress = True
        self.scanTrainerButton.setEnabled(False)
        self.scanPedalButton.setEnabled(False)
        self.appendLog("Bluetooth scan started")
        self.bluetoothScanWorker = CyclingDeviceScanWorker()
        self.bluetoothScanWorker.devicesFound.connect(self.showCyclingDevices)
        self.bluetoothScanWorker.failed.connect(self.cyclingScanFailed)
        self.bluetoothScanWorker.finished.connect(self.finishCyclingScan)
        self.bluetoothScanWorker.start()

    def showCyclingDevices(self, devices: list[tuple[str, str]]) -> None:
        self.showScanResults(devices)
        self.appendLog(f"Bluetooth scan | {len(devices)} devices found")

    def showScanResults(self, devices: list[tuple[str, str]]) -> None:
        if not hasattr(self, "scanResultsLayout"):
            return
        while self.scanResultsLayout.count():
            item = self.scanResultsLayout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()
        if not devices:
            self.scanResultsLayout.addWidget(QLabel("未找到藍牙裝置", objectName="muted"))
            self.scanResultsLayout.addStretch()
            return
        self.scanResultButtons: list[tuple[str, QPushButton]] = []
        for address, name in sorted(devices, key=lambda item: item[1].casefold()):
            button = QPushButton(f"{name or 'Unknown device'}  ·  {address}")
            button.setCheckable(True)
            button.setToolTip("點選此裝置，再在上方設定為阻力計、踏板或 攝影機")
            button.setFixedWidth(500)
            FluentThemeManager.set_property(button, "fluentRole", "scanResult")
            button.clicked.connect(
                lambda _checked=False, deviceAddress=address, deviceName=name: self.selectScannedDevice(
                    deviceAddress, deviceName
                )
            )
            self.scanResultsLayout.addWidget(button, 0, Qt.AlignLeft)
            self.scanResultButtons.append((address, button))
        self.scanResultsLayout.addStretch()

    def selectScannedDevice(self, address: str, name: str) -> None:
        self.selectedScannedDevice = (address, name)
        for buttonAddress, button in getattr(self, "scanResultButtons", []):
            selected = buttonAddress == address
            button.setChecked(selected)
            FluentThemeManager.set_property(button, "fluentSelected", "true" if selected else "false")
        label = f"{name or 'Unknown device'} · {address}"
        self.selectedScannedDeviceLabel.setText(label)
        self.assignTrainerButton.setEnabled(True)
        self.assignPedalButton.setEnabled(True)
        self.assignGoProButton.setEnabled(True)
        self.appendLog(f"Device selected | {label}")

    def assignSelectedScannedDevice(self, role: str) -> None:
        if self.selectedScannedDevice is None:
            return
        address, name = self.selectedScannedDevice
        label = f"{name or 'Unknown device'} · {address}"
        if role == "阻力計":
            self.trainerDevices.clear()
            self.trainerDevices.addItem(label, address)
            self.trainerDevices.setCurrentIndex(0)
        elif role == "踏板":
            if name.strip().upper().endswith(" R"):
                self.appendLog(f"Device assignment skipped | right pedal: {label}")
                return
            self.pedalDevices.clear()
            self.pedalDevices.addItem(label, address)
            self.pedalDevices.setCurrentIndex(0)
        elif role == "攝影機":
            self.goproAddress = address
            suffix = name.rsplit(" ", 1)[-1]
            self.goproIdentifier = suffix if suffix.isdigit() else ""
            self.settings.setValue("gopro/address", address)
            self.settings.setValue("gopro/identifier", self.goproIdentifier)
            self.setGoProConnectionState(False)
            self.goproStatusLabel.setText(f"已選擇：{label}；可按「測試攝影機連線」")
        self.appendLog(f"Device assignment | {role}: {label}")

    def closeEvent(self, event) -> None:  # noqa: N802
        # Every QThread must finish before its Python/Qt owner is destroyed.
        # In particular, a shared BLE scan can still be running during the
        # first few seconds after the application opens.
        for worker in (self.angleAnalysisWorker, self.angleExportWorker):
            if worker is not None and worker.isRunning():
                event.ignore()
                QTimer.singleShot(250, self.close)
                return
        if any(worker.isRunning() for worker in self.archiveWorkers):
            event.ignore()
            QTimer.singleShot(250, self.close)
            return
        if self.bluetoothScanWorker and self.bluetoothScanWorker.isRunning():
            self.bluetoothScanWorker.stop()
            if not self.bluetoothScanWorker.wait(3000):
                self.appendLog("Bluetooth scan is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return

        if self.testWorker and self.testWorker.isRunning():
            # ConnectionWorker uses a bounded socket timeout.  Waiting here
            # prevents a connection test from being destroyed mid-request.
            if not self.testWorker.wait(5000):
                self.appendLog("EMG connection test is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return

        if self.streamWorker and self.streamWorker.isRunning():
            self.streamWorker.request_stop()
            if not self.streamWorker.wait(6000):
                self.appendLog("EMG collection is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        if self.goproWorker and self.goproWorker.isRunning():
            self.goproWorker.request_disconnect()
            if not self.goproWorker.wait(5000):
                self.appendLog("攝影機 Bluetooth session is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        if self.goproDownloadWorker and self.goproDownloadWorker.isRunning():
            self.appendLog("攝影機 video download is still running; close delayed")
            event.ignore()
            QTimer.singleShot(250, self.close)
            return
        if self.goproMediaListWorker and self.goproMediaListWorker.isRunning():
            self.appendLog("攝影機 media list is still loading; close delayed")
            event.ignore()
            QTimer.singleShot(250, self.close)
            return
        if self.goproWifiWorker and self.goproWifiWorker.isRunning():
            self.appendLog("攝影機 Wi-Fi connection is still running; close delayed")
            event.ignore()
            QTimer.singleShot(250, self.close)
            return
        if self.bikeOnlySession:
            self.finishBikeOnlySession()
        if self.trainerWorker.isRunning():
            self.trainerWorker.stopWorker()
            if not self.trainerWorker.wait(6000):
                self.appendLog("Resistance-meter worker is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        if self.pedalWorker.isRunning():
            self.pedalWorker.stopWorker()
            if not self.pedalWorker.wait(6000):
                self.appendLog("Pedal worker is still stopping; close delayed")
                event.ignore()
                QTimer.singleShot(150, self.close)
                return
        for writer in tuple(self.databaseWriters):
            if writer.isRunning():
                writer.enqueue("stop")
                if not writer.wait(5000):
                    self.appendLog("Database writer is still stopping; close delayed")
                    event.ignore()
                    QTimer.singleShot(150, self.close)
                    return
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = EmgRehabilitationWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
