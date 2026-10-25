"""The workflow canvas: nodes on a dotted grid, joined by curved links.

The trigger sits on the left with a bolt; each step is a rounded tile with its icon,
name and a short description underneath. Drag a node to move it, drag from a node's
right-hand dot to another node to link them (or let go on empty canvas to add a node
there), click the + at the end of an open output to add the next step, hover a link and
click its ✕ to remove it. Drag empty canvas to pan, scroll to zoom.

The canvas only shows the workflow and reports what you did (signals on
:class:`FlowScene`); the builder changes the workflow and redraws.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QLineF, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from jarvis.ui import theme
from jarvis.ui.node_icons import paint_icon

NODE = 92.0  # a node's tile is NODE x NODE
GRID = 20.0
PORT_R = 6.0
STUB = 44.0  # length of the dashed line to an open output's "+"
LABEL_W = 168.0

_BODY_TOP = QColor(22, 34, 50)
_BODY_BOTTOM = QColor(11, 18, 28)
_LINK = QColor(150, 176, 196, 170)


@dataclass
class NodeView:
    """What the canvas draws for one node."""

    id: str
    title: str
    subtitle: str
    icon: str
    accent: QColor
    outputs: tuple[str, ...] = ("out",)
    number: int = 0  # shown in the corner, for {step N}
    trigger: bool = False
    x: float = 0.0
    y: float = 0.0


def snap(value: float) -> float:
    return round(value / GRID) * GRID


def _port_y(outputs: tuple[str, ...], port: str) -> float:
    if len(outputs) <= 1 or port not in outputs:
        return NODE / 2
    step = NODE / (len(outputs) + 1)
    return step * (outputs.index(port) + 1)


def link_path(start: QPointF, end: QPointF) -> QPainterPath:
    """A smooth S-curve from an output to an input (looping round when it goes back)."""
    path = QPainterPath(start)
    dx = end.x() - start.x()
    if dx >= 40:
        pull = max(50.0, dx * 0.5)
        path.cubicTo(start + QPointF(pull, 0), end - QPointF(pull, 0), end)
        return path
    # Backwards: out to the right, round below, and in from the left.
    drop = max(abs(end.y() - start.y()) / 2, NODE * 0.9)
    mid_y = max(start.y(), end.y()) + drop if end.y() >= start.y() else start.y() + drop
    path.cubicTo(start + QPointF(70, 0), QPointF(start.x() + 70, mid_y), QPointF(start.x(), mid_y))
    path.lineTo(end.x(), mid_y)
    path.cubicTo(QPointF(end.x() - 70, mid_y), end - QPointF(70, 0), end)
    return path


class NodeItem(QGraphicsObject):
    def __init__(self, view: NodeView) -> None:
        super().__init__()
        self.view = view
        self.node_id = view.id
        self.status = ""  # running | done | failed | skipped
        self.note = ""
        self.phase = 0.0
        self.linked_ports: set[str] = set()
        self._hover_port = ""
        self._hover_plus = ""
        self._press_pos = QPointF()
        self._wiring = ""
        self.setPos(view.x, view.y)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setZValue(2)
        self.setToolTip(view.subtitle)

    # --- geometry ---

    def boundingRect(self) -> QRectF:  # noqa: N802
        left = min(-28.0, NODE / 2 - LABEL_W / 2)
        return QRectF(left, -16, NODE + STUB + 30 - left, NODE + 66)

    def body(self) -> QPainterPath:
        path = QPainterPath()
        if self.view.trigger:
            r, k = NODE / 2, 12.0
            path.moveTo(r, 0)
            path.lineTo(NODE - k, 0)
            path.quadTo(NODE, 0, NODE, k)
            path.lineTo(NODE, NODE - k)
            path.quadTo(NODE, NODE, NODE - k, NODE)
            path.lineTo(r, NODE)
            path.arcTo(QRectF(0, 0, NODE, NODE), 270, -180)
            path.closeSubpath()
        else:
            path.addRoundedRect(QRectF(0, 0, NODE, NODE), 14, 14)
        return path

    def shape(self) -> QPainterPath:
        path = self.body()
        path.addRect(QRectF(NODE / 2 - LABEL_W / 2, NODE + 4, LABEL_W, 44))
        for port in self.view.outputs:
            path.addEllipse(self.port_point(port), PORT_R + 4, PORT_R + 4)
            if port not in self.linked_ports:
                path.addRect(self.plus_rect(port))
        return path

    def port_point(self, port: str) -> QPointF:
        return QPointF(NODE, _port_y(self.view.outputs, port))

    def plus_rect(self, port: str) -> QRectF:
        y = _port_y(self.view.outputs, port)
        return QRectF(NODE + STUB, y - 11, 22, 22)

    def input_scene(self) -> QPointF:
        return self.mapToScene(QPointF(0, NODE / 2))

    def output_scene(self, port: str) -> QPointF:
        return self.mapToScene(self.port_point(port))

    def _port_at(self, pos: QPointF) -> str:
        for port in self.view.outputs:
            if QLineF(pos, self.port_point(port)).length() <= PORT_R + 5:
                return port
        return ""

    def _plus_at(self, pos: QPointF) -> str:
        for port in self.view.outputs:
            if port not in self.linked_ports and self.plus_rect(port).adjusted(
                -3, -3, 3, 3
            ).contains(pos):
                return port
        return ""

    # --- painting ---

    def paint(
        self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = self.view.accent
        body = self.body()
        selected = self.isSelected()
        edge = QColor(accent)
        if self.status == "failed":
            edge = QColor(theme.DANGER)
        elif self.status == "running":
            edge = QColor(theme.CYAN)
        elif self.status == "done":
            edge = QColor(theme.ELECTRIC)

        # Glow: a few soft outlines (no blur effects, cheap at any zoom).
        glow = theme.CYAN if selected else edge if self.status in ("running", "failed") else None
        if glow is not None:
            pulse = 0.6 + 0.4 * math.sin(self.phase) if self.status == "running" else 1.0
            for width, alpha in ((14, 18), (9, 30), (5, 55)):
                painter.setPen(QPen(theme.rgba(glow, int(alpha * pulse)), width))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(body)

        fill = QLinearGradient(0, 0, 0, NODE)
        fill.setColorAt(0, _BODY_TOP)
        fill.setColorAt(1, _BODY_BOTTOM)
        painter.setBrush(QBrush(fill))
        alpha = 255 if selected or self.status else 150
        painter.setPen(
            QPen(theme.rgba(theme.CYAN if selected else edge, alpha), 2 if selected else 1.5)
        )
        painter.drawPath(body)

        # A thin accent sheen along the top edge.
        painter.save()
        painter.setClipPath(body)
        sheen = QLinearGradient(0, 0, 0, 16)
        sheen.setColorAt(0, theme.rgba(accent, 60))
        sheen.setColorAt(1, theme.rgba(accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(sheen))
        painter.drawRect(QRectF(0, 0, NODE, 16))
        painter.restore()

        paint_icon(
            painter, self.view.icon, QRectF(NODE / 2 - 21, NODE / 2 - 21, 42, 42), accent, 1.5
        )

        if self.view.trigger:
            paint_icon(painter, "bolt", QRectF(-26, NODE / 2 - 9, 18, 18), theme.AMBER, 1.8)
        elif self.view.number:
            painter.setPen(theme.rgba(theme.TEXT_DIM, 200))
            painter.setFont(theme.font(theme.LABEL_FAMILY, 8, QFont.Weight.DemiBold))
            painter.drawText(
                QRectF(8, 5, 24, 14), Qt.AlignmentFlag.AlignLeft, str(self.view.number)
            )

        self._paint_ports(painter)
        self._paint_labels(painter)
        self._paint_status(painter)

    def _paint_ports(self, painter: QPainter) -> None:
        if not self.view.trigger:
            hot = self._hover_port == "<in>"
            painter.setPen(QPen(theme.CYAN if hot else theme.TEXT_DIM, 1.5))
            painter.setBrush(theme.BG_DEEP)
            painter.drawEllipse(QPointF(0, NODE / 2), PORT_R - 1, PORT_R - 1)
        many = len(self.view.outputs) > 1
        for port in self.view.outputs:
            point = self.port_point(port)
            hot = self._hover_port == port
            if port not in self.linked_ports:
                pen = QPen(theme.rgba(theme.TEXT_DIM, 150), 1.2, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(point + QPointF(PORT_R, 0), point + QPointF(STUB, 0))
                plus = self.plus_rect(port)
                over = self._hover_plus == port
                painter.setPen(QPen(theme.CYAN if over else theme.TEXT_DIM, 1.2))
                painter.setBrush(theme.rgba(theme.CYAN, 50) if over else theme.BG_DEEP)
                painter.drawRoundedRect(plus, 5, 5)
                paint_icon(
                    painter,
                    "plus",
                    plus.adjusted(4, 4, -4, -4),
                    theme.TEXT if over else theme.TEXT_DIM,
                    2.2,
                )
            painter.setPen(QPen(theme.CYAN if hot else theme.TEXT_DIM, 1.5))
            painter.setBrush(theme.rgba(theme.CYAN, 140) if hot else theme.BG_DEEP)
            painter.drawEllipse(point, PORT_R, PORT_R)
            if many:
                painter.setPen(theme.rgba(theme.TEXT_DIM, 230))
                painter.setFont(theme.font(theme.LABEL_FAMILY, 8, QFont.Weight.DemiBold, 0.6))
                painter.drawText(
                    QRectF(NODE + 8, point.y() - 17, 60, 12), Qt.AlignmentFlag.AlignLeft, port
                )

    def _paint_labels(self, painter: QPainter) -> None:
        box = QRectF(NODE / 2 - LABEL_W / 2, NODE + 7, LABEL_W, 18)
        title_font = theme.font(theme.LABEL_FAMILY, 11.5, QFont.Weight.Bold, 0.4)
        painter.setFont(title_font)
        painter.setPen(theme.TEXT)
        title = QFontMetricsF(title_font).elidedText(
            self.view.title, Qt.TextElideMode.ElideRight, LABEL_W
        )
        painter.drawText(box, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, title)
        if self.view.subtitle:
            small = theme.font(theme.BODY_FAMILY, 8)
            painter.setFont(small)
            painter.setPen(theme.TEXT_DIM)
            metrics = QFontMetricsF(small)
            text = self.view.subtitle
            if metrics.horizontalAdvance(text) > LABEL_W * 2 - 20:
                text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, LABEL_W * 2 - 30)
            option = QTextOption(Qt.AlignmentFlag.AlignHCenter)
            option.setWrapMode(QTextOption.WrapMode.WordWrap)
            painter.drawText(QRectF(box.left(), box.bottom() + 2, LABEL_W, 30), text, option)

    def _paint_status(self, painter: QPainter) -> None:
        if not self.status:
            return
        centre = QPointF(NODE - 4, 4)
        if self.status == "running":
            painter.setPen(QPen(theme.rgba(theme.CYAN, 60), 2.4))
            painter.setBrush(theme.BG_DEEP)
            painter.drawEllipse(centre, 8, 8)
            pen = QPen(theme.CYAN, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            start = int(-math.degrees(self.phase * 1.5) * 16)
            painter.drawArc(QRectF(centre.x() - 8, centre.y() - 8, 16, 16), start, 100 * 16)
            return
        colour = {
            "done": theme.ELECTRIC,
            "failed": theme.DANGER,
        }.get(self.status, theme.STEEL)
        painter.setPen(QPen(theme.BG_DEEP, 2))
        painter.setBrush(colour)
        painter.drawEllipse(centre, 9, 9)
        mark = QPen(theme.ICE, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(mark)
        x, y = centre.x(), centre.y()
        if self.status == "done":
            painter.drawPolyline([QPointF(x - 4, y), QPointF(x - 1, y + 3), QPointF(x + 4, y - 3)])
        elif self.status == "failed":
            painter.drawLine(QPointF(x - 3, y - 3), QPointF(x + 3, y + 3))
            painter.drawLine(QPointF(x + 3, y - 3), QPointF(x - 3, y + 3))
        else:
            painter.drawLine(QPointF(x - 4, y), QPointF(x + 4, y))

    # --- interaction ---

    def flow(self) -> FlowScene | None:
        scene = self.scene()
        return scene if isinstance(scene, FlowScene) else None

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: object) -> object:  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            flow = self.flow()
            if flow is not None:
                flow.node_moved_live(self.node_id)
        return super().itemChange(change, value)

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        pos = event.pos()
        port = self._port_at(pos)
        if (
            not port
            and not self.view.trigger
            and QLineF(pos, QPointF(0, NODE / 2)).length() < PORT_R + 4
        ):
            port = "<in>"
        plus = self._plus_at(pos)
        if (port, plus) != (self._hover_port, self._hover_plus):
            self._hover_port, self._hover_plus = port, plus
            self.setCursor(
                Qt.CursorShape.CrossCursor
                if port and port != "<in>"
                else Qt.CursorShape.PointingHandCursor
            )
            self.update()

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        self._hover_port = self._hover_plus = ""
        self.update()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        flow = self.flow()
        if event.button() == Qt.MouseButton.LeftButton and flow is not None:
            plus = self._plus_at(event.pos())
            if plus:
                flow.plus_clicked.emit(self.node_id, plus)
                event.accept()
                return
            port = self._port_at(event.pos())
            if port:
                self._wiring = port
                flow.begin_wire(self, port)
                event.accept()
                return
        self._press_pos = self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        flow = self.flow()
        if self._wiring and flow is not None:
            flow.update_wire(event.scenePos())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        flow = self.flow()
        if self._wiring and flow is not None:
            port, self._wiring = self._wiring, ""
            flow.end_wire(self, port, event.scenePos())
            return
        super().mouseReleaseEvent(event)
        if flow is None:
            return
        moved = [item for item in flow.selectedItems() if isinstance(item, NodeItem)]
        if self not in moved:
            moved.append(self)
        if self.pos() != self._press_pos:
            for item in moved:
                item.setPos(snap(item.x()), snap(item.y()))
            flow.nodes_moved.emit([(i.node_id, i.x(), i.y()) for i in moved])

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        flow = self.flow()
        if flow is not None:
            flow.node_opened.emit(self.node_id)


class LinkItem(QGraphicsObject):
    def __init__(self, src: str, port: str, dst: str) -> None:
        super().__init__()
        self.src, self.port, self.dst = src, port, dst
        self.path = QPainterPath()
        self.fired = False
        self._hover = False
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(1)

    def set_ends(self, start: QPointF, end: QPointF) -> None:
        self.prepareGeometryChange()
        self.path = link_path(start, end)
        self.update()

    def middle(self) -> QPointF:
        return self.path.pointAtPercent(0.5)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self.path.boundingRect().adjusted(-14, -14, 14, 14)

    def shape(self) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        outline = stroker.createStroke(self.path)
        outline.addEllipse(self.middle(), 12, 12)
        return outline

    def paint(
        self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None
    ) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hot = self._hover or self.isSelected()
        colour = theme.CYAN if hot else theme.ELECTRIC if self.fired else _LINK
        if hot or self.fired:
            painter.setPen(QPen(theme.rgba(colour, 45), 7))
            painter.drawPath(self.path)
        painter.setPen(
            QPen(colour, 2.2 if hot else 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path)
        # An arrowhead into the input.
        end = self.path.pointAtPercent(1.0)
        angle = math.radians(self.path.angleAtPercent(0.98))
        back = QPointF(math.cos(angle), -math.sin(angle))
        side = QPointF(-back.y(), back.x())
        tip = end - back * (PORT_R - 1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        painter.drawPolygon([tip, tip - back * 8 + side * 4.5, tip - back * 8 - side * 4.5])
        if hot:
            centre = self.middle()
            painter.setPen(QPen(theme.DANGER, 1.4))
            painter.setBrush(theme.BG_DEEP)
            painter.drawEllipse(centre, 10, 10)
            pen = QPen(theme.DANGER, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(centre + QPointF(-3.5, -3.5), centre + QPointF(3.5, 3.5))
            painter.drawLine(centre + QPointF(3.5, -3.5), centre + QPointF(-3.5, 3.5))

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:  # noqa: N802
        scene = self.scene()
        if QLineF(event.pos(), self.middle()).length() <= 12 and isinstance(scene, FlowScene):
            scene.link_removed.emit(self.src, self.port, self.dst)
            event.accept()
            return
        super().mousePressEvent(event)


class FlowScene(QGraphicsScene):
    node_selected = Signal(str)  # "" when nothing is
    nodes_moved = Signal(list)  # [(id, x, y)]
    link_requested = Signal(str, str, str)  # src, port, dst
    link_removed = Signal(str, str, str)
    add_from = Signal(str, str, float, float)  # src, port, where to put the new node
    plus_clicked = Signal(str, str)  # src, port
    node_opened = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.nodes: dict[str, NodeItem] = {}
        self.links: list[LinkItem] = []
        self._wire: QGraphicsPathItem | None = None
        self._wire_from: tuple[NodeItem, str] | None = None
        self.setSceneRect(QRectF(-4000, -4000, 8000, 8000))
        self.selectionChanged.connect(self._selection)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def show(
        self, nodes: list[NodeView], links: list[tuple[str, str, str]], select: str = ""
    ) -> None:
        """Draw ``nodes`` and ``links`` (keeping run statuses for nodes that stay)."""
        old = {node_id: (item.status, item.note) for node_id, item in self.nodes.items()}
        self.blockSignals(True)
        self.clear()
        self.nodes, self.links, self._wire = {}, [], None
        for view in nodes:
            item = NodeItem(view)
            item.status, item.note = old.get(view.id, ("", ""))
            self.addItem(item)
            self.nodes[view.id] = item
        for src, port, dst in links:
            if src in self.nodes and dst in self.nodes:
                link = LinkItem(src, port, dst)
                self.addItem(link)
                self.links.append(link)
                self.nodes[src].linked_ports.add(port)
        self._place_links()
        if select in self.nodes:
            self.nodes[select].setSelected(True)
        self.blockSignals(False)

    def _place_links(self, only: str = "") -> None:
        for link in self.links:
            if only and only not in (link.src, link.dst):
                continue
            src, dst = self.nodes[link.src], self.nodes[link.dst]
            link.set_ends(src.output_scene(link.port), dst.input_scene())

    def node_moved_live(self, node_id: str) -> None:
        self._place_links(node_id)

    def _selection(self) -> None:
        chosen = [item for item in self.selectedItems() if isinstance(item, NodeItem)]
        self.node_selected.emit(chosen[0].node_id if len(chosen) == 1 else "")

    def selected_link(self) -> LinkItem | None:
        return next((i for i in self.selectedItems() if isinstance(i, LinkItem)), None)

    # --- drawing a new link ---

    def begin_wire(self, node: NodeItem, port: str) -> None:
        self._wire_from = (node, port)
        self._wire = QGraphicsPathItem()
        pen = QPen(theme.CYAN, 2, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap)
        self._wire.setPen(pen)
        self._wire.setZValue(3)
        self.addItem(self._wire)
        self.update_wire(node.output_scene(port))

    def update_wire(self, pos: QPointF) -> None:
        if self._wire is None or self._wire_from is None:
            return
        node, port = self._wire_from
        self._wire.setPath(link_path(node.output_scene(port), pos))

    def end_wire(self, node: NodeItem, port: str, pos: QPointF) -> None:
        if self._wire is not None:
            self.removeItem(self._wire)
        self._wire = None
        self._wire_from = None
        target = self.node_at(pos)
        if target is not None and target is not node:
            if not target.view.trigger:
                self.link_requested.emit(node.node_id, port, target.node_id)
            return
        if target is None:
            self.add_from.emit(node.node_id, port, snap(pos.x() + 10), snap(pos.y() - NODE / 2))

    def node_at(self, pos: QPointF) -> NodeItem | None:
        for item in self.items(pos):
            if isinstance(item, NodeItem):
                return item
        return None

    # --- run status ---

    def set_status(self, node_id: str, status: str, note: str = "") -> None:
        item = self.nodes.get(node_id)
        if item is None:
            return
        item.status, item.note = status, note
        if note:
            item.setToolTip(f"{item.view.subtitle}\n\n{note}")
        item.update()
        if status == "running" and not self._timer.isActive():
            self._timer.start()

    def set_fired(self, ran: set[str]) -> None:
        for link in self.links:
            link.fired = link.src in ran and link.dst in ran
            link.update()

    def clear_status(self) -> None:
        for item in self.nodes.values():
            item.status, item.note = "", ""
            item.setToolTip(item.view.subtitle)
            item.update()
        self.set_fired(set())

    def _tick(self) -> None:
        running = [item for item in self.nodes.values() if item.status == "running"]
        if not running:
            self._timer.stop()
            return
        for item in running:
            item.phase += 0.25
            item.update()


class FlowView(QGraphicsView):
    """The canvas viewport: dotted grid, drag to pan, scroll to zoom."""

    MIN_ZOOM, MAX_ZOOM = 0.3, 2.0

    def __init__(self, scene: FlowScene) -> None:
        super().__init__(scene)
        self.flow = scene
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setStyleSheet(f"background: {theme.css(theme.BG_DEEP)}; border: none;")

    def zoom(self) -> float:
        return self.transform().m11()

    def zoom_by(self, factor: float) -> None:
        target = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self.zoom() * factor))
        factor = target / self.zoom()
        self.scale(factor, factor)

    def zoom_in(self) -> None:
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.zoom_by(1.2)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def zoom_out(self) -> None:
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.zoom_by(1 / 1.2)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)

    def fit(self) -> None:
        items = [item for item in self.flow.nodes.values()]
        if not items:
            self.resetTransform()
            self.centerOn(0, 0)
            return
        area = QRectF()
        for item in items:
            area = area.united(item.sceneBoundingRect())
        area = area.adjusted(-60, -60, 60, 60)
        self.resetTransform()
        self.fitInView(area, Qt.AspectRatioMode.KeepAspectRatio)
        if self.zoom() > 1.0:
            self.resetTransform()
        elif self.zoom() < self.MIN_ZOOM:
            self.resetTransform()
            self.scale(self.MIN_ZOOM, self.MIN_ZOOM)
        self.centerOn(area.center())

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta:
            self.zoom_by(math.pow(1.0015, delta))
        event.accept()

    def drawBackground(self, painter: QPainter, rect: QRectF | QRect) -> None:  # noqa: N802
        rect = QRectF(rect)
        painter.fillRect(rect, theme.BG_DEEP)
        zoom = self.zoom()
        spacing = GRID if zoom >= 0.6 else GRID * 2
        left = math.floor(rect.left() / spacing) * spacing
        top = math.floor(rect.top() / spacing) * spacing
        small = QPen(theme.rgba(theme.STEEL, 95), 1.8 / zoom)
        small.setCapStyle(Qt.PenCapStyle.RoundCap)
        big = QPen(theme.rgba(theme.CYAN, 55), 2.6 / zoom)
        big.setCapStyle(Qt.PenCapStyle.RoundCap)
        dots: list[QPointF] = []
        majors: list[QPointF] = []
        y = top
        while y < rect.bottom():
            x = left
            major_row = round(y / spacing) % 5 == 0
            while x < rect.right():
                point = QPointF(x, y)
                if major_row and round(x / spacing) % 5 == 0:
                    majors.append(point)
                else:
                    dots.append(point)
                x += spacing
            y += spacing
        painter.setPen(small)
        painter.drawPoints(dots)
        painter.setPen(big)
        painter.drawPoints(majors)
