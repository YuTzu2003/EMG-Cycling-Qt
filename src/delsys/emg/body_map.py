from __future__ import annotations
from collections.abc import Callable
from PySide6.QtCore import QByteArray, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QTransform
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QButtonGroup,QFrame,QGraphicsPathItem,QGraphicsScene,QGraphicsView,QHBoxLayout,QLabel,QPushButton,QVBoxLayout,QWidget,)
from delsys.emg.anatomy_svg_data import ANATOMY_SVG
from delsys.emg.sensor_mapping import SensorMapping

MUSCLE_ALIASES = {
    "biceps femoris": "biceps-femoris",
    "hamstring": "biceps-femoris",
    "vastus medialis": "vastus-medialis",
    "rectus femoris": "rectus-femoris",
    "vastus lateralis": "vastus-lateralis",
    "tibialis anterior": "tibialis-anterior",
    "gastrocnemius": "gastrocnemius",
    "soleus": "soleus",
}

MUSCLE_LABELS = {
    "biceps-femoris": "股二頭肌",
    "vastus-medialis": "股內側肌",
    "rectus-femoris": "股直肌",
    "vastus-lateralis": "股外側肌",
    "tibialis-anterior": "脛前肌",
    "gastrocnemius": "腓腸肌",
    "soleus": "比目魚肌",
}

ANATOMY_COLORS = {
    "hair": "#695a58",
    "head": "#ead7d1",
    "neck": "#d7a59d",
    "chest": "#d6958c",
    "abs": "#d8aaa2",
    "obliques": "#ca8d85",
    "biceps": "#d5a099",
    "triceps": "#c9867f",
    "deltoids": "#c77d76",
    "trapezius": "#b96f69",
    "upper-back": "#c7857d",
    "lower-back": "#d29b93",
    "forearm": "#d9aaa3",
    "hands": "#e0bbb4",
    "adductors": "#c98a82",
    "gluteal": "#c77c75",
    "quadriceps": "#d4968e",
    "hamstring": "#ce8981",
    "knees": "#e5c2bb",
    "tibialis": "#cd8e86",
    "calves": "#cc8981",
    "ankles": "#deb7b0",
    "feet": "#dfb9b2",
}

BODY_RECT = QRectF(0, 0, 118, 236)

# Click targets use the matched SVG model's local 724 x 1448 view box.
# The posterior x coordinates have already been shifted left by 724.
HIT_ZONES = {
    ("rectus-femoris", "right"): ("front", QRectF(270, 680, 42, 260)),
    ("vastus-lateralis", "right"): ("front", QRectF(235, 720, 36, 230)),
    ("vastus-medialis", "right"): ("front", QRectF(310, 745, 31, 210)),
    ("rectus-femoris", "left"): ("front", QRectF(414, 680, 42, 260)),
    ("vastus-lateralis", "left"): ("front", QRectF(456, 720, 36, 230)),
    ("vastus-medialis", "left"): ("front", QRectF(383, 745, 31, 210)),
    ("tibialis-anterior", "right"): ("front", QRectF(242, 970, 62, 270)),
    ("tibialis-anterior", "left"): ("front", QRectF(420, 970, 62, 270)),
    # Posterior targets intentionally cover only the highlighted muscle path,
    # rather than the whole hamstring/calf group.
    ("biceps-femoris", "left"): ("back", QRectF(232, 735, 42, 270)),
    ("biceps-femoris", "right"): ("back", QRectF(462, 735, 38, 270)),
    ("gastrocnemius", "left"): ("back", QRectF(242, 970, 47, 190)),
    ("gastrocnemius", "right"): ("back", QRectF(435, 970, 48, 190)),
    ("soleus", "left"): ("back", QRectF(250, 1155, 39, 145)),
    ("soleus", "right"): ("back", QRectF(440, 1155, 39, 145)),
}


def mappingRegion(mapping: SensorMapping) -> tuple[str, str] | None:
    name = " ".join(mapping.english_name.casefold().replace("_", " ").split())
    muscle = next((key for alias, key in MUSCLE_ALIASES.items() if alias in name), None)
    if muscle is None:
        return None

    abbreviation = mapping.abbreviation.upper().strip()
    side = (
        "right"
        if abbreviation.endswith("-R")
        else "left"
        if abbreviation.endswith("-L")
        else ""
    )
    if not side:
        chinese = mapping.chinese_name.strip()
        side = (
            "right"
            if chinese.startswith("右")
            else "left"
            if chinese.startswith("左")
            else ""
        )
    return (muscle, side) if side else None

