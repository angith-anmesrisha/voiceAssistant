"""Line icons for workflow nodes, drawn with QPainter (no image files, crisp at any zoom).

Each icon is drawn in a 24x24 box, in the style of a thin outline icon set, and scaled
to the rectangle it's given. Unknown names fall back to a dot.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from jarvis.ui import theme

# Each node group's accent colour and default icon (kept to the HUD's cool palette,
# with amber and coral for contrast; no greens).
GROUP_STYLE: dict[str, tuple[QColor, str]] = {
    "Trigger": (theme.AMBER, "bolt"),
    "Gmail": (QColor(255, 112, 112), "mail"),
    "Calendar": (QColor(96, 165, 250), "calendar"),
    "Notes & memory": (QColor(250, 204, 21), "note"),
    "Tasks & reminders": (QColor(125, 211, 252), "check"),
    "JARVIS AI": (theme.CYAN, "spark"),
    "Web & info": (QColor(129, 140, 248), "globe"),
    "Apps & files": (QColor(186, 230, 253), "folder"),
    "Messaging": (QColor(45, 212, 191), "chat"),
    "PC": (QColor(148, 163, 184), "monitor"),
    "Core": (QColor(251, 146, 60), "gear"),
}


def accent(group: str) -> QColor:
    return QColor(GROUP_STYLE.get(group, (theme.CYAN, ""))[0])


Draw = Callable[[QPainter], None]


def _path(*points: tuple[float, float], closed: bool = False) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if closed:
        path.closeSubpath()
    return path


def _lines(p: QPainter, *segments: tuple[float, float, float, float]) -> None:
    for x1, y1, x2, y2 in segments:
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))


def _mail(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 5.5, 18, 13), 2, 2)
    p.drawPath(_path((3.5, 7), (12, 13), (20.5, 7)))


def _reply(p: QPainter) -> None:
    p.drawPath(_path((9, 6), (3.5, 11), (9, 16)))
    path = QPainterPath(QPointF(4, 11))
    path.lineTo(14, 11)
    path.cubicTo(QPointF(18.5, 11), QPointF(20.5, 13.5), QPointF(20.5, 18.5))
    p.drawPath(path)


def _calendar(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 5, 17, 15.5), 2, 2)
    _lines(p, (3.5, 10, 20.5, 10), (8, 3, 8, 7), (16, 3, 16, 7))
    p.setBrush(p.pen().color())
    for x, y in ((8, 14), (12, 14), (16, 14), (8, 17.5)):
        p.drawEllipse(QPointF(x, y), 0.9, 0.9)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _note(p: QPainter) -> None:
    p.drawPath(_path((14.5, 3), (5.5, 3), (5.5, 21), (18.5, 21), (18.5, 7), (14.5, 3)))
    p.drawPath(_path((14.5, 3), (14.5, 7), (18.5, 7)))
    _lines(p, (8.5, 12, 15.5, 12), (8.5, 15.5, 15.5, 15.5))


def _brain(p: QPainter) -> None:
    p.drawEllipse(QRectF(4, 4, 16, 16))
    path = QPainterPath(QPointF(12, 4))
    path.cubicTo(QPointF(9, 8), QPointF(15, 10), QPointF(12, 13))
    path.cubicTo(QPointF(9, 16), QPointF(14, 18), QPointF(12, 20))
    p.drawPath(path)
    _lines(p, (6.5, 10, 9.5, 11), (17.5, 13, 14.5, 14))


def _check(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 3.5, 17, 17), 3, 3)
    p.drawPath(_path((8, 12.5), (11, 15.5), (16.5, 9)))


def _list(p: QPainter) -> None:
    _lines(p, (9, 6.5, 20, 6.5), (9, 12, 20, 12), (9, 17.5, 20, 17.5))
    p.setBrush(p.pen().color())
    for y in (6.5, 12, 17.5):
        p.drawEllipse(QPointF(4.8, y), 1.1, 1.1)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _timer(p: QPainter) -> None:
    p.drawEllipse(QRectF(4, 5, 16, 16))
    _lines(p, (12, 13, 12, 8.5), (12, 13, 15, 15), (9.5, 2.5, 14.5, 2.5))


def _bell(p: QPainter) -> None:
    path = QPainterPath(QPointF(6, 16.5))
    path.lineTo(6, 11)
    path.cubicTo(QPointF(6, 7), QPointF(8.5, 4.5), QPointF(12, 4.5))
    path.cubicTo(QPointF(15.5, 4.5), QPointF(18, 7), QPointF(18, 11))
    path.lineTo(18, 16.5)
    path.lineTo(19.5, 18)
    path.lineTo(4.5, 18)
    path.closeSubpath()
    p.drawPath(path)
    p.drawArc(QRectF(10, 18.5, 4, 3), 180 * 16, 180 * 16)


def _spark(p: QPainter) -> None:
    def star(cx: float, cy: float, r: float) -> QPainterPath:
        k = r * 0.28
        path = QPainterPath(QPointF(cx, cy - r))
        path.quadTo(QPointF(cx + k, cy - k), QPointF(cx + r, cy))
        path.quadTo(QPointF(cx + k, cy + k), QPointF(cx, cy + r))
        path.quadTo(QPointF(cx - k, cy + k), QPointF(cx - r, cy))
        path.quadTo(QPointF(cx - k, cy - k), QPointF(cx, cy - r))
        return path

    p.drawPath(star(10, 13, 7.5))
    p.drawPath(star(18.5, 5.5, 3))


def _deck(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 4, 18, 12.5), 1.5, 1.5)
    _lines(p, (12, 16.5, 12, 20.5), (8, 20.5, 16, 20.5))
    p.drawPath(_path((7, 13), (10, 10), (12.5, 12), (17, 8)))


def _search(p: QPainter) -> None:
    p.drawEllipse(QRectF(4, 4, 12, 12))
    _lines(p, (14.5, 14.5, 20, 20))


def _book(p: QPainter) -> None:
    p.drawPath(_path((12, 6.5), (12, 20)))
    left = QPainterPath(QPointF(12, 6.5))
    left.cubicTo(QPointF(9, 4.5), QPointF(5.5, 4.5), QPointF(3.5, 5.5))
    left.lineTo(3.5, 18.5)
    left.cubicTo(QPointF(5.5, 17.5), QPointF(9, 17.5), QPointF(12, 20))
    p.drawPath(left)
    right = QPainterPath(QPointF(12, 6.5))
    right.cubicTo(QPointF(15, 4.5), QPointF(18.5, 4.5), QPointF(20.5, 5.5))
    right.lineTo(20.5, 18.5)
    right.cubicTo(QPointF(18.5, 17.5), QPointF(15, 17.5), QPointF(12, 20))
    p.drawPath(right)


def _pen(p: QPainter) -> None:
    p.drawPath(_path((15.5, 4.5), (19.5, 8.5), (8.5, 19.5), (4, 20), (4.5, 15.5), closed=True))
    _lines(p, (13.5, 6.5, 17.5, 10.5))


def _globe(p: QPainter) -> None:
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    p.drawEllipse(QRectF(8.5, 3.5, 7, 17))
    _lines(p, (3.5, 12, 20.5, 12))


def _cloud(p: QPainter) -> None:
    path = QPainterPath(QPointF(7, 18.5))
    path.cubicTo(QPointF(3.5, 18.5), QPointF(2.5, 13.5), QPointF(6.5, 12.5))
    path.cubicTo(QPointF(6.5, 7), QPointF(14, 5.5), QPointF(16, 10))
    path.cubicTo(QPointF(21.5, 9.5), QPointF(22.5, 18.5), QPointF(17, 18.5))
    path.closeSubpath()
    p.drawPath(path)


def _sun(p: QPainter) -> None:
    p.drawEllipse(QRectF(8, 8, 8, 8))
    for i in range(8):
        a = i * math.pi / 4
        c, s = math.cos(a), math.sin(a)
        p.drawLine(QPointF(12 + 6.2 * c, 12 + 6.2 * s), QPointF(12 + 8.8 * c, 12 + 8.8 * s))


def _news(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 4.5, 18, 15), 2, 2)
    p.drawRect(QRectF(6, 8, 5, 4))
    _lines(p, (13.5, 8.5, 18, 8.5), (13.5, 11.5, 18, 11.5), (6, 15.5, 18, 15.5))


def _chart(p: QPainter) -> None:
    _lines(p, (4, 20, 20, 20))
    p.drawPath(_path((4, 16), (9, 11), (13, 14), (20, 6)))
    p.drawPath(_path((16, 6), (20, 6), (20, 10)))


def _play(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(2.5, 5, 19, 14), 4, 4)
    p.drawPath(_path((10, 9), (15.5, 12), (10, 15), closed=True))


def _folder(p: QPainter) -> None:
    p.drawPath(
        _path((3, 7), (3, 19), (21, 19), (21, 8.5), (12, 8.5), (10, 6), (3, 6), (3, 7), closed=True)
    )


def _file(p: QPainter) -> None:
    p.drawPath(_path((14, 3), (6, 3), (6, 21), (18, 21), (18, 7), (14, 3)))
    p.drawPath(_path((14, 3), (14, 7), (18, 7)))


def _save(p: QPainter) -> None:
    p.drawPath(_path((4, 4), (16.5, 4), (20, 7.5), (20, 20), (4, 20), closed=True))
    p.drawRect(QRectF(8, 13, 8, 7))
    _lines(p, (8, 4, 8, 8.5), (8, 8.5, 14.5, 8.5), (14.5, 8.5, 14.5, 4))


def _app(p: QPainter) -> None:
    for x, y in ((4, 4), (13.5, 4), (4, 13.5), (13.5, 13.5)):
        p.drawRoundedRect(QRectF(x, y, 6.5, 6.5), 1.5, 1.5)


def _chat(p: QPainter) -> None:
    path = QPainterPath(QPointF(12, 3.5))
    path.cubicTo(QPointF(17, 3.5), QPointF(20.5, 7), QPointF(20.5, 11.5))
    path.cubicTo(QPointF(20.5, 16), QPointF(17, 19.5), QPointF(12, 19.5))
    path.cubicTo(QPointF(10.5, 19.5), QPointF(9, 19.2), QPointF(7.8, 18.6))
    path.lineTo(3.5, 20.5)
    path.lineTo(5, 16.4)
    path.cubicTo(QPointF(4, 15), QPointF(3.5, 13.3), QPointF(3.5, 11.5))
    path.cubicTo(QPointF(3.5, 7), QPointF(7, 3.5), QPointF(12, 3.5))
    p.drawPath(path)


def _speaker(p: QPainter) -> None:
    p.drawPath(_path((4, 9.5), (8, 9.5), (13, 5), (13, 19), (8, 14.5), (4, 14.5), closed=True))
    p.drawArc(QRectF(12, 8, 6, 8), -60 * 16, 120 * 16)
    p.drawArc(QRectF(12, 5, 9.5, 14), -60 * 16, 120 * 16)


def _moon(p: QPainter) -> None:
    path = QPainterPath(QPointF(19.5, 14.5))
    path.cubicTo(QPointF(15, 16.5), QPointF(8.5, 13), QPointF(10, 4.5))
    path.cubicTo(QPointF(5.5, 5.5), QPointF(3, 10), QPointF(4.5, 14.5))
    path.cubicTo(QPointF(6.5, 20.5), QPointF(15.5, 21.5), QPointF(19.5, 14.5))
    p.drawPath(path)


def _pulse(p: QPainter) -> None:
    p.drawPath(_path((2.5, 12), (7, 12), (9.5, 6), (13.5, 18.5), (16, 12), (21.5, 12)))


def _monitor(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 4, 18, 12.5), 1.5, 1.5)
    _lines(p, (12, 16.5, 12, 20.5), (8, 20.5, 16, 20.5))


def _close(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3.5, 3.5, 17, 17), 3, 3)
    _lines(p, (9, 9, 15, 15), (15, 9, 9, 15))


def _lock(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(5, 11, 14, 9.5), 2, 2)
    p.drawPath(_path((8, 11), (8, 8)))
    p.drawArc(QRectF(8, 4, 8, 8), 0, 180 * 16)
    p.drawPath(_path((16, 8), (16, 11)))


def _question(p: QPainter) -> None:
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    path = QPainterPath(QPointF(9.3, 9.5))
    path.cubicTo(QPointF(9.3, 6), QPointF(15, 6), QPointF(14.8, 9.6))
    path.cubicTo(QPointF(14.6, 11.8), QPointF(12, 11.8), QPointF(12, 14))
    p.drawPath(path)
    p.setBrush(p.pen().color())
    p.drawEllipse(QPointF(12, 17), 0.9, 0.9)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _branch(p: QPainter) -> None:
    p.drawEllipse(QPointF(6, 12), 2.2, 2.2)
    p.drawEllipse(QPointF(18.5, 5.5), 2.2, 2.2)
    p.drawEllipse(QPointF(18.5, 18.5), 2.2, 2.2)
    up = QPainterPath(QPointF(8.2, 12))
    up.cubicTo(QPointF(12, 12), QPointF(12, 5.5), QPointF(16.3, 5.5))
    p.drawPath(up)
    down = QPainterPath(QPointF(8.2, 12))
    down.cubicTo(QPointF(12, 12), QPointF(12, 18.5), QPointF(16.3, 18.5))
    p.drawPath(down)


def _gear(p: QPainter) -> None:
    p.drawEllipse(QRectF(9, 9, 6, 6))
    path = QPainterPath()
    teeth = 8
    for i in range(teeth * 2):
        a = i * math.pi / teeth
        r = 8.5 if i % 2 == 0 else 6.6
        for da in (-0.18, 0.18):
            point = QPointF(12 + r * math.cos(a + da), 12 + r * math.sin(a + da))
            if path.elementCount() == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
    path.closeSubpath()
    p.drawPath(path)


def _bolt(p: QPainter) -> None:
    p.drawPath(_path((13.5, 2.5), (4.5, 13.5), (11.5, 13.5), (10.5, 21.5), (19.5, 10), (12.5, 10),
                     closed=True))  # fmt: skip


def _mic(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(9, 3, 6, 11), 3, 3)
    p.drawArc(QRectF(5.5, 6, 13, 11), 180 * 16, 180 * 16)
    _lines(p, (12, 17, 12, 21), (8.5, 21, 15.5, 21))


def _power(p: QPainter) -> None:
    p.drawArc(QRectF(4, 5, 16, 16), 125 * 16, -250 * 16)
    _lines(p, (12, 3, 12, 11))


def _hand(p: QPainter) -> None:
    p.drawEllipse(QRectF(3.5, 3.5, 17, 17))
    p.drawPath(_path((10, 8.5), (16, 12), (10, 15.5), closed=True))


def _plus(p: QPainter) -> None:
    _lines(p, (12, 5, 12, 19), (5, 12, 19, 12))


_ICONS: dict[str, Draw] = {
    "mail": _mail,
    "reply": _reply,
    "calendar": _calendar,
    "note": _note,
    "brain": _brain,
    "check": _check,
    "list": _list,
    "timer": _timer,
    "bell": _bell,
    "spark": _spark,
    "deck": _deck,
    "search": _search,
    "book": _book,
    "pen": _pen,
    "globe": _globe,
    "cloud": _cloud,
    "sun": _sun,
    "news": _news,
    "chart": _chart,
    "play": _play,
    "folder": _folder,
    "file": _file,
    "save": _save,
    "app": _app,
    "chat": _chat,
    "speaker": _speaker,
    "moon": _moon,
    "pulse": _pulse,
    "monitor": _monitor,
    "close": _close,
    "lock": _lock,
    "question": _question,
    "branch": _branch,
    "gear": _gear,
    "bolt": _bolt,
    "mic": _mic,
    "power": _power,
    "hand": _hand,
    "plus": _plus,
}
NAMES = tuple(_ICONS)


def paint_icon(
    painter: QPainter, name: str, rect: QRectF, color: QColor, width: float = 1.6
) -> None:
    """Draw icon ``name`` filling ``rect`` (square) in ``color``."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = min(rect.width(), rect.height())
    painter.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
    painter.scale(side / 24, side / 24)
    pen = QPen(color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw = _ICONS.get(name)
    if draw is None:
        painter.setBrush(color)
        painter.drawEllipse(QPointF(12, 12), 3, 3)
    else:
        draw(painter)
    painter.restore()


def icon(name: str, color: QColor, size: int = 32) -> QIcon:
    """``name`` as a QIcon (for lists and buttons)."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    paint_icon(painter, name, QRectF(2, 2, size - 4, size - 4), color)
    painter.end()
    return QIcon(pixmap)
