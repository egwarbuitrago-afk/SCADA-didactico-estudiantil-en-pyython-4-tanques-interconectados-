# -*- coding: utf-8 -*-
"""SCADA académico para cuatro tanques de proceso y el depósito principal TK-105.

Interfaz: PySide6 + archivo editado en Qt Designer (ui/main_window.ui).
Vista de proceso: P&ID dibujado con QPainter.
Tendencias: PyQtGraph si está instalado; incluye un gráfico de respaldo.

IMPORTANTE: el modo Simulación es un modelo didáctico, no un gemelo validado.
Modbus TCP y MQTT son clientes reales opcionales. La aplicación no se debe usar
para control de seguridad ni para una planta industrial real sin ingeniería,
validación, análisis de riesgos y pruebas específicas.
"""

from __future__ import annotations

import csv
import json
import math
import sys
import ctypes
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from PySide6 import QtGui
from PySide6.QtCore import Qt, QObject, QEvent, QTimer, QFile, QIODevice, QPointF, QRectF, QSignalBlocker
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont, QPolygonF, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QMessageBox, QInputDialog, QWidget, QFrame, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout, QGroupBox, QComboBox,
    QLineEdit, QSpinBox, QDoubleSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QStackedWidget, QSizePolicy, QStyle, QScrollArea,
)
from PySide6.QtUiTools import QUiLoader

try:
    import pyqtgraph as pg
except Exception:
    pg = None

BASE = Path(__file__).resolve().parent
UI_PATH = BASE / "ui" / "main_window.ui"
if not UI_PATH.exists():
    UI_PATH = BASE / "main_window.ui"
ICON_PATH = BASE / "assets" / "icono_scada_nivel_temperatura_caudal.ico"
AUDIT_PATH = BASE / "audit" / "events.csv"

# Paleta HMI neutra. El color se reserva principalmente a estados y alarmas.
COLORS = {
    "bg": "#edf1f3", "panel": "#ffffff", "header": "#263238", "menu": "#334047",
    "text": "#263238", "muted": "#64727c", "border": "#cfd8dc", "water": "#8fc8e8",
    "pipe": "#607d8b", "flow": "#2878b5", "good": "#2f7d5b", "high": "#e07b00",
    "alarm": "#c42b1c", "quality": "#7f8c94", "purple": "#7356a8",
}
PRIORITY_COLOR = {"ALTA": COLORS["alarm"], "MEDIA": COLORS["high"], "BAJA": "#8a6d00"}
SERIES_COLORS = ["#2878b5", "#2a9d8f", "#9b59b6", "#d17a22"]


class AuditLogger:
    """Bitácora CSV local para registrar accesos, cambios y fallos de comunicación."""
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["timestamp", "usuario", "tipo", "detalle"])

    def __call__(self, kind: str, detail: str, user: str = "sistema"):
        try:
            with self.path.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([datetime.now().isoformat(timespec="seconds"), user, kind, detail])
        except OSError:
            # Un fallo de auditoría no debe tumbar la interfaz; se informa en consola.
            print(f"No se pudo escribir la bitácora: {kind} - {detail}")


class TitleBarController(QObject):
    """Permite arrastrar/maximizar la ventana sin la barra nativa de Windows."""
    def __init__(self, window, maximize_button):
        super().__init__(window)
        self.window = window
        self.maximize_button = maximize_button
        self.drag_position = None

    def toggle_maximize(self):
        if self.window.isMaximized():
            self.window.showNormal()
            self.maximize_button.setText("□")
        else:
            self.window.showMaximized()
            self.maximize_button.setText("❐")

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton and not self.window.isMaximized():
                self.drag_position = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
        elif event.type() == QEvent.Type.MouseMove:
            if (self.drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton
                    and not self.window.isMaximized()):
                self.window.move(event.globalPosition().toPoint() - self.drag_position)
                return True
        elif event.type() == QEvent.Type.MouseButtonRelease:
            self.drag_position = None
        return False

# Reemplaza la clase PIDCanvas existente por esta versión.
# Usa las clases/importaciones Qt que ya aparecen en main_qt_hibrido.py.

class PIDCanvas(QWidget):
    """P&ID esquemático basado en la imagen de referencia de cuatro tanques."""

    W = 517
    H = 389

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setMinimumHeight(460)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip(
            "P&ID interactivo: haga clic en T-101…T-104 o TK-105 para abrir su HMI. "
            "Verifique conexiones y sentido del flujo antes de utilizarlo con un proceso físico."
        )
        self.setMouseTracking(True)
        self._hit_targets = {
            0: QRectF(121, 175, 80, 91),   # T-101
            1: QRectF(304, 175, 80, 91),   # T-102
            2: QRectF(121, 43, 80, 91),    # T-103
            3: QRectF(304, 43, 80, 91),    # T-104
            4: QRectF(98, 316, 296, 69),   # TK-105
        }

    def _logical_position(self, widget_pos):
        """Convierte coordenadas del widget a coordenadas lógicas 517 x 389."""
        sx = self.width() / self.W
        sy = self.height() / self.H
        scale = min(sx, sy)
        if scale <= 0:
            return QPointF(-1, -1)
        offset_x = (self.width() - self.W * scale) / 2
        offset_y = (self.height() - self.H * scale) / 2
        return QPointF((widget_pos.x() - offset_x) / scale,
                       (widget_pos.y() - offset_y) / scale)

    def _target_at(self, widget_pos):
        logical = self._logical_position(widget_pos)
        for tank_index, rect in self._hit_targets.items():
            if rect.contains(logical):
                return tank_index
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            target = self._target_at(event.position())
            if target is not None:
                if target == 4:
                    self.owner.open_reservoir_hmi()
                else:
                    self.owner.open_tank_hmi(target)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if self._target_at(event.position()) is not None
            else Qt.CursorShape.ArrowCursor
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#ffffff"))

        # Se conserva la proporción de la imagen original (517 x 389).
        sx = self.width() / self.W
        sy = self.height() / self.H
        scale = min(sx, sy)
        p.translate((self.width() - self.W * scale) / 2,
                    (self.height() - self.H * scale) / 2)
        p.scale(scale, scale)

        pipe = QColor("#202629") if self.owner.comms_ok else QColor("#9aa4aa")
        pipe_width = 1.55

        # ---------------------------------------------------------------
        # Tuberías principales y alimentación cruzada a los tanques altos.
        # P-101 -> T-104; P-102 -> T-103.
        self._polyline(p, [(52, 9), (343, 9), (343, 46)], pipe, pipe_width)
        self._polyline(p, [(160, 20), (456, 20)], pipe, pipe_width)
        self._polyline(p, [(160, 20), (160, 46)], pipe, pipe_width)
        self._polyline(p, [(456, 20), (456, 42)], pipe, pipe_width)

        # Montante izquierdo: FT-122, CV-104 y FT-101.
        self._polyline(p, [(52, 9), (52, 42)], pipe, pipe_width)
        self._polyline(p, [(52, 66), (52, 91)], pipe, pipe_width)
        self._polyline(p, [(52, 113), (52, 197)], pipe, pipe_width)
        self._polyline(p, [(52, 228), (52, 270), (64, 270), (64, 350), (103, 350)], pipe, pipe_width)

        # Montante derecho: FT-124, CV-103 y FT-101.
        self._polyline(p, [(456, 66), (456, 91)], pipe, pipe_width)
        self._polyline(p, [(456, 113), (456, 197)], pipe, pipe_width)
        self._polyline(p, [(456, 228), (456, 350), (389, 350)], pipe, pipe_width)

        # Ramal izquierdo CV-101 / FT-121 -> T-101.
        self._polyline(p, [(52, 156), (160, 156), (160, 179)], pipe, pipe_width)
        # Ramal derecho CV-102 / FT-123 -> T-102.
        self._polyline(p, [(456, 156), (360, 156), (360, 179)], pipe, pipe_width)

        # Descarga por gravedad: T-103 -> T-101 y T-104 -> T-102.
        self._polyline(p, [(160, 129), (160, 179)], pipe, pipe_width)
        self._polyline(p, [(343, 129), (343, 179)], pipe, pipe_width)

        # Descargas de T-101 y T-102 hacia el depósito inferior TK-105.
        self._polyline(p, [(160, 261), (160, 307)], pipe, pipe_width)
        self._polyline(p, [(343, 261), (343, 307)], pipe, pipe_width)

        # ---------------------------------------------------------------
        # Equipos: primero se dibujan los recipientes encima de las tuberías.
        # El orden de datos del programa es [T-101, T-102, T-103, T-104].
        self._draw_tank(p, 126, 48, 69, 81, "T-103", 2)
        self._draw_tank(p, 309, 48, 69, 81, "T-104", 3)
        self._draw_tank(p, 126, 180, 69, 81, "T-101", 0)
        self._draw_tank(p, 309, 180, 69, 81, "T-102", 1)
        self._draw_reservoir(p)

        # Flechas que muestran el sentido de circulación.
        self._arrow(p, 160, 37, 160, 46, pipe)
        self._arrow(p, 343, 37, 343, 46, pipe)
        self._arrow(p, 160, 166, 160, 177, pipe)
        self._arrow(p, 360, 166, 360, 177, pipe)
        self._arrow(p, 160, 294, 160, 306, pipe)
        self._arrow(p, 343, 294, 343, 306, pipe)

        # ---------------------------------------------------------------
        # Líderes de los instrumentos de nivel y temperatura.
        # T-103
        self._polyline(p, [(195, 61), (211, 61), (211, 49)], pipe, 1.0)
        self._line(p, 195, 88, 204, 88, pipe, 1.0)
        # T-104
        self._polyline(p, [(378, 61), (394, 61), (394, 49)], pipe, 1.0)
        self._line(p, 378, 88, 388, 88, pipe, 1.0)
        # T-101
        self._polyline(p, [(195, 194), (203, 194), (203, 181)], pipe, 1.0)
        self._line(p, 195, 234, 204, 234, pipe, 1.0)
        # T-102
        self._polyline(p, [(378, 194), (388, 194), (388, 181)], pipe, 1.0)
        self._line(p, 378, 234, 388, 234, pipe, 1.0)

        # Instrumentos LT y TT en cada tanque.
        self._instrument(p, 211, 37, "LT", "103")
        self._instrument(p, 216, 88, "TT", "103")
        self._instrument(p, 394, 37, "LT", "104")
        self._instrument(p, 400, 88, "TT", "104")
        self._instrument(p, 211, 181, "LT", "101")
        self._instrument(p, 216, 234, "TT", "101")
        self._instrument(p, 400, 181, "LT", "102")
        self._instrument(p, 400, 234, "TT", "102")

        # Caudalímetros. Hay cuatro señales de caudal disponibles en el modelo;
        # FT-122/FT-101 comparten la señal izquierda y FT-124/FT-101 la derecha.
        self._instrument(p, 52, 54, "FT", "122", radius=12)
        self._instrument(p, 456, 54, "FT", "124", radius=12)
        self._instrument(p, 52, 214, "FT", "101", radius=12)
        self._instrument(p, 456, 214, "FT", "101", radius=12)
        self._instrument(p, 111, 156, "FT", "121", radius=12)
        self._instrument(p, 382, 142, "FT", "123", radius=12)
        self._line(p, 382, 154, 382, 156, pipe, 1.0)

        # Válvulas según la ubicación de la referencia.
        self._valve_vertical(p, 52, 103, "CV-104", self.owner.valves[3], side="left")
        self._valve_vertical(p, 456, 103, "CV-103", self.owner.valves[2], side="right")
        self._valve_horizontal(p, 82, 156, "CV-101", self.owner.valves[0], label_x=66, label_y=173)
        self._valve_horizontal(p, 426, 156, "CV-102", self.owner.valves[1], label_x=410, label_y=143)

        # Bombas y estados.
        self._draw_pump(p, 64, 283, "P-101", self.owner.pumps[0], label_side="right")
        self._draw_pump(p, 456, 283, "P-102", self.owner.pumps[1], label_side="right")

        # LT-105: lectura dinámica del depósito principal TK-105.
        self._line(p, 389, 314, 389, 320, pipe, 1.0)
        self._instrument(p, 389, 302, "LT", "105")
        lt105_value = f"{self.owner.level_tk105:.1f} %" if self.owner.comms_ok else "---"
        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 5.7, QFont.Weight.DemiBold))
        p.drawText(QRectF(400, 296, 48, 12), Qt.AlignmentFlag.AlignLeft, lt105_value)

        # Leyenda inferior compacta.
        p.setPen(QColor("#657178"))
        p.setFont(QFont("Segoe UI", 6.3))
        p.drawText(QRectF(8, 374, 501, 11), Qt.AlignmentFlag.AlignCenter,
                   "LT: nivel  |  TT: temperatura  |  FT: caudal  |  LT-105: nivel de TK-105")
        p.end()

    # ------------------------------------------------------------------
    # Funciones de dibujo
    @staticmethod
    def _line(p, x1, y1, x2, y2, color, width=1.4):
        p.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    @staticmethod
    def _polyline(p, points, color, width=1.4):
        if not points:
            return
        path = QPainterPath(QPointF(points[0][0], points[0][1]))
        for x, y in points[1:]:
            path.lineTo(x, y)
        p.setPen(QPen(color, width, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(path)

    @staticmethod
    def _arrow(p, x1, y1, x2, y2, color):
        p.save()
        p.setPen(QPen(color, 1.25))
        p.setBrush(QBrush(color))
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        angle = math.atan2(y2 - y1, x2 - x1)
        size = 5
        pts = [QPointF(x2, y2),
               QPointF(x2 - size * math.cos(angle - 0.55), y2 - size * math.sin(angle - 0.55)),
               QPointF(x2 - size * math.cos(angle + 0.55), y2 - size * math.sin(angle + 0.55))]
        p.drawPolygon(QPolygonF(pts))
        p.restore()

    def _draw_tank(self, p, x, y, w, h, tag, idx):
        # Fondo blanco para tapar los segmentos de tubería que pasan por el tanque.
        p.setPen(QPen(QColor("#202629"), 1.35))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawRect(QRectF(x, y, w, h))

        level = self.owner.levels[idx] if self.owner.comms_ok else None
        temp = self.owner.temperatures[idx] if self.owner.comms_ok else None
        if level is not None:
            level = max(0.0, min(100.0, float(level)))
            water_h = (h - 2) * level / 100.0
            surface_y = y + h - 1 - water_h
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(143, 200, 232, 95)))
            p.drawRect(QRectF(x + 1, surface_y, w - 2, water_h))
            p.setPen(QPen(QColor("#426d82"), 0.9))
            p.drawLine(QPointF(x + 1, surface_y), QPointF(x + w - 1, surface_y))
            # Ondas cortas, similares a las de la imagen de referencia.
            p.setPen(QPen(QColor("#426d82"), 0.65))
            for j, span in enumerate((w * 0.60, w * 0.42, w * 0.28)):
                yy = min(y + h - 9, surface_y + 7 + j * 4)
                if yy < y + h - 3:
                    left = x + (w - span) / 2
                    p.drawLine(QPointF(left, yy), QPointF(left + span, yy))

        # Repasar el contorno después del llenado.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#202629"), 1.35))
        p.drawRect(QRectF(x, y, w, h))

        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 6.5, QFont.Weight.DemiBold))
        p.drawText(QRectF(x + 1, y + 7, w - 2, 12), Qt.AlignmentFlag.AlignCenter, tag)
        p.setFont(QFont("Segoe UI", 5.4))
        nivel_txt = "N/D" if level is None else f"{level:.1f} %"
        temp_txt = "N/D" if temp is None else f"{temp:.1f} °C"
        p.drawText(QRectF(x + 1, y + h - 19, w - 2, 8), Qt.AlignmentFlag.AlignCenter,
                   f"L: {nivel_txt}")
        p.drawText(QRectF(x + 1, y + h - 10, w - 2, 8), Qt.AlignmentFlag.AlignCenter,
                   f"T: {temp_txt}")

    def _draw_reservoir(self, p):
        """Dibuja TK-105 y representa el nivel real/simulado de LT-105."""
        x, y, w, h = 103, 320, 286, 62
        level = float(self.owner.level_tk105) if self.owner.comms_ok else None

        # Agua: el porcentaje se representa como altura relativa del depósito.
        if level is not None:
            level = max(0.0, min(100.0, level))
            water_h = (h - 2) * level / 100.0
            surface_y = y + h - 1 - water_h
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(143, 200, 232, 105)))
            p.drawRect(QRectF(x + 1, surface_y, w - 2, water_h))
            p.setPen(QPen(QColor("#426d82"), 0.85))
            p.drawLine(QPointF(x + 2, surface_y), QPointF(x + w - 2, surface_y))
            for span in (w * 0.65, w * 0.42, w * 0.25):
                yy = min(y + h - 5, surface_y + 5 + (w * 0.65 - span) / 22)
                if yy < y + h - 2:
                    left = x + (w - span) / 2
                    p.drawLine(QPointF(left, yy), QPointF(left + span, yy))

        # Depósito abierto en la parte superior.
        p.setPen(QPen(QColor("#202629"), 1.35))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(x, y), QPointF(x, y + h))
        p.drawLine(QPointF(x, y + h), QPointF(x + w, y + h))
        p.drawLine(QPointF(x + w, y), QPointF(x + w, y + h))

        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 6.2, QFont.Weight.DemiBold))
        p.drawText(QRectF(190, 325, 112, 12), Qt.AlignmentFlag.AlignCenter, "TK-105")
        p.setFont(QFont("Segoe UI", 5.5))
        value = "N/D" if level is None else f"LT-105: {level:.1f} %"
        p.drawText(QRectF(190, 337, 112, 10), Qt.AlignmentFlag.AlignCenter, value)

    def _instrument(self, p, cx, cy, kind, number, radius=10.5):
        p.save()
        p.setPen(QPen(QColor("#202629"), 1.0))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(cx, cy), radius, radius)
        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 5.5, QFont.Weight.DemiBold))
        p.drawText(QRectF(cx - radius + 1, cy - radius + 2,
                          2 * radius - 2, 2 * radius - 3),
                   Qt.AlignmentFlag.AlignCenter, f"{kind}\n{number}")
        p.restore()

    def _valve_vertical(self, p, x, y, tag, opened, side="left"):
        color = QColor("#2f7d5b") if opened else QColor("#c42b1c")
        p.save()
        p.setPen(QPen(color, 1.1))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawPolygon(QPolygonF([QPointF(x - 5, y - 6), QPointF(x, y), QPointF(x + 5, y - 6)]))
        p.drawPolygon(QPolygonF([QPointF(x - 5, y + 6), QPointF(x, y), QPointF(x + 5, y + 6)]))
        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 5.2))
        if side == "left":
            rect = QRectF(4, y - 4, 35, 10)
            align = Qt.AlignmentFlag.AlignLeft
        else:
            rect = QRectF(463, y - 4, 48, 10)
            align = Qt.AlignmentFlag.AlignLeft
        p.drawText(rect, align, tag)
        p.restore()

    def _valve_horizontal(self, p, x, y, tag, opened, label_x, label_y):
        color = QColor("#2f7d5b") if opened else QColor("#c42b1c")
        p.save()
        p.setPen(QPen(color, 1.1))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawPolygon(QPolygonF([QPointF(x - 6, y - 5), QPointF(x, y), QPointF(x - 6, y + 5)]))
        p.drawPolygon(QPolygonF([QPointF(x + 6, y - 5), QPointF(x, y), QPointF(x + 6, y + 5)]))
        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 5.2))
        p.drawText(QRectF(label_x, label_y, 44, 9), Qt.AlignmentFlag.AlignLeft, tag)
        p.restore()

    def _draw_pump(self, p, cx, cy, tag, running, label_side="right"):
        color = QColor("#2f7d5b") if running else QColor("#c42b1c")
        p.save()
        p.setPen(QPen(color, 1.25))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(cx, cy), 12, 12)
        p.setBrush(QBrush(color))
        # Triángulo de impulsión simplificado.
        p.drawPolygon(QPolygonF([QPointF(cx - 3, cy + 5),
                                 QPointF(cx + 4, cy),
                                 QPointF(cx - 3, cy - 5)]))
        p.setPen(QColor("#202629"))
        p.setFont(QFont("Segoe UI", 5.6))
        p.drawText(QRectF(cx + 14, cy - 3, 41, 9), Qt.AlignmentFlag.AlignLeft, tag)
        p.setPen(color)
        p.setFont(QFont("Segoe UI", 4.8, QFont.Weight.DemiBold))
        p.drawText(QRectF(cx + 14, cy + 5, 41, 8), Qt.AlignmentFlag.AlignLeft,
                   "RUN" if running else "STOP")
        p.restore()