def svgPartRegion(view: str,slug: str,sourceSide: str,index: int,) -> tuple[str, str] | None:
    if view == "front":
        side = "right" if sourceSide == "left" else "left"
        if slug == "quadriceps":
            muscles = ("rectus-femoris", "vastus-lateralis", "vastus-medialis")
            return (muscles[index], side) if index < len(muscles) else None
        if slug == "tibialis":
            return ("tibialis-anterior", side)
        return None

    # In the posterior view, visual and anatomical left/right are aligned.
    side = sourceSide
    if slug == "hamstring":
        # The source separates the posterior thigh into four paths.  EMG slot
        # labels in this project are specifically biceps femoris, so colour
        # only its lateral path instead of the complete hamstring group.
        bicepsIndex = 0 if side == "left" else 3
        return ("biceps-femoris", side) if index == bicepsIndex else None
    if slug == "calves":
        # Use one distinct path for each named muscle.  This prevents one
        # sensor selection from painting both heads/the whole lower leg.
        regions = {
            "left": {0: "gastrocnemius", 1: "soleus"},
            "right": {0: "gastrocnemius", 2: "soleus"},
        }
        muscle = regions.get(side, {}).get(index)
        return (muscle, side) if muscle is not None else None
    return None


class ClickableMuscleItem(QGraphicsPathItem):
    def __init__(self, path: QPainterPath, onClick: Callable[[], None]) -> None:
        super().__init__(path)
        self.onClick = onClick
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(QColor(255, 255, 255, 1)))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.onClick()
            event.accept()
            return
        super().mousePressEvent(event)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self.setBrush(QBrush(QColor(255, 255, 255, 45)))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self.setBrush(QBrush(QColor(255, 255, 255, 1)))
        super().hoverLeaveEvent(event)


