"""Soft shadows that cost nothing while the window is used.

The look is the same soft shadow as before, but it used to be Qt's `QGraphicsDropShadowEffect`: an effect on a card makes Qt re-draw the WHOLE card and blur it
again every time anything inside changes - a blinking text cursor, a hover, the pulsing step of a running booking, one scrolled line of the flight list. On a modest
computer that was most of the app's work (measured: a blinking cursor in a card cost 27 % of a core, 5 % without the effect).

Here the shadow is painted ONCE per size class into a small picture and put behind the widget by a transparent layer that lives in the widget's parent. The layer
repaints only when a card moves, changes size, appears or disappears - never because something inside a card changed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

SIGMA_PER_BLUR = 0.26            # how soft Qt's "blur radius" looks, as a Gaussian width: matched pixel by pixel against the old effect (mean difference < 0.3 of 255)
_cache: dict[tuple, tuple[QPixmap, int, int]] = {}


def _sdf(x: float, y: float, hw: float, hh: float, r: float) -> float:
    """Signed distance of a point to a rounded rectangle of half size (hw, hh) and corner radius r, centred at the origin (negative inside)."""
    qx, qy = abs(x) - (hw - r), abs(y) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def shadow_slices(blur: int, alpha: int, radius: int) -> tuple[QPixmap, int, int]:
    """The shadow of a rounded rectangle as ONE small picture that can be cut in nine: (picture, margin, corner) - `margin` is how far the shadow reaches beyond
    the rectangle, `corner` the size of each corner piece (the picture is 2 * corner + 2 wide). Rendered once per (blur, alpha, radius) and kept."""
    key = (blur, alpha, radius)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    sigma = max(1.0, blur * SIGMA_PER_BLUR)
    margin = int(math.ceil(sigma * 2.8))
    corner = margin + radius + 2
    size = 2 * corner + 2
    half = (size - 2 * margin) / 2.0                        # the rectangle's half size inside the picture
    c0 = size / 2.0
    peak = alpha / 255.0
    root2 = sigma * math.sqrt(2.0)
    r, g, b = 30, 40, 90
    data = bytearray(size * size * 4)
    i = 0
    for py in range(size):
        for px in range(size):
            d = _sdf(px + 0.5 - c0, py + 0.5 - c0, half, half, float(radius))
            a = peak * 0.5 * math.erfc(d / root2)
            if a > 0.002:
                data[i] = int(b * a + 0.5)                  # (premultiplied BGRA)
                data[i + 1] = int(g * a + 0.5)
                data[i + 2] = int(r * a + 0.5)
                data[i + 3] = int(255 * a + 0.5)
            i += 4
    img = QImage(bytes(data), size, size, size * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    out = (QPixmap.fromImage(img), margin, corner)
    _cache[key] = out
    return out


def paint_shadow(p: QPainter, rect: QRect, blur: int, alpha: int, dy: int, radius: int) -> None:
    """Draw the shadow of `rect` (the widget's rectangle, in the painter's coordinates) with the nine-slice picture."""
    pm, margin, c = shadow_slices(blur, alpha, radius)
    sw = pm.width()
    x0, y0 = rect.left() - margin, rect.top() + dy - margin
    x1, y1 = rect.left() + rect.width() + margin, rect.top() + dy + rect.height() + margin      # (exclusive)
    w, h = x1 - x0, y1 - y0
    k = min(c, w // 2, h // 2)
    if k < 1:
        return
    mid = c                                                 # the picture's one-pixel middle row / column: the edges are stretched from it
    pieces = [
        (QRect(x0, y0, k, k), QRect(0, 0, k, k)),                                  # the four corners
        (QRect(x1 - k, y0, k, k), QRect(sw - k, 0, k, k)),
        (QRect(x0, y1 - k, k, k), QRect(0, sw - k, k, k)),
        (QRect(x1 - k, y1 - k, k, k), QRect(sw - k, sw - k, k, k)),
        (QRect(x0 + k, y0, w - 2 * k, k), QRect(mid, 0, 1, k)),                    # the four edges
        (QRect(x0 + k, y1 - k, w - 2 * k, k), QRect(mid, sw - k, 1, k)),
        (QRect(x0, y0 + k, k, h - 2 * k), QRect(0, mid, k, 1)),
        (QRect(x1 - k, y0 + k, k, h - 2 * k), QRect(sw - k, mid, k, 1)),
    ]
    for dst, src in pieces:
        if dst.width() > 0 and dst.height() > 0:
            p.drawPixmap(dst, pm, src)


@dataclass
class _Item:
    widget: QWidget
    blur: int
    alpha: int
    dy: int
    radius: int


class ShadowLayer(QWidget):
    """A transparent layer inside `host`, under everything else in it, that paints the soft shadows of the widgets registered with `watch()` (children of the host)."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        self._items: list[_Item] = []
        self._host = host
        host.installEventFilter(self)
        self.setGeometry(host.rect())
        self.lower()
        self.show()

    def watch(self, widget: QWidget, blur: int = 22, alpha: int = 16, dy: int = 4, radius: int = 16) -> None:
        self._items.append(_Item(widget, blur, alpha, dy, radius))
        widget.installEventFilter(self)
        self.lower()
        self.update()

    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802 (Qt name)
        t = ev.type()
        if obj is self._host:
            if t == QEvent.Type.Resize:
                self.setGeometry(self._host.rect())
                self.lower()
        elif t in (QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.Hide):
            self.update()
        return False

    def paintEvent(self, _ev) -> None:  # noqa: N802 (Qt name)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)          # (on a scaled-up screen the little pictures are enlarged smoothly)
        for it in self._items:
            w = it.widget
            if w.isHidden() or not w.isVisibleTo(self._host):
                continue
            top_left = w.mapTo(self._host, QPoint(0, 0))
            paint_shadow(p, QRect(top_left, w.size()), it.blur, it.alpha, it.dy, it.radius)
        p.end()
