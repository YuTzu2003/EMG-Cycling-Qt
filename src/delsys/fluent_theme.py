from __future__ import annotations
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

class FluentThemeManager(QObject):
    themeChanged = Signal()

    LIGHT = {
        "window_background": "#eef2f6", "surface_background": "#ffffff",
        "surface_secondary": "#f7f9fb", "text_primary": "#242424",
        "text_secondary": "#616161", "text_disabled": "#a6a6a6",
        "border_primary": "#d1d1d1", "focus_border": "#0f6cbd",
        "brand_background": "#0f6cbd", "brand_hover": "#115ea3",
        "brand_pressed": "#0c3b5e", "brand_foreground": "#ffffff",
        "selection_background": "#cfe4fa", "selected_surface": "#e5e7eb", "info": "#0f6cbd", "info_surface": "#e8f2fc", "success": "#107c10",
        "success_surface": "#dff6dd", "warning": "#8a6100",
        "warning_surface": "#fff4ce", "danger": "#c50f1f", "danger_surface": "#fde7e9",
    }
    DARK = {
        "window_background": "#1f1f1f", "surface_background": "#292929",
        "surface_secondary": "#242424", "text_primary": "#ffffff",
        "text_secondary": "#c7c7c7", "text_disabled": "#8a8a8a",
        "border_primary": "#5a5a5a", "focus_border": "#479ef5",
        "brand_background": "#479ef5", "brand_hover": "#62abf5",
        "brand_pressed": "#2886de", "brand_foreground": "#0f0f0f",
        "selection_background": "#1a4f78", "selected_surface": "#3a3a3a", "info": "#479ef5", "info_surface": "#173a5e", "success": "#6ccb5f",
        "success_surface": "#163d18", "warning": "#f9c74f",
        "warning_surface": "#4c3b0a", "danger": "#ff99a4", "danger_surface": "#4c1d25",
    }

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.app = app
        self.tokens: dict[str, str] = {}

    @staticmethod
    def set_property(widget: QWidget, name: str, value: object) -> None:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def apply(self) -> None:
        dark = self.app.styleHints().colorScheme().name == "Dark"
        self.tokens = dict(self.DARK if dark else self.LIGHT)
        t = self.tokens
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(t["window_background"]))
        palette.setColor(QPalette.ColorRole.Base, QColor(t["surface_background"]))
        palette.setColor(QPalette.ColorRole.Button, QColor(t["surface_background"]))
        palette.setColor(QPalette.ColorRole.Text, QColor(t["text_primary"]))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(t["text_primary"]))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(t["text_primary"]))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(t["brand_background"]))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(t["brand_foreground"]))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(t["surface_background"]))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(t["text_primary"]))
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t["text_disabled"]))
        self.app.setPalette(palette)
        self.app.setStyleSheet(self._qss(t))
        self.themeChanged.emit()

    @staticmethod
    def _qss(t: dict[str, str]) -> str:
        return f"""
        QWidget {{ color: {t['text_primary']}; font-family: 'Segoe UI'; font-size: 12px; }}
        QToolTip {{ background: {t['surface_background']}; color: {t['text_primary']}; border: 1px solid {t['border_primary']}; padding: 4px 6px; }}
        QMainWindow, QWidget#root {{ background: {t['window_background']}; }}
        QTabWidget::pane {{ background: {t['window_background']}; border: 1px solid {t['border_primary']}; border-radius: 6px; top: -1px; }}
        QLabel {{ background: transparent; }}
        QFrame[fluentRole="surface"], QFrame#panel {{ background: {t['surface_background']}; border: 1px solid {t['border_primary']}; border-radius: 6px; }}
        QFrame[fluentRole="scanResults"] {{ background: {t['surface_secondary']}; border: 1px solid {t['border_primary']}; border-radius: 4px; }}
        QLabel#title {{ font-size: 20px; font-weight: 600; }} QLabel#sectionTitle {{ font-size: 14px; font-weight: 600; }} QLabel#muted {{ color: {t['text_secondary']}; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTableWidget {{ background: {t['surface_background']}; border: 1px solid {t['border_primary']}; border-radius: 4px; padding: 4px 6px; }}
        QTableWidget::item {{ padding: 5px; }}
        QTableWidget::item:selected {{ background: {t['selection_background']}; color: {t['text_primary']}; }}
        QHeaderView::section {{ background: {t['surface_secondary']}; color: {t['text_primary']}; border: 0; border-bottom: 1px solid {t['border_primary']}; padding: 7px; font-weight: 600; }}
        QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {t['text_secondary']}; }}
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTableWidget:focus, QPushButton:focus, QCheckBox:focus {{ border: 2px solid {t['focus_border']}; }}
        QPushButton {{ background: {t['surface_background']}; border: 1px solid {t['border_primary']}; border-radius: 4px; padding: 4px 8px; min-height: 16px; }}
        QPushButton:hover {{ background: {t['surface_secondary']}; }} QPushButton:pressed {{ background: {t['selection_background']}; }}
        QPushButton[fluentAppearance="primary"], QPushButton#primary {{ background: {t['brand_background']}; color: {t['brand_foreground']}; border-color: {t['brand_background']}; font-weight: 600; }}
        QPushButton[fluentAppearance="primary"]:hover, QPushButton#primary:hover {{ background: {t['brand_hover']}; }}
        QPushButton[fluentAppearance="dark"] {{ background: {t['text_primary']}; color: {t['surface_background']}; border-color: {t['text_primary']}; font-weight: 600; }}
        QPushButton[fluentAppearance="dark"]:hover {{ background: {t['text_secondary']}; }}
        QPushButton[fluentAppearance="success"], QPushButton#record {{ background: {t['success']}; color: {t['brand_foreground']}; border-color: {t['success']}; font-weight: 600; }}
        QPushButton[fluentAppearance="danger"] {{ background: {t['danger']}; color: {t['brand_foreground']}; border-color: {t['danger']}; font-weight: 600; }}
        QPushButton[fluentAppearance="warning"] {{ background: {t['warning']}; color: {t['brand_foreground']}; border-color: {t['warning']}; font-weight: 600; }}
        QPushButton[fluentRole="scanResult"] {{ text-align: left; background: transparent; border: 0; padding: 4px 6px; }}
        QPushButton[fluentRole="scanResult"]:hover, QPushButton[fluentRole="scanResult"][fluentSelected="true"] {{ background: {t['selected_surface']}; border: 1px solid {t['border_primary']}; }}
        QPushButton:disabled {{ color: {t['text_disabled']}; background: {t['surface_secondary']}; border-color: {t['border_primary']}; }}
        QCheckBox {{ spacing: 6px; }}
        QTabBar::tab {{ padding: 7px 12px; color: {t['text_primary']}; border: 1px solid transparent; }}
        QTabBar::tab:hover {{ background: {t['surface_secondary']}; }}
        QTabBar::tab:selected {{ color: {t['text_primary']}; background: {t['surface_background']}; border: 1px solid {t['border_primary']}; border-bottom: 2px solid {t['text_primary']}; font-weight: 600; }}
        QFrame#metricCard {{ background: {t['window_background']}; border: 1px solid {t['border_primary']}; border-radius: 6px; }}
        QLabel#metricValue {{ font-size: 20px; font-weight: 600; }} QLabel#metricTitle, QLabel#metricUnit {{ color: {t['text_secondary']}; }}
        QLabel#countdown {{ font-size: 15px; font-weight: 700; }}
        QLabel#recordingIndicator {{ color: {t['danger']}; font-size: 15px; font-weight: 700; }}
        QLabel#status {{ padding: 3px 6px; border-radius: 4px; font-weight: 600; }}
        QLabel#connectionIndicator {{ padding: 3px 7px; border: 1px solid transparent; border-radius: 7px; font-size: 11px; font-weight: 600; }}
        QLabel[fluentSeverity="info"] {{ color: {t['info']}; }} QLabel[fluentSeverity="success"] {{ color: {t['success']}; }} QLabel[fluentSeverity="warning"] {{ color: {t['warning']}; }} QLabel[fluentSeverity="danger"] {{ color: {t['danger']}; }}
        QLabel#status[fluentSeverity="info"], QLabel#connectionIndicator[fluentSeverity="info"] {{ background: {t['info_surface']}; }}
        QLabel#status[fluentSeverity="success"] {{ background: {t['success_surface']}; }}
        QLabel#connectionIndicator[fluentSeverity="success"] {{ background: {t['success_surface']}; border-color: {t['success']}; }}
        QLabel#status[fluentSeverity="warning"], QLabel#connectionIndicator[fluentSeverity="warning"] {{ background: {t['warning_surface']}; }}
        QLabel#status[fluentSeverity="danger"] {{ background: {t['danger_surface']}; }}
        QLabel#connectionIndicator[fluentSeverity="danger"] {{ background: {t['danger_surface']}; border-color: {t['danger']}; }}
        QFrame#sensorRow[emgStatus="normal"], QFrame#sensorRow[emgStatus="unknown"] {{ background: {t['surface_background']}; border: 1px solid {t['border_primary']}; }}
        QFrame#sensorRow[emgStatus="warning"] {{ background: {t['warning_surface']}; border: 1px solid {t['warning']}; }}
        QFrame#sensorRow[emgStatus="focus"] {{ background: {t['danger_surface']}; border: 1px solid {t['danger']}; }}
        QLabel#emgMetrics {{ color: {t['text_secondary']}; font-size: 11px; }}
        QLabel#emgMuscleStatus {{ font-weight: 600; }}
        QFrame#eventLogPanel {{ background: #111827; border: 1px solid #374151; border-radius: 0px; }}
        QFrame#eventLogPanel QLabel {{ color: #f3f4f6; }}
        QPlainTextEdit#eventLog {{ background: transparent; color: #d1d5db; border: 0; padding: 3px; font-family: Consolas, monospace; }}
        """