class TankDetailCanvas(QWidget):
    """Vista ampliada del equipo para colocar a la izquierda de cada HMI."""

    W = 250
    H = 440

    def __init__(self, owner, tank_index):
        super().__init__()
        self.owner = owner
        self.tank_index = tank_index  # 0..3: T-101..T-104; 4: TK-105
        self.setMinimumSize(225, 405)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#f7f9fa"))

        sx = self.width() / self.W
        sy = self.height() / self.H
        scale = min(sx, sy)
        p.translate((self.width() - self.W * scale) / 2,
                    (self.height() - self.H * scale) / 2)
        p.scale(scale, scale)

        p.setPen(QPen(QColor(COLORS["border"]), 1.2))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(5, 5, 240, 430), 9, 9)

        idx = self.tank_index
        is_reservoir = idx == 4
        tag = "TK-105" if is_reservoir else self.owner.TANK_TAGS[idx]
        lt_tag = "LT-105" if is_reservoir else f"LT-{101 + idx}"
        level = (self.owner.level_tk105 if is_reservoir else self.owner.levels[idx]) if self.owner.comms_ok else None
        temp = None if is_reservoir or not self.owner.comms_ok else self.owner.temperatures[idx]

        p.setPen(QColor(COLORS["text"]))
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.drawText(QRectF(15, 18, 220, 25), Qt.AlignmentFlag.AlignCenter, tag)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(COLORS["muted"]))
        p.drawText(QRectF(15, 43, 220, 18), Qt.AlignmentFlag.AlignCenter,
                   "DEPÓSITO PRINCIPAL" if is_reservoir else "VISTA LOCAL DEL TANQUE")

        if is_reservoir:
            # Depósito abierto, representado horizontalmente.
            x, y, w, h = 28, 175, 194, 112
            if level is not None:
                water_h = (h - 4) * max(0.0, min(100.0, float(level))) / 100.0
                surface_y = y + h - 2 - water_h
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(COLORS["water"]))
                p.drawRect(QRectF(x + 2, surface_y, w - 4, water_h))
                p.setPen(QPen(QColor("#426d82"), 1.1))
                p.drawLine(QPointF(x + 3, surface_y), QPointF(x + w - 3, surface_y))
            p.setPen(QPen(QColor("#526570"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawLine(QPointF(x, y), QPointF(x, y + h))
            p.drawLine(QPointF(x, y + h), QPointF(x + w, y + h))
            p.drawLine(QPointF(x + w, y), QPointF(x + w, y + h))
            p.setPen(QColor(COLORS["text"]))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(x + 5, y + 42, w - 10, 18), Qt.AlignmentFlag.AlignCenter, "TK-105")
        else:
            # Recipiente vertical con llenado proporcional y superficie de líquido.
            x, y, w, h = 66, 82, 118, 244
            if level is not None:
                water_h = (h - 4) * max(0.0, min(100.0, float(level))) / 100.0
                surface_y = y + h - 2 - water_h
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(COLORS["water"]))
                p.drawRect(QRectF(x + 2, surface_y, w - 4, water_h))
                p.setPen(QPen(QColor("#426d82"), 1.1))
                p.drawLine(QPointF(x + 3, surface_y), QPointF(x + w - 3, surface_y))
                p.setPen(QPen(QColor("#426d82"), 0.8))
                for j, span in enumerate((w * 0.66, w * 0.46, w * 0.28)):
                    yy = min(y + h - 13, surface_y + 10 + j * 6)
                    if yy < y + h - 5:
                        left = x + (w - span) / 2
                        p.drawLine(QPointF(left, yy), QPointF(left + span, yy))
            p.setPen(QPen(QColor("#526570"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(x, y, w, h), 7, 7)
            p.setPen(QColor(COLORS["text"]))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(x + 4, y + 10, w - 8, 18), Qt.AlignmentFlag.AlignCenter, tag)
            # Burbujas LT y TT y sus líneas de conexión.
            p.setPen(QPen(QColor(COLORS["pipe"]), 1.2))
            p.drawLine(QPointF(x + w, y + 42), QPointF(205, y + 42))
            p.drawLine(QPointF(x + w, y + 176), QPointF(205, y + 176))
            self._bubble(p, 218, y + 42, "LT", str(101 + idx))
            self._bubble(p, 218, y + 176, "TT", str(101 + idx))

        # Lecturas numéricas a la izquierda del tanque.
        p.setPen(QColor(COLORS["text"]))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        level_text = "---" if level is None else f"{level:.1f} %"
        p.drawText(QRectF(15, 315 if is_reservoir else 340, 220, 22),
                   Qt.AlignmentFlag.AlignCenter, f"{lt_tag}: {level_text}")
        if not is_reservoir:
            temp_text = "---" if temp is None else f"{temp:.1f} °C"
            p.setFont(QFont("Segoe UI", 9))
            p.drawText(QRectF(15, 365, 220, 20), Qt.AlignmentFlag.AlignCenter,
                       f"TT-{101 + idx}: {temp_text}")
            p.setPen(QColor(COLORS["muted"]))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(15, 391, 220, 18), Qt.AlignmentFlag.AlignCenter,
                       f"Consigna: {self.owner.setpoints[idx]:.1f} %")
            p.drawText(QRectF(15, 410, 220, 16), Qt.AlignmentFlag.AlignCenter,
                       f"Salida: {self.owner.outputs[idx]:.1f} %")
        else:
            p.setPen(QColor(COLORS["muted"]))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(QRectF(15, 348, 220, 22), Qt.AlignmentFlag.AlignCenter,
                       "Depósito de recirculación")

        if not self.owner.comms_ok:
            p.setPen(QColor(COLORS["quality"]))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(15, 65, 220, 15), Qt.AlignmentFlag.AlignCenter,
                       "DATOS NO CONFIABLES")
        elif getattr(self.owner, "emergency_stopped", False):
            p.setPen(QColor(COLORS["alarm"]))
            p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            p.drawText(QRectF(15, 65, 220, 15), Qt.AlignmentFlag.AlignCenter,
                       "PARADA SOFTWARE ACTIVA")
        p.end()

    @staticmethod
    def _bubble(p, cx, cy, kind, number):
        p.save()
        p.setPen(QPen(QColor("#526570"), 1.1))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(cx, cy), 12, 12)
        p.setPen(QColor(COLORS["text"]))
        p.setFont(QFont("Segoe UI", 5.8, QFont.Weight.Bold))
        p.drawText(QRectF(cx - 10, cy - 7, 20, 14), Qt.AlignmentFlag.AlignCenter,
                   f"{kind}\n{number}")
        p.restore()


class FallbackTrendWidget(QWidget):
    """Gráfico QPainter de respaldo cuando PyQtGraph no está instalado."""
    def __init__(self):
        super().__init__()
        self.series = {}
        self.title = "Tendencia"
        self.unit = ""
        self.setMinimumHeight(360)

    def set_series(self, series, title, unit):
        self.series, self.title, self.unit = series, title, unit
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("white"))
        plot = QRectF(70, 45, max(100, self.width() - 100), max(100, self.height() - 100))
        p.setPen(QColor(COLORS["text"]))
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, 8, self.width(), 25), Qt.AlignmentFlag.AlignCenter, self.title)
        values = [v for points in self.series.values() for v in points]
        if not values:
            p.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Esperando datos…")
            return
        ymin, ymax = min(values), max(values)
        if abs(ymax - ymin) < 1e-6:
            ymax += 1.0; ymin -= 1.0
        margin = (ymax - ymin) * 0.12
        ymin -= margin; ymax += margin
        p.setPen(QPen(QColor("#dfe5e8"), 1))
        for i in range(6):
            y = plot.top() + plot.height() * i / 5
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        p.setPen(QPen(QColor("#7a8991"), 1.2))
        p.drawRect(plot)
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(4, plot.top(), 60, 16), Qt.AlignmentFlag.AlignRight, f"{ymax:.1f}")
        p.drawText(QRectF(4, plot.bottom() - 16, 60, 16), Qt.AlignmentFlag.AlignRight, f"{ymin:.1f}")
        p.drawText(QRectF(plot.left(), plot.bottom() + 14, plot.width(), 18), Qt.AlignmentFlag.AlignCenter, "Muestras recientes")
        for idx, (name, points) in enumerate(self.series.items()):
            if len(points) < 2:
                continue
            p.setPen(QPen(QColor(SERIES_COLORS[idx % len(SERIES_COLORS)]), 2))
            path = QPainterPath()
            for j, value in enumerate(points):
                xx = plot.left() + plot.width() * j / max(1, len(points) - 1)
                yy = plot.bottom() - ((value - ymin) / (ymax - ymin)) * plot.height()
                if j == 0: path.moveTo(xx, yy)
                else: path.lineTo(xx, yy)
            p.drawPath(path)
        legend_x, legend_y = int(plot.left()), int(self.height() - 28)
        for idx, name in enumerate(self.series.keys()):
            x = legend_x + idx * max(120, int(plot.width() / max(1, len(self.series))))
            p.setPen(QPen(QColor(SERIES_COLORS[idx % len(SERIES_COLORS)]), 3))
            p.drawLine(x, legend_y + 6, x + 18, legend_y + 6)
            p.setPen(QColor(COLORS["text"]))
            p.drawText(x + 23, legend_y + 10, name)