class BodyGraphicsView(QGraphicsView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet("background: transparent; border: 0;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.scene() is not None:
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)


class BodyMapPanel(QFrame):
    slotToggled = Signal(int)

    def __init__(self, colors: tuple[str, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent, objectName="panel")
        self.colors = colors
        self.currentView = "front"
        self.mappings: list[SensorMapping] = []
        self.selectedSlots: set[int] = set()
        self.activeSlots: set[int] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        layout.addWidget(QLabel("肌群定位", objectName="sectionTitle"))

        switch = QHBoxLayout()
        switch.setSpacing(0)
        self.frontButton = QPushButton("正面 Anterior")
        self.backButton = QPushButton("背面 Posterior")
        for button in (self.frontButton, self.backButton):
            button.setCheckable(True)
            button.setObjectName("bodyViewButton")
            switch.addWidget(button)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.frontButton)
        group.addButton(self.backButton)
        self.frontButton.setChecked(True)
        self.frontButton.clicked.connect(lambda: self.setView("front"))
        self.backButton.clicked.connect(lambda: self.setView("back"))
        layout.addLayout(switch)

        self.contentLayout = QHBoxLayout()
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.setSpacing(6)
        self.view = BodyGraphicsView()
        self.view.setMinimumHeight(440)
        self.contentLayout.addWidget(self.view, 1)
        layout.addLayout(self.contentLayout, 1)
        self.rebuildScene()

    def setLegendWidget(self, widget: QWidget) -> None:
        """Embed the collection sensor legend beside the anatomy diagram."""
        widget.setFixedWidth(180)
        self.contentLayout.insertWidget(0, widget)

    def setView(self, view: str) -> None:
        if view not in {"front", "back"}:
            return
        self.currentView = view
        self.frontButton.setChecked(view == "front")
        self.backButton.setChecked(view == "back")
        self.rebuildScene()

    def setMappings(self,mappings: list[SensorMapping],selectedSlots: set[int],activeSlots: set[int],) -> None:
        self.mappings = mappings
        self.selectedSlots = selectedSlots
        self.activeSlots = activeSlots
        self.rebuildScene()

    def rebuildScene(self) -> None:
        previous = self.view.scene()
        scene = QGraphicsScene(self)
        # Reserve space above the anatomy diagram for the L/R orientation labels.
        scene.setSceneRect(-2, -22, 122, 265)
        mapped = {
            region: mapping
            for mapping in self.mappings
            if (region := mappingRegion(mapping)) is not None
        }
        anatomy = ANATOMY_SVG[self.currentView]
        _, _, viewWidth, viewHeight = anatomy["viewBox"]
        transform = QTransform(BODY_RECT.width() / viewWidth,0,0,BODY_RECT.height() / viewHeight,BODY_RECT.left(),BODY_RECT.top(),)
        svg = self._buildSvg(mapped)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")), scene)
        svgItem = QGraphicsSvgItem()
        svgItem.setSharedRenderer(renderer)
        svgItem.setTransform(transform)
        svgItem.setZValue(1)
        scene.addItem(svgItem)

        for region, (view, sourceRect) in HIT_ZONES.items():
            if view != self.currentView or region not in mapped:
                continue
            mapping = mapped[region]
            localRect = sourceRect
            zonePath = QPainterPath()
            zonePath.addRoundedRect(localRect, 8, 8)
            zonePath = transform.map(zonePath)
            self._addClickZone(scene, zonePath, region, mapping)
            center = zonePath.boundingRect().center()
            self._addSlotBadge(scene, mapping.slot, center.x(), center.y())

        self._addOrientationLabels(scene)
        self.view.setScene(scene)
        self.view.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)
        if previous is not None:
            previous.deleteLater()

    def _buildSvg(self, mapped: dict[tuple[str, str], SensorMapping]) -> str:
        anatomy = ANATOMY_SVG[self.currentView]
        x, y, width, height = anatomy["viewBox"]
        elements = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {width} {height}" 'f'width="{width}" height="{height}">']
        for part in anatomy["parts"]:
            slug = str(part["slug"])
            # The upstream posterior head contains offset facial fragments.
            # Replace it below with the matching anterior head geometry.
            if self.currentView == "back" and slug in {"head", "hair"}:
                continue
            side = str(part["side"])
            index = int(part["index"])
            region = svgPartRegion(self.currentView, slug, side, index)
            mapping = mapped.get(region) if region is not None else None
            base = QColor(ANATOMY_COLORS.get(slug, "#d6aaa3"))
            selected = mapping is not None and mapping.slot in self.selectedSlots
            active = mapping is not None and mapping.slot in self.activeSlots
            fill = (
                self.colors[(mapping.slot - 1) % len(self.colors)]
                if mapping is not None and selected
                else base.name()
            )
            stroke = "#16a34a" if active else "#ffffff"
            strokeWidth = 5 if active else 1.6
            elements.append(f'<path d="{part["path"]}" fill="{fill}" stroke="{stroke}" 'f'stroke-width="{strokeWidth}" stroke-linejoin="round"/>')
        if self.currentView == "back":
            # The source posterior head includes front-facing facial details.
            # Draw a dedicated rear-head silhouette instead: a rounded crown,
            # subtle hair layer, visible ears, and a smooth nape transition.
            elements.extend(
                [
                    '<ellipse cx="1037" cy="202" rx="5" ry="13" '
                    'fill="#ead7d1" stroke="#8f6f6b" stroke-width="2"/>',
                    '<ellipse cx="1135" cy="202" rx="5" ry="13" '
                    'fill="#ead7d1" stroke="#8f6f6b" stroke-width="2"/>',
                    '<path d="M1034 207 C1031 168 1041 122 1064 104 '
                    'C1077 94 1101 94 1114 104 C1137 122 1147 168 '
                    '1138 207 L1132 228 C1127 244 1109 253 1086 255 '
                    'C1063 253 1045 244 1040 228 Z" fill="#695a58" '
                    'stroke="#8f6f6b" stroke-width="2" stroke-linejoin="round"/>',
                    '<path d="M1053 130 C1063 116 1075 111 1086 111 '
                    'C1098 111 1110 116 1120 130" fill="none" '
                    'stroke="#8c7974" stroke-width="3" stroke-linecap="round" '
                    'opacity="0.55"/>',
                ]
            )
        elements.append(f'<path d="{anatomy["outline"]}" fill="none" stroke="#8f6f6b" ''stroke-width="2.2" stroke-linejoin="round"/>')
        elements.append("</svg>")
        return "".join(elements)

    def _addClickZone(self,scene: QGraphicsScene,path: QPainterPath,region: tuple[str, str],mapping: SensorMapping,) -> None:
        item = ClickableMuscleItem(path,lambda slot=mapping.slot: self.slotToggled.emit(slot),)
        side = "右" if region[1] == "right" else "左"
        status = "已啟用" if mapping.slot in self.activeSlots else "未連線"
        item.setToolTip(f"S{mapping.slot:02d}｜{side}{MUSCLE_LABELS[region[0]]}\n"f"{mapping.english_name}｜{status}\n點擊切換即時波形")
        item.setZValue(20)
        scene.addItem(item)

    def _addSlotBadge(self, scene: QGraphicsScene, slot: int, x: float, y: float) -> None:
        selected = slot in self.selectedSlots
        badge = scene.addEllipse(QRectF(x - 3.1, y - 3.1, 6.2, 6.2),QPen(QColor("#ffffff"), 0.45),QBrush(QColor("#0f172a" if selected else "#64748b")),)
        badge.setAcceptedMouseButtons(Qt.NoButton)
        badge.setZValue(30)
        label = scene.addText(str(slot))
        label.setDefaultTextColor(QColor("#ffffff"))
        font = label.font()
        font.setPointSizeF(3.2 if slot < 10 else 2.8)
        font.setBold(True)
        label.setFont(font)
        bounds = label.boundingRect()
        label.setPos(x - bounds.width() / 2, y - bounds.height() / 2 - 0.2)
        label.setAcceptedMouseButtons(Qt.NoButton)
        label.setZValue(31)

    def _addOrientationLabels(self, scene: QGraphicsScene) -> None:
        leftText, rightText = ("R", "L") if self.currentView == "front" else ("L", "R")
        for text, x in ((leftText, BODY_RECT.left()), (rightText, BODY_RECT.right() - 5)):
            label = scene.addText(text)
            label.setDefaultTextColor(QColor("#475569"))
            font = label.font()
            font.setPointSizeF(5.5)
            font.setBold(True)
            label.setFont(font)
            label.setPos(x, BODY_RECT.top() - 16)
            label.setAcceptedMouseButtons(Qt.NoButton)
            label.setZValue(32)