class SCADA:
    TANK_TAGS = ["T-101", "T-102", "T-103", "T-104"]
    MODE_OPTIONS = ["PID", "ON/OFF", "Manual"]

    def __init__(self):
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SCADA.Tanques.Academico")
            except Exception:
                pass
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setApplicationName("SCADA académico | Cuatro tanques + TK-105")
        if ICON_PATH.exists():
            self.icon = QtGui.QIcon(str(ICON_PATH))
            self.app.setWindowIcon(self.icon)
        else:
            self.icon = self.app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.app.setWindowIcon(self.icon)

        self.log = AuditLogger(AUDIT_PATH)
        role, accepted = QInputDialog.getItem(
            None, "Acceso al SCADA", "Seleccione el rol de usuario:",
            ["estudiante", "operario", "invitado"], 0, False)
        self.role = role if accepted else "invitado"
        self.read_only = self.role == "invitado"
        self.log("LOGIN", f"Inicio de sesión: {self.role}", self.role)

        if not UI_PATH.exists():
            raise FileNotFoundError(f"No se encontró el archivo Qt Designer:\n{UI_PATH}")
        self.ui = self._load_ui(UI_PATH)
        self.ui.setWindowIcon(self.icon)
        self._bind_ui()
        self._setup_title_bar()
        self._style_ui()

        # Estado del proceso. Valores iniciales solo para entrenamiento.
        self.levels = [52.0, 46.0, 39.0, 34.0]  # T-101…T-104, %
        self.level_tk105 = 65.0  # TK-105 / LT-105, %; depósito principal de recirculación
        self.temperatures = [24.5, 25.0, 24.0, 24.8]  # °C, T-101…T-104
        self.flows = [0.8, 0.7, 0.4, 0.35]  # L/min, FT-101…FT-104
        self.setpoints = [50.0, 45.0, 40.0, 35.0]
        self.modes = ["PID", "PID", "PID", "PID"]
        self.manual_outputs = [30.0, 30.0, 25.0, 25.0]
        self.outputs = [30.0, 30.0, 25.0, 25.0]
        self.integrals = [0.0] * 4
        self.prev_errors = [0.0] * 4
        # Ganancias configurables e independientes por tanque.
        self.pid_gains = {"Kp": [2.6] * 4, "Ki": [0.035] * 4, "Kd": [0.08] * 4}
        self.pumps = [True, True]
        self.valves = [True, True, True, True]
        # Enclavamiento de parada por software (no sustituye un E-Stop físico).
        self.emergency_stopped = False
        self.emergency_stop_timestamp = None
        self.emergency_command_status = "Sin solicitud de paro"
        self.remote_estop_feedback = None
        self.comms_mode = "SIM"
        self.comms_ok = True
        self.sim_fault = False
        self.modbus_client = None
        self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_status_message = "MQTT sin conexión"
        self.mqtt_last_rx = None
        self.mqtt_snapshot = None
        self._closing = False
        self._active_alarms = {}
        self._acked_alarms = set()
        self._shelved_alarms = {}
        self._visible_alarm_codes = []
        self.scan_count = 0
        self.history_length = 180
        self.level_history = [deque([v] * 15, maxlen=self.history_length) for v in self.levels]
        self.level_history.append(deque([self.level_tk105] * 15, maxlen=self.history_length))
        self.temp_history = [deque([v] * 15, maxlen=self.history_length) for v in self.temperatures]
        self.flow_history = [deque([v] * 15, maxlen=self.history_length) for v in self.flows]
        self.time_history = deque(range(-14, 1), maxlen=self.history_length)

        self._build_overview()
        self._build_control_page()
        self._build_trends_page()
        self._build_instrumentation_page()
        self._build_communication_page()
        self._build_student_page()
        self._build_tank_pages()
        self._connect_signals()
        self._apply_role_permissions()
        self._update_alarm_list()
        self.refresh_ui()

        self.scan_timer = QTimer(self.ui)
        self.scan_timer.timeout.connect(self.scan_process)
        self.scan_timer.start(1000)
        self.ui_timer = QTimer(self.ui)
        self.ui_timer.timeout.connect(self.refresh_ui)
        self.ui_timer.start(500)

    # ----------------------------- UI / carga
    def _load_ui(self, path: Path):
        loader = QUiLoader()
        file = QFile(str(path))
        if not file.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError(f"No se pudo abrir el archivo UI: {path}")
        try:
            widget = loader.load(file)
        finally:
            file.close()
        if widget is None:
            raise RuntimeError("Qt no pudo cargar ui/main_window.ui. Revise el XML en Qt Designer.")
        return widget

    def get(self, cls, name):
        obj = self.ui.findChild(cls, name)
        if obj is None:
            raise RuntimeError(f"No se encontró el widget '{name}' en ui/main_window.ui")
        return obj

    @staticmethod
    def _ensure_layout(widget, layout_cls=QVBoxLayout):
        layout = widget.layout()
        if layout is None:
            layout = layout_cls(widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
        return layout

    def _bind_ui(self):
        self.stacked = self.get(QStackedWidget, "stackedWidget")
        self.menu = self.get(QFrame, "menu")
        self.header = self.get(QFrame, "header")
        self.main_title = self.get(QLabel, "mainTitle")
        self.role_label = self.get(QLabel, "roleLabel")
        self.clock = self.get(QLabel, "clock")
        self.role_info = self.get(QLabel, "roleInfo")
        self.overview_metrics = self.get(QWidget, "overviewMetrics")
        self.pid_container = self.get(QWidget, "pidContainer")
        self.overview_advice = self.get(QLabel, "overviewAdvice")
        self.control_container = self.get(QWidget, "controlContainer")
        self.trend_controls = self.get(QWidget, "trendControls")
        self.plot_container = self.get(QWidget, "plotContainer")
        self.alarm_table = self.get(QTableWidget, "alarmTable")
        self.alarm_summary = self.get(QLabel, "alarmSummary")
        self.instrument_container = self.get(QWidget, "instrumentContainer")
        self.comm_container = self.get(QWidget, "commContainer")
        self.student_container = self.get(QWidget, "studentContainer")
        self.buttons = {
            "overview": self.get(QPushButton, "btnOverview"),
            "control": self.get(QPushButton, "btnControl"),
            "trends": self.get(QPushButton, "btnTrends"),
            "alarms": self.get(QPushButton, "btnAlarms"),
            "instrumentation": self.get(QPushButton, "btnInstrumentation"),
            "communication": self.get(QPushButton, "btnCommunication"),
            "student": self.get(QPushButton, "btnStudentGuide"),
            "comm_fault": self.get(QPushButton, "btnCommFault"),
            "ack_all": self.get(QPushButton, "btnAckAll"),
            "shelve": self.get(QPushButton, "btnShelve"),
            "minimize": self.get(QPushButton, "btnMinimize"),
            "maximize": self.get(QPushButton, "btnMaximize"),
            "close": self.get(QPushButton, "btnClose"),
        }

    def _setup_title_bar(self):
        self.ui.setWindowFlags(self.ui.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.title_bar_controller = TitleBarController(self.ui, self.buttons["maximize"] if hasattr(self, "buttons") else self.get(QPushButton, "btnMaximize"))
        # Selector permanente: permite cambiar de perfil sin cerrar ni reiniciar el SCADA.
        header_layout = self.header.layout()
        self.role_selector_label = QLabel("Perfil:")
        self.role_selector_label.setStyleSheet("color:#dbe3e7;font-size:9pt;padding-left:6px;")
        self.role_selector = QComboBox()
        self.role_selector.addItems(["estudiante", "operario", "invitado"])
        self.role_selector.setCurrentText(self.role)
        self.role_selector.setMinimumWidth(105)
        self.role_selector.setToolTip("Perfil académico de demostración; no sustituye autenticación real de usuarios.")
        header_layout.insertWidget(2, self.role_selector_label)
        header_layout.insertWidget(3, self.role_selector)
        for widget in (self.header, self.main_title, self.clock, self.role_label):
            widget.installEventFilter(self.title_bar_controller)
        self.buttons["minimize"].clicked.connect(self.ui.showMinimized)
        self.buttons["maximize"].clicked.connect(self.title_bar_controller.toggle_maximize)
        self.buttons["close"].clicked.connect(self.request_close)

    def _style_ui(self):
        self.ui.setStyleSheet(f"""
        QWidget {{ color: {COLORS['text']}; font-family: 'Segoe UI'; font-size: 10pt; }}
        QMainWindow, QWidget#centralWidget {{ background: {COLORS['bg']}; }}
        QFrame#header {{ background: {COLORS['header']}; border: 0; }}
        QLabel#mainTitle {{ color: white; font-size: 16pt; font-weight: 700; }}
        QLabel#roleLabel {{ color: #c9d5db; font-size: 9pt; padding: 6px; }}
        QLabel#clock {{ color: #e1e8eb; font-size: 9pt; padding: 6px; }}
        QPushButton#btnMinimize, QPushButton#btnMaximize, QPushButton#btnClose {{ color: white; background: transparent; border: 0; min-width: 34px; max-width: 34px; min-height: 28px; }}
        QPushButton#btnMinimize:hover, QPushButton#btnMaximize:hover {{ background: #45545d; }}
        QPushButton#btnClose:hover {{ background: {COLORS['alarm']}; }}
        QFrame#menu {{ background: {COLORS['menu']}; border: 0; }}
        QLabel#menuTitle {{ color: #ccd6da; font-weight: 700; padding: 4px 8px; }}
        QLabel#roleInfo {{ color: #dbe3e7; padding: 8px; background: #2b373d; border-radius: 6px; }}
        QFrame#menu QPushButton {{ color: #edf2f4; background: transparent; border: 0; text-align: left; padding: 11px 9px; border-radius: 5px; }}
        QFrame#menu QPushButton:hover {{ background: #45545d; }}
        QFrame#menu QPushButton:disabled {{ color: #88969d; }}
        QLabel#sectionTitle, QLabel#controlTitle, QLabel#trendsTitle, QLabel#alarmsTitle,
        QLabel#instrumentationTitle, QLabel#communicationTitle, QLabel#studentTitle {{ font-size: 19pt; font-weight: 700; color: {COLORS['text']}; }}
        QLabel#subtitle, QLabel#controlSubtitle, QLabel#trendsSubtitle, QLabel#instrumentationSubtitle, QLabel#communicationSubtitle {{ color: {COLORS['muted']}; }}
        QGroupBox {{ background: white; border: 1px solid {COLORS['border']}; border-radius: 8px; margin-top: 12px; padding: 10px; font-weight: 700; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 5px; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: white; border: 1px solid #b8c5cc; border-radius: 5px; padding: 5px; min-height: 24px; }}
        QPushButton {{ background: #e4ebef; border: 1px solid #c4d0d6; border-radius: 5px; padding: 7px 9px; }}
        QPushButton:hover {{ background: #d6e3e9; }}
        QPushButton:disabled {{ color: #87939a; background: #eceff1; }}
        QTableWidget {{ background: white; alternate-background-color: #f6f8f9; gridline-color: #e0e6e9; border: 1px solid {COLORS['border']}; border-radius: 5px; }}
        QHeaderView::section {{ background: #e8eef1; padding: 7px; border: 0; font-weight: 700; }}
        QLabel#overviewAdvice, QLabel#alarmSummary {{ background: white; border: 1px solid {COLORS['border']}; border-radius: 6px; padding: 8px; }}
        """)

    def _build_tank_pages(self):
        """Construye una HMI dedicada por tanque, reutilizando el estado común del SCADA."""
        self.tank_page_widgets = []
        self.tank_page_indices = []
        self.tank_nav_buttons = []
        menu_layout = self.menu.layout()
        nav_title = QLabel("HMI POR TANQUE")
        nav_title.setStyleSheet("color:#ccd6da;font-weight:700;padding:8px 8px 2px 8px;")
        menu_layout.insertWidget(8, nav_title)

        pump_for_tank = [0, 1, 1, 0]
        valve_for_tank = [0, 1, 2, 3]
        flow_descriptions = [
            "FT-101 (descarga principal P-101)",
            "FT-102 (descarga principal P-102)",
            "FT-103 (rama P-102 → T-103)",
            "FT-104 (rama P-101 → T-104)",
        ]
        for i, tag in enumerate(self.TANK_TAGS):
            button = QPushButton(f"{tag} · Detalle")
            button.setMinimumHeight(32)
            menu_layout.insertWidget(9 + i, button)
            self.tank_nav_buttons.append(button)

            page = QWidget()
            root = QVBoxLayout(page)
            root.setContentsMargins(18, 16, 18, 12)
            root.setSpacing(8)

            header_row = QHBoxLayout()
            title = QLabel(f"HMI INDIVIDUAL · {tag}")
            title.setStyleSheet(f"font-size:19pt;font-weight:700;color:{COLORS['text']};")
            status = QLabel("COMPROBANDO ESTADO…")
            status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status.setMinimumWidth(180)
            status.setStyleSheet("background:#e8eef1;border:1px solid #cfd8dc;border-radius:5px;padding:7px;font-weight:700;")
            header_row.addWidget(title)
            header_row.addStretch(1)
            header_row.addWidget(status)
            root.addLayout(header_row)

            subtitle = QLabel(
                f"Supervisión de LT-{101+i}, TT-{101+i} y {flow_descriptions[i]}; "
                "las medidas son valores de simulación o telemetría según el modo de adquisición."
            )
            subtitle.setWordWrap(True)
            subtitle.setStyleSheet(f"color:{COLORS['muted']};")
            root.addWidget(subtitle)

            # Zona central: equipo ampliado a la izquierda y datos/mandos a la derecha.
            content_row = QHBoxLayout()
            content_row.setSpacing(12)

            tank_group = QGroupBox(f"Equipo · {tag}")
            tank_group.setMinimumWidth(245)
            tank_group.setMaximumWidth(295)
            tank_group_layout = QVBoxLayout(tank_group)
            tank_canvas = TankDetailCanvas(self, i)
            tank_group_layout.addWidget(tank_canvas, 1)
            content_row.addWidget(tank_group, 0)

            right_scroll = QScrollArea()
            right_scroll.setWidgetResizable(True)
            right_scroll.setFrameShape(QFrame.Shape.NoFrame)
            right_panel = QWidget()
            right_layout = QVBoxLayout(right_panel)
            right_layout.setContentsMargins(2, 2, 2, 2)
            right_layout.setSpacing(8)
            right_scroll.setWidget(right_panel)
            content_row.addWidget(right_scroll, 1)
            root.addLayout(content_row, 1)

            metric_row = QHBoxLayout()
            metric_labels = {}
            metric_specs = [
                ("NIVEL", "level", "%"),
                ("TEMPERATURA", "temperature", "°C"),
                ("CAUDAL ASOCIADO", "flow", "L/min"),
                ("CONSIGNA", "setpoint", "%"),
                ("SALIDA", "output", "%"),
            ]
            for caption, key, unit in metric_specs:
                card = QFrame()
                card.setStyleSheet(f"QFrame {{background:white;border:1px solid {COLORS['border']};border-radius:7px;}}")
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(9, 8, 9, 8)
                lab_caption = QLabel(caption)
                lab_caption.setStyleSheet(f"color:{COLORS['muted']};font-size:8pt;font-weight:700;border:0;")
                lab_value = QLabel(f"--- {unit}")
                lab_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lab_value.setStyleSheet("font-size:17pt;font-weight:700;border:0;")
                card_layout.addWidget(lab_caption)
                card_layout.addWidget(lab_value)
                metric_row.addWidget(card, 1)
                metric_labels[key] = lab_value
            right_layout.addLayout(metric_row)

            control_group = QGroupBox(f"Control de nivel · LIC-{101+i}")
            control_layout = QGridLayout(control_group)
            mode = QComboBox()
            mode.addItems(self.MODE_OPTIONS)
            mode.setCurrentText(self.modes[i])
            mode.currentTextChanged.connect(lambda text, idx=i: self._set_mode(idx, text))
            setpoint = QDoubleSpinBox()
            setpoint.setRange(0, 100); setpoint.setDecimals(1); setpoint.setSuffix(" %")
            setpoint.setValue(self.setpoints[i]); setpoint.setKeyboardTracking(False)
            setpoint.valueChanged.connect(lambda value, idx=i: self._set_setpoint(idx, value))
            manual = QDoubleSpinBox()
            manual.setRange(0, 100); manual.setDecimals(1); manual.setSuffix(" %")
            manual.setValue(self.manual_outputs[i]); manual.setKeyboardTracking(False)
            manual.setEnabled(False)
            manual.valueChanged.connect(lambda value, idx=i: self._set_manual_output(idx, value))
            gains = {}
            for key, step in (("Kp", 0.1), ("Ki", 0.01), ("Kd", 0.01)):
                gain = QDoubleSpinBox()
                gain.setRange(0.0, 100.0 if key == "Ki" else 1000.0)
                gain.setDecimals(4); gain.setSingleStep(step); gain.setKeyboardTracking(False)
                gain.setValue(self.pid_gains[key][i])
                gain.setToolTip(f"Ganancia {key} del PID de {tag}. Solo modifica el modelo local en SIM; el control remoto requiere mapear estas variables en el PLC/ESP32.")
                gain.valueChanged.connect(lambda value, idx=i, gain_key=key: self._set_pid_gain(idx, gain_key, value))
                gains[key] = gain
            output_value = QLabel("--- %")
            error_value = QLabel("--- %")
            for label in (output_value, error_value):
                label.setStyleSheet("background:#f7f9fa;border:1px solid #dfe5e8;border-radius:4px;padding:5px;")
            control_layout.addWidget(QLabel("Modo"), 0, 0)
            control_layout.addWidget(mode, 0, 1)
            control_layout.addWidget(QLabel("Consigna"), 0, 2)
            control_layout.addWidget(setpoint, 0, 3)
            control_layout.addWidget(QLabel("Salida manual"), 1, 0)
            control_layout.addWidget(manual, 1, 1)
            control_layout.addWidget(QLabel("Salida calculada"), 1, 2)
            control_layout.addWidget(output_value, 1, 3)
            control_layout.addWidget(QLabel("Kp"), 2, 0)
            control_layout.addWidget(gains["Kp"], 2, 1)
            control_layout.addWidget(QLabel("Ki"), 2, 2)
            control_layout.addWidget(gains["Ki"], 2, 3)
            control_layout.addWidget(QLabel("Kd"), 2, 4)
            control_layout.addWidget(gains["Kd"], 2, 5)
            control_layout.addWidget(QLabel("Error SP − PV"), 3, 0, 1, 2)
            control_layout.addWidget(error_value, 3, 2)
            control_layout.setColumnStretch(1, 1)
            control_layout.setColumnStretch(3, 1)
            control_layout.setColumnStretch(5, 1)
            right_layout.addWidget(control_group)

            actuator_group = QGroupBox("Actuadores relacionados")
            actuator_layout = QHBoxLayout(actuator_group)
            pump_button = QPushButton("P-101: RUN" if self.pumps[pump_for_tank[i]] else "P-101: STOP")
            valve_button = QPushButton("V-101: ABIERTA" if self.valves[valve_for_tank[i]] else "V-101: CERRADA")
            pump_button.setMinimumHeight(38); valve_button.setMinimumHeight(38)
            pump_button.clicked.connect(lambda checked=False, pump_idx=pump_for_tank[i]: self.toggle_pump(pump_idx))
            valve_button.clicked.connect(lambda checked=False, valve_idx=valve_for_tank[i]: self.toggle_valve(valve_idx))
            actuator_layout.addWidget(QLabel(f"Bomba P-{101+pump_for_tank[i]}"))
            actuator_layout.addWidget(pump_button, 1)
            actuator_layout.addSpacing(12)
            actuator_layout.addWidget(QLabel(f"Válvula V-{101+valve_for_tank[i]}"))
            actuator_layout.addWidget(valve_button, 1)
            right_layout.addWidget(actuator_group)

            alarm_group = QGroupBox(f"Alarmas activas · {tag}")
            alarm_layout = QVBoxLayout(alarm_group)
            alarm_table = QTableWidget(0, 4)
            alarm_table.setHorizontalHeaderLabels(["Prioridad", "Alarma / condición", "Estado", "Hora"])
            alarm_table.verticalHeader().setVisible(False)
            alarm_table.setAlternatingRowColors(True)
            alarm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            alarm_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            alarm_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            alarm_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            alarm_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            alarm_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            alarm_table.setMinimumHeight(128)
            alarm_layout.addWidget(alarm_table)
            ack_button = QPushButton(f"Reconocer alarmas de {tag}")
            ack_button.clicked.connect(lambda checked=False, tank_idx=i: self._ack_tank_alarms(tank_idx))
            alarm_layout.addWidget(ack_button, 0, Qt.AlignmentFlag.AlignRight)
            right_layout.addWidget(alarm_group, 1)

            note = QLabel("HMI de supervisión académica. Los enclavamientos de seguridad y la parada de emergencia deben residir en el sistema de control/seguridad apropiado, no en esta interfaz.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{COLORS['muted']};font-size:9pt;")
            right_layout.addWidget(note)

            page_index = self.stacked.addWidget(page)
            self.tank_page_indices.append(page_index)
            self.tank_page_widgets.append({
                "page": page, "title": title, "status": status, "metrics": metric_labels,
                "mode": mode, "setpoint": setpoint, "manual": manual, "gains": gains,
                "output": output_value, "error": error_value, "pump_button": pump_button,
                "valve_button": valve_button, "alarm_table": alarm_table, "ack_button": ack_button,
                "pump_idx": pump_for_tank[i], "valve_idx": valve_for_tank[i],
                "tank_canvas": tank_canvas,
            })
            self.tank_nav_buttons[i].clicked.connect(
                lambda checked=False, tank_idx=i: self.open_tank_hmi(tank_idx)
            )

        self._build_reservoir_hmi(menu_layout)
        self._refresh_tank_pages()

    def _build_reservoir_hmi(self, menu_layout):
        """HMI de supervisión de TK-105; no incorpora un quinto lazo PID."""
        self.btn_reservoir_nav = QPushButton("TK-105 · Depósito")
        self.btn_reservoir_nav.setMinimumHeight(32)
        menu_layout.insertWidget(13, self.btn_reservoir_nav)

        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(18, 16, 18, 12)
        root.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("HMI INDIVIDUAL · TK-105")
        title.setStyleSheet(f"font-size:19pt;font-weight:700;color:{COLORS['text']};")
        status = QLabel("COMPROBANDO ESTADO…")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setMinimumWidth(190)
        status.setStyleSheet("background:#e8eef1;border:1px solid #cfd8dc;border-radius:5px;padding:7px;font-weight:700;")
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(status)
        root.addLayout(header)

        subtitle = QLabel(
            "Supervisión del depósito principal de recirculación mediante LT-105. "
            "TK-105 no tiene controlador PID propio en este modelo; recibe el retorno "
            "de T-101/T-102 y suministra agua a P-101/P-102."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{COLORS['muted']};")
        root.addWidget(subtitle)

        content = QHBoxLayout()
        content.setSpacing(12)
        left_group = QGroupBox("Equipo · TK-105")
        left_group.setMinimumWidth(245)
        left_group.setMaximumWidth(295)
        left_layout = QVBoxLayout(left_group)
        canvas = TankDetailCanvas(self, 4)
        left_layout.addWidget(canvas, 1)
        content.addWidget(left_group, 0)

        right_group = QGroupBox("Variables y estado del depósito")
        right_layout = QVBoxLayout(right_group)
        level_label = QLabel("--- %")
        level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        level_label.setStyleSheet("font-size:25pt;font-weight:700;background:#f7f9fa;border:1px solid #dfe5e8;border-radius:6px;padding:18px;")
        right_layout.addWidget(QLabel("LT-105 · NIVEL DEL DEPÓSITO"))
        right_layout.addWidget(level_label)
        detail = QLabel(
            "Función: almacenamiento y recirculación.\n\n"
            "Entradas: retorno hidráulico desde T-101 y T-102.\n"
            "Salidas: aspiración de las bombas P-101 y P-102.\n\n"
            "El porcentaje es una variable normalizada del modelo académico. "
            "Para convertirlo a cm o volumen real se requiere la geometría y calibración del tanque."
        )
        detail.setWordWrap(True)
        detail.setStyleSheet(f"background:white;border:1px solid {COLORS['border']};border-radius:6px;padding:12px;")
        right_layout.addWidget(detail)

        alarm_group = QGroupBox("Alarmas activas · TK-105")
        alarm_layout = QVBoxLayout(alarm_group)
        alarm_table = QTableWidget(0, 4)
        alarm_table.setHorizontalHeaderLabels(["Prioridad", "Alarma / condición", "Estado", "Hora"])
        alarm_table.verticalHeader().setVisible(False)
        alarm_table.setAlternatingRowColors(True)
        alarm_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        alarm_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        alarm_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        alarm_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        alarm_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        alarm_table.setMinimumHeight(155)
        alarm_layout.addWidget(alarm_table)
        ack_button = QPushButton("Reconocer alarmas de TK-105")
        ack_button.clicked.connect(self._ack_reservoir_alarms)
        alarm_layout.addWidget(ack_button, 0, Qt.AlignmentFlag.AlignRight)
        right_layout.addWidget(alarm_group, 1)
        content.addWidget(right_group, 1)
        root.addLayout(content, 1)

        note = QLabel(
            "HMI de supervisión. Las alarmas y el indicador no sustituyen la protección "
            "independiente contra sobrellenado ni los dispositivos físicos de seguridad."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{COLORS['muted']};font-size:9pt;")
        root.addWidget(note)

        self.reservoir_page_index = self.stacked.addWidget(page)
        self.reservoir_page_widgets = {
            "page": page, "status": status, "canvas": canvas,
            "level": level_label, "alarm_table": alarm_table, "ack_button": ack_button,
        }
        self.btn_reservoir_nav.clicked.connect(self.open_reservoir_hmi)

    def open_tank_hmi(self, idx):
        """Abre la HMI del tanque seleccionado desde el P&ID o menú lateral."""
        if 0 <= idx < len(self.tank_page_indices):
            self.stacked.setCurrentIndex(self.tank_page_indices[idx])
            self.log("NAVIGATION", f"Apertura de HMI {self.TANK_TAGS[idx]}", self.role)

    def open_reservoir_hmi(self):
        """Abre la HMI de supervisión del depósito TK-105."""
        page_index = getattr(self, "reservoir_page_index", None)
        if page_index is not None:
            self.stacked.setCurrentIndex(page_index)
            self.log("NAVIGATION", "Apertura de HMI TK-105", self.role)

    def _ack_reservoir_alarms(self):
        if self.read_only:
            return
        matching = [code for code in self._active_alarms if code.startswith("LT105")]
        for code in matching:
            self._acked_alarms.add(code)
        if matching:
            self.log("ALARM", f"Reconocimiento de alarmas TK-105: {', '.join(matching)}", self.role)
        self.refresh_alarms()
        self._refresh_reservoir_hmi()

    def _refresh_reservoir_hmi(self):
        entry = getattr(self, "reservoir_page_widgets", None)
        if not entry:
            return
        valid = self.comms_ok
        entry["canvas"].update()
        if valid:
            label_text = f"{self.level_tk105:.1f} %"
            state_text = "SIMULACIÓN ACTIVA" if self.comms_mode == "SIM" else "COMUNICACIÓN OK"
            color = COLORS["flow"] if self.comms_mode == "SIM" else COLORS["good"]
        else:
            label_text = "--- %"
            state_text = "DATOS NO CONFIABLES"
            color = COLORS["quality"]
        if self.emergency_stopped:
            state_text = "PARADA SOFTWARE ACTIVA"
            color = COLORS["alarm"]
        entry["level"].setText(label_text)
        entry["status"].setText(state_text)
        entry["status"].setStyleSheet(
            f"background:white;border:1px solid {color};color:{color};border-radius:5px;padding:7px;font-weight:700;"
        )
        relevant = [(code, alarm) for code, alarm in self._active_alarms.items()
                    if code.startswith("LT105") or code == "COMMS-001"]
        relevant.sort(key=lambda pair: (0 if pair[1]["priority"] == "ALTA" else 1, pair[1]["since"]))
        table = entry["alarm_table"]
        table.setRowCount(max(1, len(relevant)))
        if not relevant:
            values = ["—", "Sin alarmas activas", "NORMAL", "—"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                table.setItem(0, col, item)
        else:
            for row, (code, alarm) in enumerate(relevant):
                state = "Activa reconocida" if code in self._acked_alarms else "Activa no reconocida"
                values = [alarm["priority"], f"{code}: {alarm['message']}", state,
                          alarm["since"].strftime("%H:%M:%S")]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    if col == 0:
                        item.setForeground(QColor(PRIORITY_COLOR[alarm["priority"]]))
                    table.setItem(row, col, item)
        entry["ack_button"].setEnabled(not self.read_only and any(
            code.startswith("LT105") and code not in self._acked_alarms for code, _ in relevant
        ))

    def _ack_tank_alarms(self, idx):
        if self.read_only or not 0 <= idx < 4:
            return
        prefixes = (f"LT{101+idx}", f"TT{101+idx}")
        matching = [code for code in self._active_alarms if code.startswith(prefixes)]
        for code in matching:
            self._acked_alarms.add(code)
        if matching:
            self.log("ALARM", f"Reconocimiento de alarmas de {self.TANK_TAGS[idx]}: {', '.join(matching)}", self.role)
        self.refresh_alarms()
        self._refresh_tank_pages()

    def _refresh_tank_pages(self):
        """Actualiza las cuatro HMI individuales desde el mismo estado del proceso."""
        if not getattr(self, "tank_page_widgets", None):
            return
        for idx, entry in enumerate(self.tank_page_widgets):
            entry["tank_canvas"].update()
            valid = self.comms_ok
            entry["status"].setText(
                ("SIMULACIÓN ACTIVA" if self.comms_mode == "SIM" else "COMUNICACIÓN OK")
                if valid else "DATOS NO CONFIABLES"
            )
            if valid:
                state_color = COLORS["good"] if self.comms_mode != "SIM" else COLORS["flow"]
                entry["status"].setStyleSheet(
                    f"background:white;border:1px solid {state_color};color:{state_color};border-radius:5px;padding:7px;font-weight:700;"
                )
                entry["metrics"]["level"].setText(f"{self.levels[idx]:.1f} %")
                entry["metrics"]["temperature"].setText(f"{self.temperatures[idx]:.1f} °C")
                entry["metrics"]["flow"].setText(f"{self.flows[idx]:.2f} L/min")
                entry["metrics"]["setpoint"].setText(f"{self.setpoints[idx]:.1f} %")
                entry["metrics"]["output"].setText(f"{self.outputs[idx]:.1f} %" if self.comms_mode == "SIM" else "N/D")
                entry["output"].setText(f"{self.outputs[idx]:.1f} %" if self.comms_mode == "SIM" else "N/D")
                entry["error"].setText(f"{self.setpoints[idx] - self.levels[idx]:+.1f} %")
            else:
                for key, unit in (("level", "%"), ("temperature", "°C"), ("flow", "L/min"), ("output", "%")):
                    entry["metrics"][key].setText(f"--- {unit}")
                entry["output"].setText("--- %")
                entry["error"].setText("--- %")

            # Sincroniza los controles duplicados de la HMI individual.
            if entry["mode"].currentText() != self.modes[idx]:
                self._set_blocked(entry["mode"], "setCurrentText", self.modes[idx])
            if abs(entry["setpoint"].value() - self.setpoints[idx]) > 1e-9:
                self._set_blocked(entry["setpoint"], "setValue", self.setpoints[idx])
            if abs(entry["manual"].value() - self.manual_outputs[idx]) > 1e-9:
                self._set_blocked(entry["manual"], "setValue", self.manual_outputs[idx])
            for key in ("Kp", "Ki", "Kd"):
                if abs(entry["gains"][key].value() - self.pid_gains[key][idx]) > 1e-9:
                    self._set_blocked(entry["gains"][key], "setValue", self.pid_gains[key][idx])
                entry["gains"][key].setEnabled(not self.read_only and not self.emergency_stopped and self.comms_mode == "SIM")
            controls_enabled = not self.read_only and not self.emergency_stopped
            entry["mode"].setEnabled(controls_enabled)
            entry["setpoint"].setEnabled(controls_enabled)
            entry["manual"].setEnabled(controls_enabled and self.modes[idx] == "Manual")
            entry["pump_button"].setEnabled(controls_enabled)
            entry["valve_button"].setEnabled(controls_enabled)
            entry["pump_button"].setText(
                f"P-{101+entry['pump_idx']}: {'RUN' if self.pumps[entry['pump_idx']] else 'STOP'}"
            )
            entry["valve_button"].setText(
                f"V-{101+entry['valve_idx']}: {'ABIERTA' if self.valves[entry['valve_idx']] else 'CERRADA'}"
            )
            entry["pump_button"].setStyleSheet(
                f"background:{'#e1f2e8' if self.pumps[entry['pump_idx']] else '#eceff1'};padding:8px;"
            )
            entry["valve_button"].setStyleSheet(
                f"background:{'#e1f2e8' if self.valves[entry['valve_idx']] else '#f9e7e5'};padding:8px;"
            )

            prefixes = (f"LT{101+idx}", f"TT{101+idx}")
            relevant = [(code, alarm) for code, alarm in self._active_alarms.items()
                        if code.startswith(prefixes) or code == "COMMS-001"]
            relevant.sort(key=lambda pair: (0 if pair[1]["priority"] == "ALTA" else 1, pair[1]["since"]))
            table = entry["alarm_table"]
            table.setRowCount(max(1, len(relevant)))
            if not relevant:
                values = ["—", "Sin alarmas activas", "NORMAL", "—"]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    table.setItem(0, col, item)
            else:
                for row, (code, alarm) in enumerate(relevant):
                    status_text = "Activa reconocida" if code in self._acked_alarms else "Activa no reconocida"
                    values = [alarm["priority"], f"{code}: {alarm['message']}", status_text,
                              alarm["since"].strftime("%H:%M:%S")]
                    for col, value in enumerate(values):
                        item = QTableWidgetItem(value)
                        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                        if col == 0:
                            item.setForeground(QColor(PRIORITY_COLOR[alarm["priority"]]))
                        table.setItem(row, col, item)
            entry["ack_button"].setEnabled(not self.read_only and bool(relevant) and any(
                code.startswith(prefixes) and code not in self._acked_alarms for code, _ in relevant
            ))

        self._refresh_reservoir_hmi()

    def _connect_signals(self):
        self.role_selector.currentTextChanged.connect(self._change_role)
        pages = [("overview", 0), ("control", 1), ("trends", 2), ("alarms", 3),
                 ("instrumentation", 4), ("communication", 5), ("student", 6)]
        for key, index in pages:
            self.buttons[key].clicked.connect(lambda checked=False, i=index: self.stacked.setCurrentIndex(i))
        self.buttons["comm_fault"].clicked.connect(self.toggle_simulated_comms_fault)
        self.buttons["ack_all"].clicked.connect(self.ack_all_alarms)
        self.buttons["shelve"].clicked.connect(self.shelve_selected_alarm)
        self.alarm_table.cellDoubleClicked.connect(self.ack_selected_alarm)

    # ----------------------------- Overview
    def _build_overview(self):
        layout = self._ensure_layout(self.overview_metrics, QHBoxLayout)
        self.metric_labels = {}
        metric_specs = [("Nivel medio · 5 depósitos", "metricLevel"),
                        ("Nivel TK-105", "metricTK105"),
                        ("Temperatura media", "metricTemp"),
                        ("Caudal total", "metricFlow"), ("Comunicación", "metricComms")]
        for title, key in metric_specs:
            card = QFrame()
            card.setObjectName("metricCard")
            card.setStyleSheet(f"QFrame#metricCard {{ background:white; border:1px solid {COLORS['border']}; border-radius:8px; }}")
            box = QVBoxLayout(card)
            box.setContentsMargins(10, 7, 10, 7)
            lab_title = QLabel(title.upper()); lab_title.setStyleSheet(f"color:{COLORS['muted']};font-size:9pt;font-weight:700;border:0;")
            lab_value = QLabel("---"); lab_value.setStyleSheet("font-size:17pt;font-weight:700;border:0;")
            box.addWidget(lab_title); box.addWidget(lab_value)
            layout.addWidget(card, 1)
            self.metric_labels[key] = lab_value
        self.pid_canvas = PIDCanvas(self)
        self.pid_canvas.setToolTip("Haga clic sobre T-101, T-102, T-103, T-104 o TK-105 para abrir la HMI correspondiente.")
        self._ensure_layout(self.pid_container, QVBoxLayout).addWidget(self.pid_canvas)

    # ----------------------------- Control
    def _build_control_page(self):
        root = self._ensure_layout(self.control_container, QVBoxLayout)
        top_row = QWidget()
        top_layout = QHBoxLayout(top_row)
        top_layout.setContentsMargins(0, 0, 0, 0)
        # Parada general visible en la página de control. Es una orden software.
        emergency_group = QGroupBox("Parada general")
        emergency_layout = QVBoxLayout(emergency_group)
        emergency_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_emergency_stop = QPushButton("PARO\nEMERGENCIA")
        self.btn_emergency_stop.setFixedSize(104, 104)
        self.btn_emergency_stop.setToolTip(
            "Orden de parada por software. NO sustituye el pulsador físico, relé de seguridad "
            "ni circuito cableado de parada de emergencia."
        )
        self.btn_emergency_stop.setStyleSheet(
            "QPushButton {background:#d71920;color:white;border:5px solid #8f1015;"
            "border-radius:52px;font-size:10pt;font-weight:900;padding:4px;}"
            "QPushButton:hover {background:#b51218;}"
            "QPushButton:pressed {background:#7f0d12;}"
        )
        self.btn_emergency_stop.clicked.connect(self.activate_emergency_stop)
        self.btn_emergency_reset = QPushButton("Rearmar software…")
        self.btn_emergency_reset.setMinimumHeight(32)
        self.btn_emergency_reset.setEnabled(False)
        self.btn_emergency_reset.setToolTip(
            "Libera el enclavamiento software después de verificar la causa y el dispositivo físico. "
            "No arranca automáticamente las bombas."
        )
        self.btn_emergency_reset.clicked.connect(self.reset_emergency_stop)
        self.emergency_status_label = QLabel("ESTADO: NORMAL")
        self.emergency_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emergency_status_label.setWordWrap(True)
        self.emergency_status_label.setStyleSheet(
            "background:#e8f3eb;color:#24613f;border:1px solid #9fc8ad;"
            "border-radius:5px;padding:5px;font-weight:700;"
        )
        emergency_layout.addWidget(self.btn_emergency_stop, 0, Qt.AlignmentFlag.AlignCenter)
        emergency_layout.addWidget(self.btn_emergency_reset)
        emergency_layout.addWidget(self.emergency_status_label)

        pumps_group = QGroupBox("Bombas de alimentación")
        pumps_layout = QHBoxLayout(pumps_group)
        self.btn_pumps = []
        for i, tag in enumerate(("P-101", "P-102")):
            btn = QPushButton(f"{tag}: RUN")
            btn.setMinimumHeight(42)
            btn.clicked.connect(lambda checked=False, idx=i: self.toggle_pump(idx))
            pumps_layout.addWidget(btn)
            self.btn_pumps.append(btn)
        valves_group = QGroupBox("Electroválvulas (ramas de caudal)")
        valves_layout = QGridLayout(valves_group)
        self.btn_valves = []
        valve_tags = ["V-101 · P101→T101", "V-102 · P102→T102", "V-103 · P102→T103", "V-104 · P101→T104"]
        for i, tag in enumerate(valve_tags):
            btn = QPushButton(tag + " · ABIERTA")
            btn.setMinimumHeight(36)
            btn.clicked.connect(lambda checked=False, idx=i: self.toggle_valve(idx))
            valves_layout.addWidget(btn, i // 2, i % 2)
            self.btn_valves.append(btn)
        top_layout.addWidget(emergency_group, 0)
        top_layout.addWidget(pumps_group, 1)
        top_layout.addWidget(valves_group, 2)
        root.addWidget(top_row)

        controller_group = QGroupBox("Controladores independientes de nivel")
        controller_layout = QVBoxLayout(controller_group)
        self.control_table = QTableWidget(4, 9)
        self.control_table.setHorizontalHeaderLabels(["Tanque / sensor", "Nivel actual", "Algoritmo", "Consigna (%)", "Salida manual (%)", "Salida calculada (%)", "Kp", "Ki", "Kd"])
        self.control_table.verticalHeader().setVisible(False)
        self.control_table.setAlternatingRowColors(True)
        self.control_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.control_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.control_table.verticalHeader().setDefaultSectionSize(48)
        self.control_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for col in range(1, 9):
            self.control_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.control_modes = []
        self.control_setpoints = []
        self.control_manuals = []
        self.control_value_labels = []
        self.pid_gain_widgets = {"Kp": [], "Ki": [], "Kd": []}
        for i in range(4):
            tag_item = QTableWidgetItem(f"{self.TANK_TAGS[i]} / LT-{101+i}")
            tag_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.control_table.setItem(i, 0, tag_item)
            current_item = QTableWidgetItem("-- %")
            current_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            current_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.control_table.setItem(i, 1, current_item)
            mode = QComboBox(); mode.addItems(self.MODE_OPTIONS); mode.setCurrentText("PID")
            mode.currentTextChanged.connect(lambda text, idx=i: self._set_mode(idx, text))
            self.control_table.setCellWidget(i, 2, mode); self.control_modes.append(mode)
            sp = QDoubleSpinBox(); sp.setRange(0, 100); sp.setDecimals(1); sp.setSuffix(" %"); sp.setValue(self.setpoints[i])
            sp.valueChanged.connect(lambda value, idx=i: self._set_setpoint(idx, value))
            self.control_table.setCellWidget(i, 3, sp); self.control_setpoints.append(sp)
            manual = QDoubleSpinBox(); manual.setRange(0, 100); manual.setDecimals(1); manual.setSuffix(" %"); manual.setValue(self.manual_outputs[i]); manual.setEnabled(False)
            manual.valueChanged.connect(lambda value, idx=i: self._set_manual_output(idx, value))
            self.control_table.setCellWidget(i, 4, manual); self.control_manuals.append(manual)
            output = QLabel("0.0 %"); output.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.control_table.setCellWidget(i, 5, output); self.control_value_labels.append(output)
            for col, key, step in ((6, "Kp", 0.1), (7, "Ki", 0.01), (8, "Kd", 0.01)):
                gain = QDoubleSpinBox()
                gain.setRange(0.0, 1000.0 if key != "Ki" else 100.0)
                gain.setDecimals(4)
                gain.setSingleStep(step)
                gain.setKeyboardTracking(False)
                gain.setValue(self.pid_gains[key][i])
                gain.setToolTip(f"Ganancia {key} para {self.TANK_TAGS[i]}. Se aplica al modelo local en SIM; el PLC/ESP32 remoto requiere un mapa de ganancias propio.")
                gain.valueChanged.connect(lambda value, idx=i, gain_key=key: self._set_pid_gain(idx, gain_key, value))
                self.control_table.setCellWidget(i, col, gain)
                self.pid_gain_widgets[key].append(gain)
        controller_layout.addWidget(self.control_table)
        note = QLabel("PID con ganancias Kp, Ki y Kd configurables por tanque. ON/OFF conmuta alrededor de una banda; Manual fija la salida. En simulación, la salida se representa como demanda proporcional/PWM idealizada, no como modulación continua de una electroválvula real. Ajustar ganancias cambia la respuesta del modelo; valide el método de sintonía antes de conectar un proceso físico.")
        note.setWordWrap(True); note.setStyleSheet(f"color:{COLORS['muted']};font-size:9pt;")
        controller_layout.addWidget(note)
        root.addWidget(controller_group, 1)

    @staticmethod
    def _set_blocked(widget, setter_name, value):
        """Actualiza un control duplicado sin volver a disparar su señal."""
        if widget is None:
            return
        blocker = QSignalBlocker(widget)
        getattr(widget, setter_name)(value)
        del blocker

    def _set_mode(self, idx, text):
        if self.emergency_stopped:
            return
        self.modes[idx] = text
        main_mode = self.control_modes[idx]
        if main_mode.currentText() != text:
            self._set_blocked(main_mode, "setCurrentText", text)
        self.control_manuals[idx].setEnabled(text == "Manual" and not self.read_only)
        if self.tank_page_widgets:
            page_mode = self.tank_page_widgets[idx]["mode"]
            if page_mode.currentText() != text:
                self._set_blocked(page_mode, "setCurrentText", text)
            self.tank_page_widgets[idx]["manual"].setEnabled(text == "Manual" and not self.read_only)
        self.log("CONTROL", f"Modo {self.TANK_TAGS[idx]}: {text}", self.role)
        self._write_modbus_mode(idx, text)
        self._publish_mqtt_command()

    def _set_setpoint(self, idx, value):
        if self.emergency_stopped:
            return
        value = float(value)
        self.setpoints[idx] = value
        self.integrals[idx] = 0.0
        self.prev_errors[idx] = 0.0
        main_widget = self.control_setpoints[idx]
        if abs(main_widget.value() - value) > 1e-9:
            self._set_blocked(main_widget, "setValue", value)
        if self.tank_page_widgets:
            other = self.tank_page_widgets[idx]["setpoint"]
            if abs(other.value() - value) > 1e-9:
                self._set_blocked(other, "setValue", value)
        self.log("SETPOINT", f"{self.TANK_TAGS[idx]} consigna={value:.1f}%", self.role)
        self._write_modbus_setpoint(idx, value)
        self._publish_mqtt_command()

    def _set_manual_output(self, idx, value):
        if self.emergency_stopped:
            return
        value = float(value)
        self.manual_outputs[idx] = value
        main_widget = self.control_manuals[idx]
        if abs(main_widget.value() - value) > 1e-9:
            self._set_blocked(main_widget, "setValue", value)
        if self.tank_page_widgets:
            other = self.tank_page_widgets[idx]["manual"]
            if abs(other.value() - value) > 1e-9:
                self._set_blocked(other, "setValue", value)
        self._write_modbus_manual(idx, value)
        self._publish_mqtt_command()

    def _set_pid_gain(self, idx, key, value):
        if self.emergency_stopped:
            return
        value = float(value)
        if key not in self.pid_gains or not 0 <= idx < 4:
            return
        if abs(self.pid_gains[key][idx] - value) < 1e-12:
            return
        self.pid_gains[key][idx] = value
        self.integrals[idx] = 0.0
        self.prev_errors[idx] = 0.0
        main_widget = self.pid_gain_widgets[key][idx]
        if abs(main_widget.value() - value) > 1e-9:
            self._set_blocked(main_widget, "setValue", value)
        if self.tank_page_widgets:
            other = self.tank_page_widgets[idx]["gains"][key]
            if abs(other.value() - value) > 1e-9:
                self._set_blocked(other, "setValue", value)
        self.log("PID", f"{self.TANK_TAGS[idx]} {key}={value:.4f}", self.role)
        # En este prototipo, los parámetros PID se aplican localmente al modo SIM.
        # Si se usa Modbus/MQTT, el controlador remoto debe implementar su propio mapa de ganancias.

    # ----------------------------- Tendencias
    def _build_trends_page(self):
        layout = self._ensure_layout(self.trend_controls, QHBoxLayout)
        label1 = QLabel("Variable:")
        self.trend_variable = QComboBox()
        self.trend_variable.addItems(["Nivel (%)", "Temperatura (°C)", "Caudal (L/min)"])
        label2 = QLabel("Punto:")
        self.trend_tank = QComboBox()
        self.trend_tank.addItems(["Todos los puntos", "T-101", "T-102", "T-103", "T-104", "TK-105"])
        layout.addWidget(label1); layout.addWidget(self.trend_variable); layout.addSpacing(18)
        layout.addWidget(label2); layout.addWidget(self.trend_tank); layout.addStretch(1)
        self._ensure_layout(self.plot_container, QVBoxLayout)
        self.plot = None
        self.fallback_plot = None
        if pg is not None:
            pg.setConfigOptions(antialias=True)
            self.plot = pg.PlotWidget()
            self.plot.setBackground("w")
            self.plot.showGrid(x=True, y=True, alpha=0.25)
            self.plot.setLabel("bottom", "Tiempo", units="s")
            self.plot.addLegend(offset=(10, 10))
            self.plot_container.layout().addWidget(self.plot)
        else:
            self.fallback_plot = FallbackTrendWidget()
            self.plot_container.layout().addWidget(self.fallback_plot)
        self.trend_curves = []
        self.trend_variable.currentTextChanged.connect(self._configure_trends)
        self.trend_tank.currentTextChanged.connect(self._configure_trends)
        self._configure_trends()

    def _trend_indices(self, variable, selected):
        if selected == "Todos los puntos":
            return range(5) if variable == "Nivel (%)" else range(4)
        if selected == "TK-105":
            return [4] if variable == "Nivel (%)" else []
        return [int(selected[-1]) - 1]

    @staticmethod
    def _trend_series_name(idx, variable):
        if idx == 4:
            return "LT-105 · TK-105"
        return (f"LT-{101+idx}" if variable == "Nivel (%)" else
                f"TT-{101+idx}" if variable == "Temperatura (°C)" else f"FT-{101+idx}")

    def _configure_trends(self, *_):
        variable = self.trend_variable.currentText()
        # TK-105 solo tiene transmisor de nivel; evita dejar una gráfica vacía
        # cuando se selecciona temperatura o caudal.
        if variable != "Nivel (%)" and self.trend_tank.currentText() == "TK-105":
            self._set_blocked(self.trend_tank, "setCurrentText", "Todos los puntos")
        if self.plot is None:
            self.refresh_trend_plot()
            return
        self.plot.clear()
        self.plot.addLegend(offset=(10, 10))
        if variable == "Nivel (%)":
            self.plot.setLabel("left", "Nivel", units="%")
            self.plot.setYRange(0, 100)
        elif variable == "Temperatura (°C)":
            self.plot.setLabel("left", "Temperatura", units="°C")
            self.plot.setYRange(0, 80)
        else:
            self.plot.setLabel("left", "Caudal", units="L/min")
            self.plot.enableAutoRange(axis="y", enable=True)
        self.trend_curves = []
        indices = self._trend_indices(variable, self.trend_tank.currentText())
        for idx in indices:
            name = self._trend_series_name(idx, variable)
            color_idx = idx % len(SERIES_COLORS)
            curve = self.plot.plot([], [], pen=pg.mkPen(SERIES_COLORS[color_idx], width=2), name=name)
            self.trend_curves.append((idx, curve))
        self.refresh_trend_plot()

    def refresh_trend_plot(self):
        variable = self.trend_variable.currentText()
        if variable == "Nivel (%)":
            source = self.level_history
        elif variable == "Temperatura (°C)":
            source = self.temp_history
        else:
            source = self.flow_history
        indices = list(self._trend_indices(variable, self.trend_tank.currentText()))
        if self.plot is not None:
            for idx, curve in self.trend_curves:
                if idx >= len(source):
                    continue
                xs = list(range(len(source[idx])))
                ys = list(source[idx])
                curve.setData(xs, ys)
            if self.time_history:
                self.plot.setLabel("bottom", "Muestras recientes (1 s/ciclo)")
        elif self.fallback_plot is not None:
            data = {}
            for idx in indices:
                if idx >= len(source):
                    continue
                data[self._trend_series_name(idx, variable)] = list(source[idx])
            self.fallback_plot.set_series(
                data, variable,
                variable[variable.find("(")+1:variable.find(")")] if "(" in variable else ""
            )

    # ----------------------------- Instrumentación
    def _build_instrumentation_page(self):
        root = self._ensure_layout(self.instrument_container, QVBoxLayout)
        intro = QLabel("Mapa de tags y direccionamiento de referencia para el modo Modbus TCP. Las escalas son didácticas; no sustituyen las hojas de datos ni la ingeniería de instrumentación.")
        intro.setWordWrap(True); intro.setStyleSheet(f"color:{COLORS['muted']};")
        root.addWidget(intro)
        self.instrument_table = QTableWidget(0, 7)
        self.instrument_table.setHorizontalHeaderLabels(["Tag", "Variable", "Ubicación / servicio", "Rango ilustrativo", "Unidad", "Modbus", "MQTT key"])
        self.instrument_table.verticalHeader().setVisible(False)
        self.instrument_table.setAlternatingRowColors(True)
        self.instrument_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.instrument_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.instrument_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.instrument_table.horizontalHeader().setStretchLastSection(True)
        data = []
        for i in range(4):
            data.append((f"LT-{101+i}", "Transmisor de nivel", self.TANK_TAGS[i], "0…100", "%", f"HR {i} / 4xxxx {40001+i}", f"levels[{i}]"))
        data.append(("LT-105", "Transmisor de nivel", "TK-105 · depósito principal", "0…100", "%", "HR 12 / 40013", "level_tk105"))
        for i in range(4):
            data.append((f"TT-{101+i}", "Transmisor de temperatura", self.TANK_TAGS[i], "0…80", "°C", f"HR {4+i}", f"temperatures[{i}]"))
        flow_locations = ["Descarga principal P-101", "Descarga principal P-102", "Rama P-102 → T-103", "Rama P-101 → T-104"]
        for i in range(4):
            data.append((f"FT-{101+i}", "Transmisor de caudal", flow_locations[i], "0…3.0", "L/min", f"HR {8+i}", f"flows[{i}]"))
        for i in range(4):
            data.append((f"LIC-{101+i}", "Controlador de nivel", self.TANK_TAGS[i], "Consigna 0…100", "%", f"HR {20+i} consigna; HR {24+i} modo; HR {28+i} manual", f"modes[{i}], setpoints[{i}], manual_outputs[{i}]"))
        data.extend([
            ("P-101", "Bomba", "Alimentación T-101/T-104", "RUN/STOP", "binario", "Coil 0", "pumps[0]"),
            ("P-102", "Bomba", "Alimentación T-102/T-103", "RUN/STOP", "binario", "Coil 1", "pumps[1]"),
            ("V-101", "Electroválvula", "Rama P-101 → T-101", "Abierta/cerrada", "binario", "Coil 2", "valves[0]"),
            ("V-102", "Electroválvula", "Rama P-102 → T-102", "Abierta/cerrada", "binario", "Coil 3", "valves[1]"),
            ("V-103", "Electroválvula", "Rama P-102 → T-103", "Abierta/cerrada", "binario", "Coil 4", "valves[2]"),
            ("V-104", "Electroválvula", "Rama P-101 → T-104", "Abierta/cerrada", "binario", "Coil 5", "valves[3]"),
        ])
        self.instrument_table.setRowCount(len(data))
        for row, record in enumerate(data):
            for col, value in enumerate(record):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                self.instrument_table.setItem(row, col, item)
        root.addWidget(self.instrument_table, 1)

    # ----------------------------- Comunicación
    def _build_communication_page(self):
        root = self._ensure_layout(self.comm_container, QVBoxLayout)
        form_group = QGroupBox("Modo de adquisición")
        form = QGridLayout(form_group)
        form.addWidget(QLabel("Modo:"), 0, 0)
        self.comm_mode_combo = QComboBox()
        self.comm_mode_combo.addItems(["Simulación académica", "Modbus TCP (ESP32 / PLC)", "MQTT (telemetría remota)"])
        form.addWidget(self.comm_mode_combo, 0, 1, 1, 3)
        self.modbus_group = QGroupBox("Parámetros Modbus TCP")
        mb_form = QFormLayout(self.modbus_group)
        self.modbus_host = QLineEdit("192.168.1.50")
        self.modbus_port = QSpinBox(); self.modbus_port.setRange(1, 65535); self.modbus_port.setValue(502)
        self.modbus_unit = QSpinBox(); self.modbus_unit.setRange(0, 255); self.modbus_unit.setValue(1)
        mb_form.addRow("IP / Host", self.modbus_host)
        mb_form.addRow("Puerto", self.modbus_port)
        mb_form.addRow("Unit ID / Slave", self.modbus_unit)
        self.mqtt_group = QGroupBox("Parámetros MQTT")
        mqtt_form = QFormLayout(self.mqtt_group)
        self.mqtt_host = QLineEdit("127.0.0.1")
        self.mqtt_port = QSpinBox(); self.mqtt_port.setRange(1, 65535); self.mqtt_port.setValue(1883)
        self.mqtt_topic = QLineEdit("scada/tanques")
        self.mqtt_user = QLineEdit("")
        self.mqtt_password = QLineEdit(""); self.mqtt_password.setEchoMode(QLineEdit.EchoMode.Password)
        mqtt_form.addRow("Broker", self.mqtt_host)
        mqtt_form.addRow("Puerto", self.mqtt_port)
        mqtt_form.addRow("Prefijo de tópicos", self.mqtt_topic)
        mqtt_form.addRow("Usuario (opcional)", self.mqtt_user)
        mqtt_form.addRow("Contraseña (opcional)", self.mqtt_password)
        form.addWidget(self.modbus_group, 1, 0, 1, 2)
        form.addWidget(self.mqtt_group, 1, 2, 1, 2)
        root.addWidget(form_group)
        actions = QHBoxLayout()
        self.btn_connect_comm = QPushButton("Aplicar modo / conectar")
        self.btn_connect_comm.setMinimumHeight(38)
        self.btn_disconnect_comm = QPushButton("Desconectar y volver a simulación")
        actions.addWidget(self.btn_connect_comm); actions.addWidget(self.btn_disconnect_comm); actions.addStretch(1)
        root.addLayout(actions)
        self.comm_status_label = QLabel("Estado: simulación activa")
        self.comm_status_label.setObjectName("commStatus")
        root.addWidget(self.comm_status_label)
        self.comm_help_label = QLabel("")
        self.comm_help_label.setWordWrap(True)
        self.comm_help_label.setMinimumHeight(95)
        self.comm_help_label.setStyleSheet(f"background:white;border:1px solid {COLORS['border']};border-radius:6px;padding:10px;")
        root.addWidget(self.comm_help_label)
        guide = QLabel("Mapa de referencia del prototipo (direcciones Pymodbus con offset base cero): HR 0–3 = LT-101…LT-104 ×10 (%); HR 4–7 = TT-101…TT-104 ×10 (°C); HR 8–11 = FT-101…FT-104 ×100 (L/min); HR 12 = LT-105 ×10 (%). Coils 0–1 = bombas P-101/P-102; coils 2–5 = válvulas CV-101…CV-104; coil 6 = solicitud de parada de emergencia por software (debe implementarse en el PLC/ESP32). HR 20–23 = consignas ×10; HR 24–27 = modo (0 PID, 1 ON/OFF, 2 Manual); HR 28–31 = salida manual ×10. Offset 0 suele representarse como holding register 40001 en documentación Modbus. Verifique el mapa del dispositivo: no se asume compatibilidad automática.")
        guide.setWordWrap(True); guide.setStyleSheet(f"color:{COLORS['muted']};font-size:9pt;")
        self.comm_map_guide = guide
        root.addWidget(guide)
        root.addStretch(1)
        self.comm_mode_combo.currentIndexChanged.connect(self._update_comm_mode_help)
        self.mqtt_topic.textChanged.connect(self._update_comm_mode_help)
        self.btn_connect_comm.clicked.connect(self.connect_communication)
        self.btn_disconnect_comm.clicked.connect(self.return_to_simulation)
        self._update_comm_mode_help()

    def _update_comm_mode_help(self, *_):
        mode = self.comm_mode_combo.currentIndex()
        self.modbus_group.setVisible(mode == 1)
        self.mqtt_group.setVisible(mode == 2)
        if mode == 0:
            text = "SIMULACIÓN ACADÉMICA — se simulan T-101…T-104 y el depósito principal TK-105/LT-105; permite practicar PID, ON/OFF, maniobras, alarmas y parada software sin PLC ni ESP32."
        elif mode == 1:
            text = "MODBUS TCP — el SCADA actúa como cliente y consulta registros/coils en un servidor Modbus TCP del PLC o ESP32. Configure IP, puerto y Unit ID. El firmware/controlador debe publicar las variables en el mapa documentado y habilitar la red."
        else:
            base_topic = self.mqtt_topic.text().strip().strip("/") or "scada/tanques"
            text = f"MQTT — el SCADA se suscribe a {base_topic}/telemetry y publica comandos en {base_topic}/commands. Es útil para una arquitectura remota mediante broker. Este prototipo no configura TLS: en una red real implemente cifrado, autenticación y ACL por tópico. JSON esperado: levels, level_tk105, temperatures, flows, pumps y valves. El campo emergency_stop solicita el enclavamiento software en el controlador remoto."
        self.comm_help_label.setText(text)

    def connect_communication(self):
        if self.read_only:
            return
        selected = self.comm_mode_combo.currentIndex()
        self._close_communication_clients()
        self.sim_fault = False
        self.mqtt_snapshot = None
        self.buttons["comm_fault"].setEnabled(not self.read_only and selected == 0)
        self.buttons["comm_fault"].setText("Simular falla de comunicación")
        if selected == 0:
            self.comms_mode = "SIM"
            self.comms_ok = True
            self.comm_status_label.setText("Estado: simulación académica activa (sin hardware)")
            self.log("COMMS", "Modo SIM activado", self.role)
            return
        if selected == 1:
            try:
                from pymodbus.client import ModbusTcpClient
            except Exception as exc:
                self.comms_mode = "MODBUS"
                self.comms_ok = False
                self.comm_status_label.setText("Estado: falta pymodbus. Instale requirements.txt")
                QMessageBox.warning(self.ui, "Dependencia requerida", f"No se pudo importar pymodbus.\n\n{exc}\n\nInstale las dependencias del proyecto.")
                return
            self.comms_mode = "MODBUS"
            try:
                self.modbus_client = ModbusTcpClient(self.modbus_host.text().strip(), port=self.modbus_port.value(), timeout=2)
                if self.modbus_client.connect():
                    self.comms_ok = True
                    self.comm_status_label.setText(f"Estado: conectado por Modbus TCP a {self.modbus_host.text().strip()}:{self.modbus_port.value()}")
                    self.log("COMMS", f"Conectado Modbus TCP {self.modbus_host.text()}:{self.modbus_port.value()}", self.role)
                    self._poll_modbus()
                    if self.emergency_stopped:
                        self._send_remote_emergency_stop()
                else:
                    self.comms_ok = False
                    self.comm_status_label.setText("Estado: no se pudo conectar por Modbus TCP")
                    QMessageBox.warning(self.ui, "Modbus TCP", "No se pudo establecer conexión. Verifique IP, puerto, firewall, Unit ID y que el servidor esté activo.")
                    self.log("COMMS", "Fallo de conexión Modbus TCP", self.role)
            except Exception as exc:
                self.comms_ok = False
                self.comm_status_label.setText(f"Error Modbus: {exc}")
                QMessageBox.warning(self.ui, "Modbus TCP", str(exc))
            return

        # MQTT
        self.comms_mode = "MQTT"
        try:
            import paho.mqtt.client as mqtt
        except Exception as exc:
            self.comms_ok = False
            self.mqtt_status_message = "Estado: falta paho-mqtt. Instale requirements.txt"
            self.comm_status_label.setText(self.mqtt_status_message)
            QMessageBox.warning(self.ui, "Dependencia requerida", f"No se pudo importar paho-mqtt.\n\n{exc}")
            return
        topic = self.mqtt_topic.text().strip().strip("/") or "scada/tanques"
        self.mqtt_base_topic = topic
        try:
            try:
                self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="scada-cuatro-tanques")
            except Exception:
                self.mqtt_client = mqtt.Client(client_id="scada-cuatro-tanques")
            if self.mqtt_user.text().strip():
                self.mqtt_client.username_pw_set(self.mqtt_user.text().strip(), self.mqtt_password.text())
            self.mqtt_client.on_connect = self._mqtt_on_connect
            self.mqtt_client.on_message = self._mqtt_on_message
            self.mqtt_connected = False
            self.comms_ok = False
            self.mqtt_status_message = f"Estado: conectando a broker MQTT {self.mqtt_host.text()}:{self.mqtt_port.value()}…"
            self.mqtt_client.connect(self.mqtt_host.text().strip(), self.mqtt_port.value(), keepalive=60)
            self.mqtt_client.loop_start()
            self.comm_status_label.setText(self.mqtt_status_message)
            self.log("COMMS", f"Conexión MQTT iniciada a {self.mqtt_host.text()}:{self.mqtt_port.value()}", self.role)
        except Exception as exc:
            self.comms_ok = False
            self.mqtt_status_message = f"Error MQTT: {exc}"
            self.comm_status_label.setText(self.mqtt_status_message)
            QMessageBox.warning(self.ui, "MQTT", f"No se pudo conectar al broker.\n\n{exc}")

    def _mqtt_on_connect(self, client, userdata, flags, reason_code, *args):
        try:
            rc = int(getattr(reason_code, "value", reason_code))
        except Exception:
            rc = 1
        if rc == 0:
            self.mqtt_connected = True
            self.comms_ok = True
            # Callback MQTT se ejecuta en un hilo de red; no leer widgets Qt aquí.
            base_topic = getattr(self, "mqtt_base_topic", "scada/tanques")
            client.subscribe(base_topic + "/telemetry", qos=0)
            self.mqtt_status_message = f"Estado: MQTT conectado; suscrito a {base_topic}/telemetry"
            if getattr(self, "emergency_stopped", False):
                try:
                    client.publish(base_topic + "/commands", json.dumps(self._mqtt_command_payload()), qos=1, retain=False)
                except Exception:
                    pass
        else:
            self.mqtt_connected = False
            self.comms_ok = False
            self.mqtt_status_message = f"Error de conexión MQTT: código {rc}"

    def _mqtt_on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            self.mqtt_snapshot = payload
            self.mqtt_last_rx = datetime.now()
            self.comms_ok = True
        except Exception as exc:
            self.log("COMMS", f"Mensaje MQTT inválido: {exc}", self.role)

    def _consume_mqtt_snapshot(self):
        if self.mqtt_last_rx is None or (datetime.now() - self.mqtt_last_rx).total_seconds() > 5:
            self.comms_ok = False
            return
        snap = self.mqtt_snapshot or {}
        for attr, key, length in (("levels", "levels", 4), ("temperatures", "temperatures", 4), ("flows", "flows", 4)):
            values = snap.get(key)
            if isinstance(values, dict):
                lookup = [values.get(self.TANK_TAGS[i], values.get(str(i), None)) for i in range(length)]
                if key == "flows":
                    lookup = [values.get(f"FT-{101+i}", values.get(str(i), lookup[i])) for i in range(length)]
                values = lookup
            if isinstance(values, list) and len(values) >= length:
                try:
                    parsed = [float(values[i]) for i in range(length)]
                    if attr == "levels": self.levels = [max(0, min(100, v)) for v in parsed]
                    elif attr == "temperatures": self.temperatures = [max(-20, min(150, v)) for v in parsed]
                    else: self.flows = [max(0, min(1000, v)) for v in parsed]
                except (TypeError, ValueError):
                    pass

        # LT-105 puede publicarse como clave independiente o dentro de levels.
        raw_tk105 = snap.get("level_tk105", snap.get("LT-105", snap.get("TK-105")))
        if raw_tk105 is None and isinstance(snap.get("levels"), dict):
            raw_tk105 = snap["levels"].get("TK-105", snap["levels"].get("LT-105"))
        if raw_tk105 is not None:
            try:
                self.level_tk105 = max(0.0, min(100.0, float(raw_tk105)))
            except (TypeError, ValueError):
                pass

        feedback = snap.get("emergency_stop_active", snap.get("emergency_stop_feedback"))
        self.remote_estop_feedback = None if feedback is None else bool(feedback)
        for attr in ("pumps", "valves"):
            values = snap.get(attr)
            if isinstance(values, list) and len(values) >= len(getattr(self, attr)):
                setattr(self, attr, [bool(v) for v in values[:len(getattr(self, attr))]])

    def _mqtt_command_payload(self):
        return {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "pumps": self.pumps,
            "valves": self.valves,
            "setpoints": self.setpoints,
            "modes": self.modes,
            "manual_outputs": self.manual_outputs,
            "outputs": [0.0] * 4 if self.emergency_stopped else self.outputs,
            "emergency_stop": bool(self.emergency_stopped),
        }

    def _publish_mqtt_command(self, force=False):
        if self.comms_mode != "MQTT" or not self.mqtt_client or not self.mqtt_connected:
            return False
        if self.read_only and not force:
            return False
        topic = getattr(self, "mqtt_base_topic", "scada/tanques") + "/commands"
        try:
            info = self.mqtt_client.publish(
                topic, json.dumps(self._mqtt_command_payload()),
                qos=1 if force else 0, retain=False
            )
            return getattr(info, "rc", 0) == 0
        except Exception as exc:
            self.log("COMMS", f"No se pudo publicar comando MQTT: {exc}", self.role)
            return False

    def return_to_simulation(self):
        if self.read_only:
            return
        self._close_communication_clients()
        self.comms_mode = "SIM"
        self.comm_mode_combo.setCurrentIndex(0)
        self.sim_fault = False
        self.comms_ok = True
        self.comm_status_label.setText("Estado: simulación académica activa (sin hardware)")
        self.buttons["comm_fault"].setText("Simular falla de comunicación")
        self.buttons["comm_fault"].setEnabled(True)
        self.log("COMMS", "Retorno a simulación", self.role)

    def _close_communication_clients(self):
        if self.modbus_client is not None:
            try: self.modbus_client.close()
            except Exception: pass
        self.modbus_client = None
        if self.mqtt_client is not None:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception: pass
        self.mqtt_client = None
        self.mqtt_connected = False

    def toggle_simulated_comms_fault(self):
        if self.read_only or self.comms_mode != "SIM":
            return
        self.sim_fault = not self.sim_fault
        self.comms_ok = not self.sim_fault
        self.buttons["comm_fault"].setText("Restablecer comunicación" if self.sim_fault else "Simular falla de comunicación")
        self.comm_status_label.setText("Estado: falla simulada" if self.sim_fault else "Estado: simulación académica activa")
        self.log("COMMS", "Falla de comunicación simulada" if self.sim_fault else "Comunicación simulada restablecida", self.role)

    # ----------------------------- Parada general / E-Stop software
    def _send_remote_emergency_stop(self):
        """Envía la solicitud de paro al controlador remoto; no equivale a un E-Stop físico."""
        if self.comms_mode == "SIM":
            return True
        if self.comms_mode == "MODBUS":
            if self.modbus_client is None:
                return False
            results = []
            # Coil 6 debe estar programada en PLC/ESP32 como enclavamiento de paro.
            results.append(self._write_modbus_coil(6, True, force=True))
            # Primero se desenergizan las bombas; luego se ordena cerrar las válvulas.
            results.append(self._write_modbus_coil(0, False, force=True))
            results.append(self._write_modbus_coil(1, False, force=True))
            for address in range(2, 6):
                results.append(self._write_modbus_coil(address, False, force=True))
            for idx in range(4):
                try:
                    response = self._modbus_call("write_register", 28 + idx, 0)
                    results.append(self._response_ok(response))
                except Exception:
                    results.append(False)
            return all(results)
        if self.comms_mode == "MQTT":
            return self._publish_mqtt_command(force=True)
        return False

    def activate_emergency_stop(self):
        """Enclava el paro en la HMI, congela SIM y manda la orden a Modbus/MQTT."""
        if self.emergency_stopped:
            return

        self.emergency_stopped = True
        self.emergency_stop_timestamp = datetime.now()
        self.pumps = [False, False]
        self.valves = [False, False, False, False]
        self.outputs = [0.0] * 4
        self.manual_outputs = [0.0] * 4
        self.integrals = [0.0] * 4
        self.prev_errors = [0.0] * 4
        if self.comms_mode == "SIM":
            self.flows = [0.0] * 4

        # Sincroniza los valores manuales visibles sin volver a disparar señales.
        for widget in getattr(self, "control_manuals", []):
            self._set_blocked(widget, "setValue", 0.0)
        for entry in getattr(self, "tank_page_widgets", []):
            self._set_blocked(entry.get("manual"), "setValue", 0.0)

        remote_ok = self._send_remote_emergency_stop()
        self.emergency_command_status = (
            "Orden enviada al controlador remoto; verifique su confirmación."
            if remote_ok and self.comms_mode != "SIM"
            else "Simulación congelada localmente."
            if self.comms_mode == "SIM"
            else "No se pudo confirmar el envío remoto; use el paro físico."
        )
        self.log(
            "E_STOP",
            f"Parada de emergencia software activada en modo {self.comms_mode}; remoto_ok={remote_ok}",
            self.role
        )
        if self.comms_mode != "SIM" and not remote_ok:
            QMessageBox.critical(
                self.ui,
                "Parada local sin confirmación remota",
                "La HMI ha enclavado la parada local, pero no pudo enviar todas las órdenes al controlador. "
                "Use el dispositivo físico de parada de emergencia y verifique directamente el estado de los actuadores."
            )
        self._update_alarm_list()
        self.refresh_ui()

    def reset_emergency_stop(self):
        """Libera solo el enclavamiento software; jamás arranca automáticamente el proceso."""
        if self.read_only or not self.emergency_stopped:
            return
        answer = QMessageBox.question(
            self.ui,
            "Rearme de parada software",
            "¿Se eliminó la causa del paro y se verificó/restableció el dispositivo físico de seguridad?\n\n"
            "Esta acción libera únicamente el enclavamiento software. Bombas y válvulas permanecerán detenidas.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        if self.comms_mode == "MODBUS":
            if self.modbus_client is None:
                QMessageBox.warning(self.ui, "Sin conexión", "No se puede liberar la solicitud remota: Modbus no está conectado. Mantenga el paro físico aplicado.")
                return
            if not self._write_modbus_coil(6, False, force=True):
                QMessageBox.warning(self.ui, "Rearme no confirmado", "No se pudo liberar el coil 6. Verifique el mapa del PLC/ESP32 y el estado del circuito físico.")
                return
        elif self.comms_mode == "MQTT":
            if not self.mqtt_client or not self.mqtt_connected:
                QMessageBox.warning(self.ui, "Sin conexión", "No se puede publicar el rearme MQTT. Mantenga el paro físico aplicado.")
                return
            self.emergency_stopped = False
            if not self._publish_mqtt_command(force=True):
                self.emergency_stopped = True
                QMessageBox.warning(self.ui, "Rearme no confirmado", "No se pudo publicar el comando de rearme MQTT.")
                return

        self.emergency_stopped = False
        self.remote_estop_feedback = None
        # No se recuperan estados anteriores: el arranque posterior debe ser manual y deliberado.
        self.pumps = [False, False]
        self.valves = [False, False, False, False]
        self.outputs = [0.0] * 4
        self.manual_outputs = [0.0] * 4
        self.flows = [0.0] * 4 if self.comms_mode == "SIM" else self.flows
        self.log("E_STOP_RESET", "Rearme del enclavamiento software; actuadores permanecen STOP/cerrados", self.role)
        self.emergency_command_status = "Rearme software realizado; proceso detenido."
        self._update_alarm_list()
        self.refresh_ui()

    # ----------------------------- Actuadores / Modbus
    def toggle_pump(self, idx):
        if self.read_only or self.emergency_stopped:
            return
        if self.comms_mode != "SIM" and not self.comms_ok:
            QMessageBox.warning(self.ui, "Comando no enviado", "La comunicación remota no es confiable. Restablezca el enlace antes de mandar comandos.")
            self.log("CMD", f"P-{101+idx} comando rechazado: comunicación no confiable", self.role)
            return
        new_state = not self.pumps[idx]
        if new_state and self.comms_ok:
            affected = [0, 3] if idx == 0 else [1, 2]
            high_tanks = [self.TANK_TAGS[i] for i in affected if self.levels[i] >= 90.0]
            if high_tanks:
                reason = ", ".join(high_tanks)
                QMessageBox.warning(self.ui, "Permisivo de arranque", f"No se autoriza el arranque de P-{101+idx}: nivel muy alto en {reason}. Verifique el proceso antes de reintentar.")
                self.log("CMD", f"P-{101+idx} arranque rechazado por nivel HH: {reason}", self.role)
                return
        self.pumps[idx] = new_state
        tag = f"P-{101+idx}"
        self.log("CMD", f"{tag} {'RUN' if new_state else 'STOP'}", self.role)
        self._write_modbus_coil(idx, new_state)
        self._publish_mqtt_command()
        self.refresh_ui()

    def toggle_valve(self, idx):
        if self.read_only or self.emergency_stopped:
            return
        if self.comms_mode != "SIM" and not self.comms_ok:
            QMessageBox.warning(self.ui, "Comando no enviado", "La comunicación remota no es confiable. Restablezca el enlace antes de mandar comandos.")
            self.log("CMD", f"V-{101+idx} comando rechazado: comunicación no confiable", self.role)
            return
        self.valves[idx] = not self.valves[idx]
        self.log("CMD", f"V-{101+idx} {'ABIERTA' if self.valves[idx] else 'CERRADA'}", self.role)
        self._write_modbus_coil(idx + 2, self.valves[idx])
        self._publish_mqtt_command()
        self.refresh_ui()

    def _modbus_call(self, method_name, *args, **kwargs):
        if not self.modbus_client:
            return None
        method = getattr(self.modbus_client, method_name)
        unit = self.modbus_unit.value()
        try:
            return method(*args, device_id=unit, **kwargs)
        except TypeError:
            try:
                return method(*args, slave=unit, **kwargs)
            except TypeError:
                return method(*args, unit=unit, **kwargs)

    @staticmethod
    def _response_ok(response):
        return response is not None and not (hasattr(response, "isError") and response.isError())

    def _write_modbus_coil(self, address, value, force=False):
        if self.comms_mode != "MODBUS" or self.modbus_client is None:
            return False
        if not self.comms_ok and not force:
            return False
        try:
            resp = self._modbus_call("write_coil", address, bool(value))
            if not self._response_ok(resp):
                self.comms_ok = False
                self.comm_status_label.setText(f"Error al escribir coil Modbus {address}")
                return False
            return True
        except Exception as exc:
            self.comms_ok = False
            self.comm_status_label.setText(f"Error al escribir Modbus: {exc}")
            return False

    def _write_modbus_setpoint(self, idx, value):
        if self.comms_mode != "MODBUS" or self.modbus_client is None or not self.comms_ok:
            return
        try:
            resp = self._modbus_call("write_register", 20 + idx, int(round(value * 10)))
            if not self._response_ok(resp):
                self.comm_status_label.setText("No se pudo escribir la consigna Modbus")
        except Exception as exc:
            self.comm_status_label.setText(f"Error al escribir consigna: {exc}")

    def _write_modbus_mode(self, idx, mode):
        if self.comms_mode != "MODBUS" or self.modbus_client is None or not self.comms_ok:
            return
        mode_code = {"PID": 0, "ON/OFF": 1, "Manual": 2}.get(mode, 0)
        try:
            resp = self._modbus_call("write_register", 24 + idx, mode_code)
            if not self._response_ok(resp):
                self.comm_status_label.setText(f"No se pudo escribir modo de {self.TANK_TAGS[idx]}")
        except Exception as exc:
            self.comm_status_label.setText(f"Error al escribir modo Modbus: {exc}")

    def _write_modbus_manual(self, idx, value):
        if self.comms_mode != "MODBUS" or self.modbus_client is None or not self.comms_ok:
            return
        try:
            resp = self._modbus_call("write_register", 28 + idx, int(round(float(value) * 10)))
            if not self._response_ok(resp):
                self.comm_status_label.setText(f"No se pudo escribir salida manual de {self.TANK_TAGS[idx]}")
        except Exception as exc:
            self.comm_status_label.setText(f"Error al escribir salida manual: {exc}")

    def _poll_modbus(self):
        if self.comms_mode != "MODBUS" or self.modbus_client is None:
            return
        try:
            regs = self._modbus_call("read_holding_registers", 0, count=13)
            if not self._response_ok(regs) or not hasattr(regs, "registers") or len(regs.registers) < 13:
                self.comms_ok = False
                self.comm_status_label.setText("Error: lectura Modbus inválida / falta HR 12 (LT-105) o comunicación perdida")
                return
            vals = regs.registers
            self.levels = [max(0, min(100, vals[i] / 10.0)) for i in range(4)]
            self.temperatures = [max(-20, min(150, vals[4+i] / 10.0)) for i in range(4)]
            self.flows = [max(0, vals[8+i] / 100.0) for i in range(4)]
            self.level_tk105 = max(0.0, min(100.0, vals[12] / 10.0))
            coils = self._modbus_call("read_coils", 0, count=6)
            if self._response_ok(coils) and hasattr(coils, "bits") and len(coils.bits) >= 6:
                self.pumps = [bool(coils.bits[0]), bool(coils.bits[1])]
                self.valves = [bool(coils.bits[2]), bool(coils.bits[3]), bool(coils.bits[4]), bool(coils.bits[5])]
            # La realimentación del enclavamiento es opcional, pero recomendable.
            try:
                estop_coil = self._modbus_call("read_coils", 6, count=1)
                self.remote_estop_feedback = (
                    bool(estop_coil.bits[0])
                    if self._response_ok(estop_coil) and hasattr(estop_coil, "bits") and len(estop_coil.bits) >= 1
                    else None
                )
            except Exception:
                self.remote_estop_feedback = None
            self.comms_ok = True
            self.comm_status_label.setText(f"Estado: Modbus TCP conectado · última lectura {datetime.now().strftime('%H:%M:%S')}")
        except Exception as exc:
            self.comms_ok = False
            self.comm_status_label.setText(f"Fallo de lectura Modbus: {exc}")

    # ----------------------------- Motor de proceso
    def _controller_output(self, idx, dt=1.0):
        mode = self.modes[idx]
        pv = self.levels[idx]
        sp = self.setpoints[idx]
        error = sp - pv
        if mode == "Manual":
            output = self.manual_outputs[idx]
        elif mode == "ON/OFF":
            if pv < sp - 1.5:
                output = 100.0
            elif pv > sp + 1.5:
                output = 0.0
            else:
                output = self.outputs[idx]
        else:
            # PID académico acotado; ganancias editables e independientes por tanque.
            kp = self.pid_gains["Kp"][idx]
            ki = self.pid_gains["Ki"][idx]
            kd = self.pid_gains["Kd"][idx]
            self.integrals[idx] = max(-150.0, min(150.0, self.integrals[idx] + error * dt))
            derivative = (error - self.prev_errors[idx]) / max(0.01, dt)
            output = kp * error + ki * self.integrals[idx] + kd * derivative
            self.prev_errors[idx] = error
            output = max(0.0, min(100.0, output))
        self.outputs[idx] = float(max(0.0, min(100.0, output)))
        return self.outputs[idx]

    def _simulate_step(self, dt=1.0):
        outs = [self._controller_output(i, dt) for i in range(4)]
        # Las dos bombas tienen ramas a un tanque superior y al tanque inferior cruzado.
        req_101 = outs[0] if self.valves[0] and self.pumps[0] else 0.0
        req_104 = outs[3] if self.valves[3] and self.pumps[0] else 0.0
        req_102 = outs[1] if self.valves[1] and self.pumps[1] else 0.0
        req_103 = outs[2] if self.valves[2] and self.pumps[1] else 0.0
        p1_scale = min(1.0, 100.0 / max(1.0, req_101 + req_104))
        p2_scale = min(1.0, 100.0 / max(1.0, req_102 + req_103))
        req_101 *= p1_scale; req_104 *= p1_scale
        req_102 *= p2_scale; req_103 *= p2_scale

        # Balance normalizado (%/s) según el P&ID:
        # T-103 -> T-101; T-104 -> T-102; T-101/T-102 -> TK-105;
        # TK-105 alimenta las dos bombas P-101 y P-102.
        # Al disminuir el nivel del depósito, la capacidad de aspiración baja.
        supply_factor = max(0.0, min(1.0, self.level_tk105 / 15.0))
        req_101 *= supply_factor
        req_102 *= supply_factor
        req_103 *= supply_factor
        req_104 *= supply_factor

        # Caudales mostrados en L/min (escalas de simulación, no caudalímetros calibrados).
        self.flows = [2.8 * min(100.0, req_101 + req_104) / 100.0,
                      2.8 * min(100.0, req_102 + req_103) / 100.0,
                      2.0 * req_103 / 100.0,
                      2.0 * req_104 / 100.0]

        q_101 = 0.044 * req_101  # P-101 -> T-101
        q_102 = 0.044 * req_102  # P-102 -> T-102
        q_103 = 0.030 * req_103  # P-102 -> T-103
        q_104 = 0.030 * req_104  # P-101 -> T-104
        g_103_101 = 0.030 * math.sqrt(max(0.0, self.levels[2]))
        g_104_102 = 0.030 * math.sqrt(max(0.0, self.levels[3]))
        drain_101 = 0.050 * math.sqrt(max(0.0, self.levels[0]))
        drain_102 = 0.050 * math.sqrt(max(0.0, self.levels[1]))

        self.levels[0] = max(0.0, min(100.0,
            self.levels[0] + (q_101 + g_103_101 - drain_101) * dt))
        self.levels[1] = max(0.0, min(100.0,
            self.levels[1] + (q_102 + g_104_102 - drain_102) * dt))
        self.levels[2] = max(0.0, min(100.0,
            self.levels[2] + (q_103 - g_103_101) * dt))
        self.levels[3] = max(0.0, min(100.0,
            self.levels[3] + (q_104 - g_104_102) * dt))

        # El depósito acumula el retorno de T-101/T-102 y pierde el agua
        # aspirada por ambas bombas. Factor de capacidad didáctico, no calibrado.
        return_to_tk105 = drain_101 + drain_102
        pump_withdrawal = q_101 + q_102 + q_103 + q_104
        reservoir_capacity_factor = 4.0
        self.level_tk105 = max(0.0, min(100.0,
            self.level_tk105 +
            ((return_to_tk105 - pump_withdrawal) / reservoir_capacity_factor) * dt))
        # Modelo térmico simple: mezcla con ambiente y leve calentamiento asociado al bombeo.
        ambient = 22.0
        for i in range(4):
            flow_effect = min(1.0, self.flows[min(i, 3)] / 3.0)
            target = ambient + (0.6 if (self.pumps[0] or self.pumps[1]) else 0.0) + 1.2 * flow_effect
            self.temperatures[i] += (target - self.temperatures[i]) * 0.018 * dt
            self.temperatures[i] = max(0.0, min(100.0, self.temperatures[i]))

    def scan_process(self):
        self.scan_count += 1
        if self.comms_mode == "SIM":
            self.comms_ok = not self.sim_fault
            if self.comms_ok and not self.emergency_stopped:
                self._simulate_step(1.0)
            elif self.emergency_stopped:
                # Congela el estado hidráulico y lleva a cero las órdenes/caudales simulados.
                self.outputs = [0.0] * 4
                self.flows = [0.0] * 4
        elif self.comms_mode == "MODBUS":
            self._poll_modbus()  # se sigue leyendo para verificar el estado remoto
        elif self.comms_mode == "MQTT":
            self._consume_mqtt_snapshot()  # se sigue leyendo la telemetría remota

        self._update_alarm_list()
        self.time_history.append(self.scan_count)
        for i in range(4):
            self.level_history[i].append(self.levels[i])
            self.temp_history[i].append(self.temperatures[i])
            self.flow_history[i].append(self.flows[i])
        self.level_history[4].append(self.level_tk105)
        self.refresh_trend_plot()

    # ----------------------------- Alarmas
    def _update_alarm_list(self):
        now = datetime.now()
        current = {}
        if self.emergency_stopped:
            current["ESD-001"] = ("ALTA", "Parada de emergencia por software enclavada; verificar paro físico/controlador")
        if not self.comms_ok:
            current["COMMS-001"] = ("ALTA", "Pérdida de comunicación / calidad de dato no confiable")
        else:
            for i, value in enumerate(self.levels):
                tag = f"LT{101+i}"
                if value >= 90:
                    current[f"{tag}-HH"] = ("ALTA", f"{self.TANK_TAGS[i]}: nivel muy alto")
                elif value >= 80:
                    current[f"{tag}-H"] = ("MEDIA", f"{self.TANK_TAGS[i]}: nivel alto")
                if value <= 10:
                    current[f"{tag}-LL"] = ("ALTA", f"{self.TANK_TAGS[i]}: nivel muy bajo")
                elif value <= 20:
                    current[f"{tag}-L"] = ("MEDIA", f"{self.TANK_TAGS[i]}: nivel bajo")
                if self.temperatures[i] >= 45:
                    current[f"TT{101+i}-H"] = ("MEDIA", f"{self.TANK_TAGS[i]}: temperatura alta")
            # Alarmas didácticas de LT-105 en porcentaje; parametrizar con la altura real.
            if self.level_tk105 >= 90:
                current["LT105-HH"] = ("ALTA", "TK-105: nivel muy alto")
            elif self.level_tk105 >= 80:
                current["LT105-H"] = ("MEDIA", "TK-105: nivel alto")
            if self.level_tk105 <= 10:
                current["LT105-LL"] = ("ALTA", "TK-105: nivel muy bajo")
            elif self.level_tk105 <= 20:
                current["LT105-L"] = ("MEDIA", "TK-105: nivel bajo")
        for code, (priority, message) in current.items():
            if code not in self._active_alarms:
                self._active_alarms[code] = {"priority": priority, "message": message, "since": now}
                self._acked_alarms.discard(code)
                self.log("ALARM", f"{code}: {message}", self.role)
        for code in list(self._active_alarms):
            if code not in current:
                self.log("ALARM", f"{code}: condición retornó a normal", self.role)
                self._active_alarms.pop(code, None)
                self._acked_alarms.discard(code)
                self._shelved_alarms.pop(code, None)
        for code, until in list(self._shelved_alarms.items()):
            if now >= until:
                self._shelved_alarms.pop(code, None)

    def refresh_alarms(self):
        codes = [code for code, alarm in self._active_alarms.items()
                 if code not in self._shelved_alarms]
        codes.sort(key=lambda c: (0 if self._active_alarms[c]["priority"] == "ALTA" else 1,
                                  self._active_alarms[c]["since"]))
        self._visible_alarm_codes = codes
        self.alarm_table.setRowCount(len(codes))
        for row, code in enumerate(codes):
            alarm = self._active_alarms[code]
            status = "Activa reconocida" if code in self._acked_alarms else "Activa no reconocida"
            values = [code, alarm["priority"], alarm["message"], status,
                      alarm["since"].strftime("%H:%M:%S"), "ACK" if code not in self._acked_alarms else "—"]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if col in (0, 1):
                    item.setForeground(QColor(PRIORITY_COLOR[alarm["priority"]]))
                self.alarm_table.setItem(row, col, item)
        counts = {p: 0 for p in ("ALTA", "MEDIA", "BAJA")}
        for code in codes:
            counts[self._active_alarms[code]["priority"]] += 1
        total = len(codes)
        if total == 0:
            self.alarm_summary.setText("Estado de alarmas: NORMAL")
            self.alarm_summary.setStyleSheet(f"background:white;border:1px solid {COLORS['border']};border-radius:6px;padding:8px;color:{COLORS['text']};")
        else:
            parts = ", ".join(f"{n} {priority}" for priority, n in counts.items() if n)
            top = self._active_alarms[codes[0]]
            color = PRIORITY_COLOR[top["priority"]]
            self.alarm_summary.setText(f"Alarmas activas: {total} ({parts}) — {top['message']}")
            self.alarm_summary.setStyleSheet(f"background:{color};border:1px solid {color};border-radius:6px;padding:8px;color:white;font-weight:700;")

    def ack_all_alarms(self):
        if self.read_only:
            return
        self._acked_alarms.update(self._active_alarms.keys())
        self.log("ALARM", "Reconocimiento global ACK ALL", self.role)
        self.refresh_alarms()

    def ack_selected_alarm(self, row, col=0):
        if self.read_only or row < 0 or row >= len(self._visible_alarm_codes):
            return
        code = self._visible_alarm_codes[row]
        self._acked_alarms.add(code)
        self.log("ALARM", f"Reconocimiento: {code}", self.role)
        self.refresh_alarms()

    def shelve_selected_alarm(self):
        if self.read_only:
            return
        row = self.alarm_table.currentRow()
        if 0 <= row < len(self._visible_alarm_codes):
            code = self._visible_alarm_codes[row]
            self._shelved_alarms[code] = datetime.now() + timedelta(minutes=10)
            self.log("ALARM", f"Shelving por 10 minutos: {code}", self.role)
            self.refresh_alarms()

    # ----------------------------- Guía del estudiante / rol
    def _build_student_page(self):
        """Guía didáctica detallada: explica los criterios, tags y mapas utilizados."""
        page_layout = self._ensure_layout(self.student_container, QVBoxLayout)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(6, 6, 10, 10)
        root.setSpacing(10)

        intro = QLabel(
            "Esta guía explica por qué se organizó así la interfaz, cómo se identifican los "
            "instrumentos, qué representan las alarmas y cómo se intercambian datos con un PLC/ESP32. "
            "Los criterios son referencias didácticas; el prototipo no constituye una certificación normativa."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"background:white;border:1px solid {COLORS['border']};border-radius:6px;padding:10px;")
        root.addWidget(intro)

        # Panel visual de referencia inspirado en la lámina normativa aportada.
        norm_panel = QFrame()
        norm_panel.setObjectName("normPanel")
        norm_panel.setStyleSheet(
            "QFrame#normPanel {background:#ffffff;border:3px solid #808080;border-radius:10px;}"
        )
        panel_layout = QVBoxLayout(norm_panel)
        panel_layout.setContentsMargins(14, 12, 14, 12)
        panel_layout.setSpacing(8)
        panel_title = QLabel("PANEL NORMATIVO DIDÁCTICO")
        panel_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_title.setStyleSheet("color:#000000;font-size:16pt;font-weight:900;padding:6px;")
        panel_layout.addWidget(panel_title)

        norm_sections = [
            ("1. ISA-5.1 · IDENTIFICACIÓN DE INSTRUMENTOS",
             "Formato didáctico: variable medida + función del instrumento + número de lazo. La primera letra identifica la variable "
             "medida (L = nivel, F = caudal, T = temperatura); la siguiente indica la función (T = transmisor). "
             "Ejemplos usados: LT-101 = Level Transmitter (transmisor de nivel), lazo 101; "
             "FT-101 = Flow Transmitter (transmisor de caudal), lazo 101; "
             "TT-101 = Temperature Transmitter (transmisor de temperatura), lazo 101. "
             "P-101 identifica una bomba y CV-101 una válvula de control según la convención de tags del proyecto. "
             "El prefijo ayuda a distinguir una medición de un elemento final de control. Mantenga el mismo tag en P&ID, PLC, tabla de instrumentos, alarmas y HMI."),
            ("2. ISA-101 · DISEÑO DE HMI DE ALTO DESEMPEÑO",
             "La interfaz separa una visión general del proceso, una página de control, tendencias, alarmas, "
             "comunicaciones, instrumentación y cuatro HMI individuales. La navegación conserva la misma ubicación "
             "y nomenclatura. La paleta utiliza superficies neutras y reserva los colores más intensos para estados, "
             "alarmas o equipos en condición anormal. El Overview resume el proceso y el P&ID; Control reúne lazos, "
             "bombas, válvulas y paro software; Tendencias muestra históricos; Alarmas lista prioridad/ACK; "
             "Instrumentación documenta tags y direcciones; Comunicación configura SIM/Modbus/MQTT; y cada HMI "
             "individual profundiza en un tanque. Los valores críticos se muestran con unidades; si se pierde "
             "comunicación se muestran guiones y el estado de dato no confiable, en vez de presentar una lectura "
             "congelada como si fuera válida. ISA-101 no obliga a usar un tema oscuro: el panel oscuro de la lámina "
             "es una decisión de presentación, mientras que contraste, jerarquía, consistencia y claridad son los objetivos."),
            ("3. ISA-18.2 / IEC 62682 · GESTIÓN DE ALARMAS",
             "Una alarma debe indicar una condición anormal que requiere respuesta del operador. La aplicación "
             "muestra prioridad (ALTA/MEDIA), condición, hora y estado de reconocimiento; permite ACK global, "
             "reconocimiento desde la tabla y silenciamiento temporal (shelving). Cuando la condición vuelve a normal, "
             "el evento deja de estar activo y queda registrado en la bitácora. En este prototipo, para T-101…T-104 "
             "los ejemplos activos son nivel bajo ≤20 %, muy bajo ≤10 %, alto ≥80 %, muy alto ≥90 % y temperatura "
             "alta ≥45 °C. Para TK-105 se aplican los mismos umbrales porcentuales de demostración. Deben racionalizarse "
             "y ajustarse con datos de ingeniería antes de cualquier uso real."),
            ("4. COMUNICACIÓN · MODBUS TCP Y ESCALAMIENTO",
             "El SCADA opera como cliente Modbus TCP. En Pymodbus se utiliza direccionamiento con offset base cero: "
             "el offset 0 suele corresponder al holding register 40001 en documentación que usa la notación 4xxxx. "
             "Mapa de este proyecto: HR 0…3 = LT-101…LT-104 ×10 en %; HR 4…7 = TT-101…TT-104 ×10 en °C; "
             "HR 8…11 = FT-101…FT-104 ×100 en L/min; HR 12 = LT-105 ×10 en %; HR 20…23 = consignas ×10; "
             "HR 24…27 = modo (0 PID, 1 ON/OFF, 2 Manual); HR 28…31 = salida manual ×10. Coils 0…1 son bombas, "
             "coils 2…5 válvulas y coil 6 la solicitud de paro software. Verifique tipo INT/UINT, endianess, offset, "
             "unidades y escala con el firmware del controlador. La posición segura de cada válvula debe definirse por análisis de riesgos; cerrar todas es solo el estado didáctico actual. La lámina aportada indica 40001, INT y escala 0.1 cm; "
             "este código actual interpreta LT-101 en porcentaje (0.1 % por cuenta), no en centímetros. Para utilizar cm "
             "hay que definir altura útil del tanque y convertir: nivel_cm = nivel_pct × altura_cm / 100."),
            ("5. ISA-18.2 · EJEMPLO DE LÍMITES EN cm DE LA LÁMINA",
             "La referencia visual muestra LA+ = 22 cm y LA++ = 27 cm para nivel alto y extremo alto. Son valores de "
             "ejemplo del panel proporcionado y NO están conectados automáticamente a los límites porcentuales del "
             "programa. Para activarlos se debe conocer la altura útil del tanque, convertir las medidas a cm, definir "
             "la prioridad y la acción requerida, y validar los puntos de alarma con el responsable del proceso. "
             "No se deben mezclar cm y % en una misma comparación."),
            ("6. ISA-5.1 + P&ID · RELACIÓN ENTRE EL DIBUJO Y LAS VARIABLES",
             "El P&ID del Overview coloca T-103/T-104 arriba, T-101/T-102 abajo y TK-105 como depósito principal. "
             "Las flechas identifican alimentación y gravedad; los círculos identifican LT, TT y FT; las bombas y "
             "válvulas reflejan su estado. LT-105 ahora representa el nivel dinámico de TK-105 en simulación y se "
             "puede recibir por Modbus HR 12 o por MQTT como level_tk105. TK-105 es supervisado, pero no forma parte "
             "de los cuatro lazos PID de nivel."),
            ("7. MODOS DE CONTROL · PID, ON/OFF Y MANUAL",
             "PID calcula una salida a partir del error SP − PV y de las ganancias Kp, Ki y Kd; cada tanque tiene "
             "ganancias independientes. ON/OFF conmuta alrededor de una banda de histéresis. Manual utiliza la "
             "salida fijada por el usuario. En este prototipo, las ganancias se aplican al modelo de simulación; para "
             "control remoto se requiere implementar su mapa y algoritmo en PLC/ESP32. La simulación es un modelo "
             "didáctico normalizado, no un gemelo digital validado ni un ajuste PID certificado."),
            ("8. IEC 62443 · SEGURIDAD DE ACCESO Y COMUNICACIÓN",
             "Los perfiles estudiante, operario e invitado ilustran separación de permisos; invitado es solo lectura. "
             "La bitácora registra cambios de perfil, órdenes, ajustes y eventos. Sin embargo, los perfiles actuales "
             "son demostrativos, no tienen autenticación real y no sustituyen cuentas individuales, contraseñas, "
             "segmentación de red, gestión de parches, listas de control, cifrado TLS ni gestión de certificados. "
             "El modo MQTT de este prototipo no configura TLS ni ACL: esos controles deben añadirse para un despliegue real."),
            ("9. ISO 9241 · USABILIDAD Y PREVENCIÓN DE ERRORES",
             "Los controles deben tener etiquetas comprensibles, unidades y rangos visibles, realimentación después "
             "de cada acción, navegación consistente y estados diferenciados. Las acciones de mayor consecuencia "
             "deben evitar activaciones ambiguas. El reconocimiento de alarmas no elimina la condición de proceso: "
             "solo registra que el operador la ha visto."),
            ("10. IEC 61511 / PARADA DE EMERGENCIA · LÍMITE DE ESTE PROTOTIPO",
             "El botón rojo de esta HMI genera una solicitud de parada por software: congela la simulación, pone las "
             "órdenes locales de bombas a STOP, cierra las válvulas del modelo y envía la solicitud por Modbus coil 6 "
             "o el campo MQTT emergency_stop. El PLC/ESP32 debe programar y confirmar ese enclavamiento. Esto NO es "
             "un dispositivo de parada de emergencia de seguridad: la parada real debe ejecutarse mediante circuito "
             "físico apropiado, relé/controlador de seguridad, evaluación de riesgos y diseño validado. El rearme "
             "software no arranca automáticamente los actuadores ni sustituye el rearme físico."),
        ]
        for title_text, body_text in norm_sections:
            title_label = QLabel(title_text)
            title_label.setWordWrap(True)
            title_label.setStyleSheet("color:#000000;font-size:10pt;font-weight:800;padding-top:4px;")
            body_label = QLabel(body_text)
            body_label.setWordWrap(True)
            body_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body_label.setStyleSheet("color:#000000;font-size:9pt;padding:2px 4px 7px 4px;")
            panel_layout.addWidget(title_label)
            panel_layout.addWidget(body_label)
            separator = QFrame()
            separator.setFrameShape(QFrame.Shape.HLine)
            separator.setStyleSheet("color:#686868;background:#686868;max-height:1px;")
            panel_layout.addWidget(separator)
        root.addWidget(norm_panel)

        decisions = QGroupBox("Asistente de toma de decisiones (orientativo)")
        decisions_layout = QVBoxLayout(decisions)
        self.decision_label = QLabel("Analizando condiciones del proceso…")
        self.decision_label.setWordWrap(True)
        self.decision_label.setMinimumHeight(130)
        self.decision_label.setStyleSheet(
            f"font-size:11pt;font-weight:400;background:#f7f9fa;border:1px solid {COLORS['border']};"
            "border-radius:6px;padding:12px;"
        )
        decisions_layout.addWidget(self.decision_label)
        root.addWidget(decisions)

        note = QLabel(
            "Referencias estudiadas: ISA-5.1 (identificación), ISA-101 (HMI), ISA-18.2 e IEC 62682 "
            "(alarmas), Modbus TCP (comunicación), IEC 62443 (ciberseguridad), IEC 61511 (seguridad funcional) "
            "e ISO 9241 (usabilidad). Se incluyen con fines didácticos; compruebe la edición vigente, el alcance "
            "y los requisitos aplicables antes de declarar conformidad."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{COLORS['muted']};background:white;border:1px solid {COLORS['border']};border-radius:6px;padding:10px;")
        root.addWidget(note)

        reminder = QLabel(
            "Uso académico: las recomendaciones son reglas didácticas de apoyo. El estudiante debe justificar "
            "la maniobra con tendencias, alarmas, permisos de proceso y criterio del instructor. No usar la HMI "
            "ni el botón software como sistema de seguridad de una planta real."
        )
        reminder.setWordWrap(True)
        reminder.setStyleSheet(f"color:{COLORS['muted']};")
        root.addWidget(reminder)
        root.addStretch(1)

        scroll.setWidget(content)
        page_layout.addWidget(scroll)

    def _change_role(self, new_role):
        """Cambia el perfil activo en caliente, preservando el estado del proceso."""
        if new_role not in ("estudiante", "operario", "invitado") or new_role == self.role:
            return
        previous = self.role
        self.role = new_role
        self.read_only = new_role == "invitado"
        self.log("ROLE_CHANGE", f"Cambio de perfil: {previous} -> {new_role}", new_role)
        self._apply_role_permissions()
        self.refresh_ui()

    def _apply_role_permissions(self):
        """Recalcula permisos al cambiar de perfil; no reinicia el proceso."""
        self.read_only = self.role == "invitado"
        self.role_label.setText(f"ROL: {self.role.upper()}")
        self.role_selector.setToolTip("Perfil académico de demostración; no sustituye autenticación real de usuarios.")
        if self.role == "estudiante":
            self.role_info.setText("ESTUDIANTE\nGuía didáctica activa; controles disponibles en el entorno académico.")
            self.buttons["student"].setVisible(True)
            self.overview_advice.setVisible(True)
        elif self.role == "operario":
            self.role_info.setText("OPERARIO\nSupervisión y control del proceso; sin guía formativa.")
            self.buttons["student"].setVisible(False)
            self.overview_advice.setVisible(False)
        else:
            self.role_info.setText("INVITADO\nSolo lectura. Se conserva el estado actual del proceso.")
            self.buttons["student"].setVisible(False)
            self.overview_advice.setVisible(False)

        # Si el usuario estaba en la guía, evita dejar una página sin acceso visible.
        if self.role != "estudiante" and self.stacked.currentIndex() == 6:
            self.stacked.setCurrentIndex(0)

        self.comm_help_label.setVisible(self.role == "estudiante")
        self.comm_map_guide.setVisible(self.role == "estudiante")

        # Primero se restauran los controles al cambiar desde invitado; luego se aplica
        # la política del perfil actual. Las páginas de HMI por tanque siguen visibles.
        editable_types = (QPushButton, QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit)
        # Los mandos de proceso quedan bloqueados con E-Stop; la página de
        # comunicación permanece utilizable para recuperar el enlace remoto.
        process_roots = [self.control_container]
        process_roots.extend(entry["page"] for entry in getattr(self, "tank_page_widgets", []))
        for root in process_roots:
            for widget in root.findChildren(QWidget):
                if isinstance(widget, editable_types):
                    widget.setEnabled(not self.read_only and not self.emergency_stopped)
        for widget in self.comm_container.findChildren(QWidget):
            if isinstance(widget, editable_types):
                widget.setEnabled(not self.read_only)

        self.buttons["comm_fault"].setEnabled(
            not self.read_only and not self.emergency_stopped and self.comms_mode == "SIM"
        )
        self.buttons["ack_all"].setEnabled(not self.read_only)
        self.buttons["shelve"].setEnabled(not self.read_only)
        # La solicitud de paro se mantiene disponible incluso en perfil invitado.
        self.btn_emergency_stop.setEnabled(True)
        self.btn_emergency_reset.setEnabled(not self.read_only and self.emergency_stopped)
        for i in range(4):
            self.control_manuals[i].setEnabled(
                not self.read_only and not self.emergency_stopped and self.modes[i] == "Manual"
            )
            if self.tank_page_widgets:
                self.tank_page_widgets[i]["manual"].setEnabled(
                    not self.read_only and not self.emergency_stopped and self.modes[i] == "Manual"
                )
        self.log("PERMISSIONS", f"Permisos aplicados al perfil {self.role}; solo_lectura={self.read_only}", self.role)

    def _decision_text(self):
        if self.emergency_stopped:
            return ("PARADA DE EMERGENCIA SOFTWARE ACTIVA. No intentes rearmar hasta identificar "
                    "la causa y verificar el dispositivo físico/controlador. Los actuadores no se "
                    "arrancan automáticamente al liberar el enclavamiento.")
        if not self.comms_ok:
            return "1. Calidad de datos: NO CONFIABLE. No tomes decisiones de proceso con valores congelados.\n2. Verifica la red, servidor/broker y estado del enlace.\n3. Restablece comunicación y confirma que las medidas se actualicen antes de volver al control normal."
        high = [(self.TANK_TAGS[i], v) for i, v in enumerate(self.levels) if v >= 80]
        low = [(self.TANK_TAGS[i], v) for i, v in enumerate(self.levels) if v <= 20]
        if self.level_tk105 >= 80:
            high.append(("TK-105", self.level_tk105))
        if self.level_tk105 <= 20:
            low.append(("TK-105", self.level_tk105))
        hot = [(self.TANK_TAGS[i], v) for i, v in enumerate(self.temperatures) if v >= 45]
        messages = []
        if high:
            messages.append("Nivel alto/muy alto en " + ", ".join(f"{t} ({v:.1f}%)" for t, v in high) + ". Verifica la tendencia y reduce aporte si la maniobra está permitida.")
        if low:
            messages.append("Nivel bajo/muy bajo en " + ", ".join(f"{t} ({v:.1f}%)" for t, v in low) + ". Comprueba bomba, válvulas y disponibilidad de alimentación.")
        if hot:
            messages.append("Temperatura alta en " + ", ".join(f"{t} ({v:.1f} °C)" for t, v in hot) + ". Revisa la evolución térmica y el origen de la condición.")
        if not self.pumps[0] and not self.pumps[1]:
            messages.append("Ambas bombas están detenidas: el aporte activo se encuentra deshabilitado.")
        if not messages:
            messages.append("No se detectan desviaciones en los límites didácticos configurados. Compara consigna y valor real, revisa tendencias y confirma el modo de control activo.")
        messages.append("Antes de actuar: confirma la causa, revisa permisos y evita maniobras repetitivas. Umbrales de entrenamiento, no límites certificados.")
        return "\n\n".join(f"{i+1}. {m}" for i, m in enumerate(messages))

    # ----------------------------- Refresco visual
    def refresh_ui(self):
        self.clock.setText(datetime.now().strftime("%d/%m/%Y  %H:%M:%S"))
        if self.comms_ok:
            self.metric_labels["metricLevel"].setText(f"{(sum(self.levels) + self.level_tk105)/5:.1f} %")
            self.metric_labels["metricTK105"].setText(f"{self.level_tk105:.1f} %")
            self.metric_labels["metricTemp"].setText(f"{sum(self.temperatures)/4:.1f} °C")
            self.metric_labels["metricFlow"].setText(f"{sum(self.flows):.2f} L/min")
            self.metric_labels["metricComms"].setText("OK")
            self.metric_labels["metricComms"].setStyleSheet(f"font-size:17pt;font-weight:700;color:{COLORS['good']};border:0;")
        else:
            for key in ("metricLevel", "metricTK105", "metricTemp", "metricFlow"):
                self.metric_labels[key].setText("---")
                self.metric_labels[key].setStyleSheet(f"font-size:17pt;font-weight:700;color:{COLORS['quality']};border:0;")
            self.metric_labels["metricComms"].setText("FALLA")
            self.metric_labels["metricComms"].setStyleSheet(f"font-size:17pt;font-weight:700;color:{COLORS['alarm']};border:0;")

        self.pid_canvas.update()
        for i in range(4):
            current = self.control_table.item(i, 1)
            if current:
                current.setText("---" if not self.comms_ok else f"{self.levels[i]:.1f} %")
            self.control_value_labels[i].setText("---" if (not self.comms_ok or self.comms_mode != "SIM") else f"{self.outputs[i]:.1f} %")
            controls_enabled = not self.read_only and not self.emergency_stopped
            self.control_modes[i].setEnabled(controls_enabled)
            self.control_setpoints[i].setEnabled(controls_enabled)
            self.control_manuals[i].setEnabled(controls_enabled and self.modes[i] == "Manual")
            for key in ("Kp", "Ki", "Kd"):
                self.pid_gain_widgets[key][i].setEnabled(controls_enabled and self.comms_mode == "SIM")
        for i, btn in enumerate(self.btn_pumps):
            btn.setEnabled(not self.read_only and not self.emergency_stopped)
            btn.setText(f"P-{101+i}: {'RUN' if self.pumps[i] else 'STOP'}")
            btn.setStyleSheet(f"background:{'#e1f2e8' if self.pumps[i] else '#eceff1'};border:1px solid {COLORS['border']};border-radius:5px;padding:8px;")
        for i, btn in enumerate(self.btn_valves):
            btn.setText(f"{['V-101 · P101→T101','V-102 · P102→T102','V-103 · P102→T103','V-104 · P101→T104'][i]} · {'ABIERTA' if self.valves[i] else 'CERRADA'}")
            btn.setEnabled(not self.read_only and not self.emergency_stopped)

        self.btn_emergency_stop.setEnabled(True)
        self.btn_emergency_stop.setText("PARO\nACTIVO" if self.emergency_stopped else "PARO\nEMERGENCIA")
        self.btn_emergency_reset.setEnabled(not self.read_only and self.emergency_stopped)
        if self.emergency_stopped:
            if self.comms_mode == "SIM":
                status_text = "PARADA SOFTWARE ACTIVA · simulación congelada"
            elif self.comms_mode == "MODBUS":
                if self.remote_estop_feedback is True:
                    status_text = "PARADA ACTIVA · PLC confirma coil 6"
                elif self.remote_estop_feedback is False:
                    status_text = "PARADA HMI ACTIVA · PLC no confirma coil 6"
                else:
                    status_text = "PARADA HMI ACTIVA · sin realimentación remota"
            else:
                if self.remote_estop_feedback is True:
                    status_text = "PARADA ACTIVA · telemetría confirma estado remoto"
                else:
                    status_text = "PARADA HMI ACTIVA · confirme recepción en el controlador"
            self.emergency_status_label.setText(status_text)
            self.emergency_status_label.setStyleSheet(
                "background:#fde8e7;color:#9e1515;border:2px solid #c42b1c;"
                "border-radius:5px;padding:5px;font-weight:900;"
            )
        else:
            self.emergency_status_label.setText("ESTADO: NORMAL · actuadores no arrancan automáticamente")
            self.emergency_status_label.setStyleSheet(
                "background:#e8f3eb;color:#24613f;border:1px solid #9fc8ad;"
                "border-radius:5px;padding:5px;font-weight:700;"
            )

        self.refresh_alarms()
        self._refresh_tank_pages()
        self.buttons["comm_fault"].setEnabled(not self.read_only and self.comms_mode == "SIM")
        if self.role == "estudiante":
            advice = self._decision_text()
            self.overview_advice.setText("ASISTENCIA: " + advice.replace("\n", " · "))
            self.decision_label.setText(advice)
        if self.comms_mode == "SIM":
            if self.sim_fault:
                self.comm_status_label.setText("Estado: falla de comunicación simulada; medidas congeladas")
            elif self.comm_status_label.text().startswith("Estado: falla"):
                self.comm_status_label.setText("Estado: simulación académica activa (sin hardware)")
        elif self.comms_mode == "MQTT":
            if not self.comms_ok and self.mqtt_connected:
                self.comm_status_label.setText("Estado: MQTT sin telemetría reciente (más de 5 s); valores no confiables")
            elif self.mqtt_status_message:
                self.comm_status_label.setText(self.mqtt_status_message)

    # ----------------------------- Cierre / ejecución
    def request_close(self):
        if self.read_only:
            self._close_communication_clients()
            self.ui.close()
            return
        high_unacked = [code for code, a in self._active_alarms.items()
                        if a["priority"] == "ALTA" and code not in self._acked_alarms]
        if high_unacked:
            answer = QMessageBox.question(self.ui, "Alarmas ALTA no reconocidas",
                f"Hay {len(high_unacked)} alarma(s) ALTA sin reconocer. ¿Desea cerrar de todas formas?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.log("SYS", "Cierre de sesión", self.role)
        self._close_communication_clients()
        self.ui.close()

    def run(self):
        self.ui.show()
        return self.app.exec()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        scada = SCADA()
        sys.exit(scada.run())
    except Exception as exc:
        QMessageBox.critical(None, "Error al iniciar SCADA", f"{exc}\n\nRevise main_window.ui (o ui/main_window.ui) y las dependencias del proyecto.")
        raise


if __name__ == "__main__":
    main()
