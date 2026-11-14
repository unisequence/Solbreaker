"""Qt editor for a Solbreaker tank project."""
from dataclasses import replace
from pathlib import Path
import json
import shutil
import sys
from time import monotonic

import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, QSettings, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QPolygonF, QRadialGradient, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QMenu, QPushButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from .. import dava_scene, dvpl
from ..build_info import BUILD_ID
from .i18n import tr
from .models import POINT_NODE, TRACK_NAMES, model_preview, texture_refs
from .mounting import MAX_CORRECTION, MESH_NAMES, MOUNTS, barrel_in_hull, check_mount, seat_mount, translate_mount
from .package import export, preview_pair, stock_assets, stock_parts, stock_project
from .parts import CATALOG, body_preview, catalog_matches, select_part as select_donor_part
from .project import ASSET_FIELDS, DONOR_PARTS, FIT_DEFAULTS, NUMBER_FIELDS, PARTS, TankProject, VEHICLE_CLASSES
from .running_gear import donor_catalog, donor_parameters, donor_preview, fit_to_model, fit_vectors, fitted_parts, track_loops

PART_LABELS = dict(hull='Корпус', turret='Башня', gun='Орудие', muzzle='Дуло')
SLOT_LABELS = dict(hull='Корпус', turret='Башня', gun='Орудие', gear='Ходовая')
PART_COLORS = dict(hull='#8fbea7', turret='#d5b2eb', gun='#ffd296', muzzle='#ee9388')
FIELD_LABELS = dict(id='ID пакета', name='Название', base_vehicle='Базовый танк', vehicle_id='ID в клиенте',
    tier='Уровень', vehicle_class='Класс', hit_points='Прочность, HP', shell_damage='Урон снаряда, HP',
    forward_kmh='Вперёд, км/ч', reverse_kmh='Назад, км/ч', engine_hp='Двигатель, л. с.',
    reload_seconds='Перезарядка, с', armor_hull_mm='Броня корпуса, мм',
    armor_turret_mm='Броня башни, мм', armor_gun_mm='Броня орудия, мм')

STYLE = """
QMainWindow, QWidget#surface { background: #1b1d1b; color: #ece8dc; }
QWidget { color: #ece8dc; }
QFrame#toolbar { background: #252823; border-bottom: 1px solid #44463d; }
QLabel#title { font-size: 21px; font-weight: 650; }
QLabel#muted { color: #c1bbaa; }
QLabel#catalog-title { font-size: 21px; font-weight: 650; }
QLabel#project-title { font-size: 27px; font-weight: 650; }
QFrame#mount-panel { background: #252823; border: 1px solid #454838; border-radius: 5px; }
QLabel#mount-note { font-weight: 600; color: #c7d4ac; }
QLabel#mount-note[warning="true"] { color: #f1bf83; }
QPushButton { background: #30332d; border: 1px solid #53574c; border-radius: 5px; padding: 7px 12px; }
QPushButton:hover, QPushButton:focus { border-color: #d6ae70; }
QPushButton#primary { color: #21190f; background: #e5b877; border-color: #e5b877; font-weight: 650; }
QPushButton:disabled, QPushButton#primary:disabled { color: #a6a496; background: #30332d; border-color: #44463d; }
QPushButton#part-slot { text-align: left; padding: 9px 11px; min-height: 26px; }
QPushButton#part-slot:checked { background: #4b3b29; border-color: #d6ae70; }
QLineEdit, QComboBox, QAbstractSpinBox, QListWidget {
    background: #20221f; border: 1px solid #505349; border-radius: 4px; padding: 5px; }
QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus { border-color: #d6ae70; }
QListWidget::item:selected { background: #514232; color: #fff0d0; }
QListWidget#donor-list { background: #20231f; padding: 2px; }
QListWidget#donor-list::item { padding: 4px 9px; border-bottom: 1px solid #373b33; }
QListWidget#donor-list::item:hover { background: #393d34; }
QListWidget#donor-list::item:selected { border-left: 2px solid #e5b877; }
QTabWidget::pane { border: 1px solid #42443b; }
QTabBar::tab { background: #252823; color: #c1bbaa; padding: 9px 11px; }
QTabBar::tab:selected { background: #383b32; color: #f2ead9; }
QScrollBar:vertical { background: #262922; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #77796a; min-height: 28px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollArea { border: none; background: #1b1d1b; }
QSplitter::handle { background: #42443b; width: 3px; }
QLabel#start-title { font-size: 28px; font-weight: 650; }
QFrame#start-divider { background: #44463d; min-height: 1px; max-height: 1px; }
"""


def button(label, callback, parent=None, *, primary=False):
    result = QPushButton(label, parent)
    if primary:
        result.setObjectName('primary')
    result.clicked.connect(callback)
    return result


class Viewport(QWidget):
    picked = Signal(object)
    gear_dragged = Signal(str, float)
    part_clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(380, 340)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.mesh = np.empty((0, 3, 3))
        self.collision_parts = {}
        self.parts = np.empty(0, dtype=int)
        self.colors = [QColor(c) for c in ('#a4a08a', '#bdb499', '#c9c4b3', '#818572')]
        self.points = {}
        self.selected = 'turret'
        self.yaw, self.pitch = .58, .38
        self.zoom = 1.
        self.center = np.zeros(3)
        self.span = 8.
        self.overlay = False
        self.last = None
        self.press_position = None
        self.click_parts = False
        self.picking = False
        self.model_frame = None
        self.frame_key = None
        self.show_points = True
        self.track_loops = []
        self.track_timer = QTimer(self, interval=33)
        self.track_timer.timeout.connect(self.update)
        self.track_started = 0.
        self.gear_controls = False
        self.gear_origin = None
        self.gear_drag = None
        self.mount_marker = None

    def set_track_loops(self, loops):
        self.track_loops = []
        for loop in loops:
            closed = np.vstack((loop, loop[0]))
            lengths = np.r_[0., np.cumsum(np.linalg.norm(np.diff(closed, axis=0), axis=1))]
            if lengths[-1] > 0:
                self.track_loops.append((closed, lengths))

    def animate_tracks(self, enabled):
        if enabled:
            self.track_started = monotonic()
            self.track_timer.start()
        else:
            self.track_timer.stop()
        self.update()

    def set_models(self, visible, collision):
        mesh, colors = [], []
        for name, triangles in visible.items():
            color = 3 if name.startswith('chassis_') else {'turret_01': 1, 'gun_01': 2}.get(name, 0)
            mesh.append(triangles)
            colors.append(np.full(len(triangles), color))
        self.mesh = np.concatenate(mesh) if mesh else np.empty((0, 3, 3))
        self.parts = np.concatenate(colors) if colors else np.empty(0, dtype=int)
        self.frame_key = None
        if len(self.mesh) and self.gear_drag is None:
            points = self.mesh.reshape(-1, 3)
            lo, hi = points.min(0), points.max(0)
            self.center = (lo + hi) / 2
            self.span = max(2., np.max(hi-lo))
        self.collision_parts = {name: m[::max(1, (len(m)+699)//700)] for name, m in collision.items()}
        self.update()

    def render_mesh(self):
        key = self.width(), self.height(), self.yaw, self.pitch, self.zoom
        if key == self.frame_key:
            return self.model_frame
        self.frame_key = key
        # Sort visibility at each pixel. A single depth per triangle hides
        # the wrong faces where long panels cross the turret or track meshes.
        scale = min(1., 850/max(self.width(), self.height()))
        width, height = max(1, round(self.width()*scale)), max(1, round(self.height()*scale))
        self.model_frame = QImage(width, height, QImage.Format.Format_ARGB32)
        self.model_frame.fill(Qt.GlobalColor.transparent)
        pixels = np.frombuffer(self.model_frame.bits(), dtype=np.uint32).reshape(height, width)
        zbuffer = np.full((height, width), -np.inf, dtype=np.float32)
        xy, depth = self.project(self.mesh)
        xy *= scale
        edge_u, edge_v = xy[:, 1]-xy[:, 0], xy[:, 2]-xy[:, 0]
        det = edge_u[:, 0]*edge_v[:, 1]-edge_u[:, 1]*edge_v[:, 0]
        lo = np.maximum(np.floor(xy.min(axis=1)), 0).astype(int)
        hi = np.minimum(np.ceil(xy.max(axis=1)), (width, height)).astype(int)
        normals = np.cross(self.mesh[:, 1]-self.mesh[:, 0], self.mesh[:, 2]-self.mesh[:, 0])
        light = np.abs(normals[:, 1]) / np.maximum(1e-9, np.linalg.norm(normals, axis=1))
        shades = np.minimum(10, (light*10).astype(int))
        palette = np.array([[color.darker(155-5*shade).rgba() for shade in range(11)]
                            for color in self.colors], dtype=np.uint32)
        xs, ys = np.arange(width)+.5, np.arange(height)[:, None]+.5
        indices = np.flatnonzero((np.abs(det) > 1e-8) & (hi > lo).all(axis=1))
        for idx in indices:
            x0, y0 = lo[idx]
            x1, y1 = hi[idx]
            x = xs[x0:x1]-xy[idx, 0, 0]
            y = ys[y0:y1]-xy[idx, 0, 1]
            ux, uy = edge_u[idx]
            vx, vy = edge_v[idx]
            u = (x*vy-y*vx)/det[idx]
            v = (y*ux-x*uy)/det[idx]
            first, second, third = depth[idx]
            z = first+u*(second-first)+v*(third-first)
            zb = zbuffer[y0:y1, x0:x1]
            mask = (u >= 0) & (v >= 0) & (u+v <= 1) & (z > zb)
            zb[mask] = z[mask]
            pixels[y0:y1, x0:x1][mask] = palette[self.parts[idx], shades[idx]]
        return self.model_frame

    def camera(self):
        cy, sy = np.cos(self.yaw), np.sin(self.yaw)
        cp, sp = np.cos(self.pitch), np.sin(self.pitch)
        return np.array(((cy, 0., -sy), (-sy*sp, cp, -cy*sp), (sy*cp, sp, cy*cp)))

    def project(self, points):
        xyz = (points-self.center) @ self.camera().T
        scale = min(self.width(), self.height())*.74*self.zoom/self.span
        xy = xyz[..., :2] * (scale, -scale) + (self.width()/2, self.height()/2)
        return xy, xyz[..., 2]

    def gear_handles(self):
        if not self.gear_controls or self.gear_origin is None:
            return {}
        origin, _ = self.project(self.gear_origin)
        axes = {'lift': np.array([0., 1., 0.]), 'shift': np.array([0., 0., 1.])}
        return {name: (origin, (self.project(self.gear_origin+axis)[0]-origin))
                for name, axis in axes.items()}

    def paint_gear_handles(self, painter):
        for name, (origin, vector) in self.gear_handles().items():
            length = np.linalg.norm(vector)
            if length < 7:
                continue
            tip = origin+vector/length*58
            color = QColor('#a4f0c8' if name == 'lift' else '#ccb8f7')
            painter.setPen(QPen(color, 2))
            painter.drawLine(self.point(origin), self.point(tip))
            painter.setBrush(QColor('#101824'))
            painter.drawEllipse(self.point(tip), 7, 7)

    def point(self, xy):
        return QPointF(*map(float, xy))

    def paintEvent(self, event):
        painter = QPainter(self)
        light = QRadialGradient(self.width()*.48, self.height()*.48, max(self.width(), self.height())*.66)
        light.setColorAt(0, QColor('#48483b'))
        light.setColorAt(.54, QColor('#30332b'))
        light.setColorAt(1, QColor('#1a1d1a'))
        painter.fillRect(self.rect(), light)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#515347'), 1))
        grid = np.ceil(self.span/2)
        for value in range(-int(grid), int(grid)+1):
            for line in (((value, 0, -grid), (value, 0, grid)), ((-grid, 0, value), (grid, 0, value))):
                xy, _ = self.project(np.asarray(line))
                painter.drawLine(*(self.point(p) for p in xy))
        if len(self.mesh):
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self.rect(), self.render_mesh())
        if self.track_timer.isActive():
            self.paint_tracks(painter)
        self.paint_gear_handles(painter)
        if self.mount_marker is not None:
            world, text = self.mount_marker
            pos = self.point(self.project(np.asarray(world))[0])
            painter.setPen(QPen(QColor('#f1bf83'), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(pos, 10, 10)
            width = painter.fontMetrics().horizontalAdvance(text)+16
            x = max(5., min(self.width()-width-5., pos.x()+16))
            y = max(5., min(self.height()-52., pos.y()-30))
            label = QRectF(x, y, width, 24)
            painter.fillRect(label, QColor('#282319'))
            painter.drawText(label, Qt.AlignmentFlag.AlignCenter, text)
        if self.overlay and self.collision_parts:
            painter.setPen(QPen(QColor('#77c6ad'), .8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for name, mesh in self.collision_parts.items():
                point = {'turret_01': 'turret', 'gun_01': 'gun'}.get(name, name)
                xy, _ = self.project(mesh + self.points.get(point, (0., 0., 0.)))
                for triangle in xy:
                    painter.drawPolygon(QPolygonF([self.point(p) for p in triangle]))
        for name, point in self.points.items() if self.show_points else ():
            xy, _ = self.project(np.asarray(point))
            position = self.point(xy)
            color = QColor(PART_COLORS[name])
            painter.setPen(QPen(color, 2 if name == self.selected else 1))
            painter.setBrush(QColor('#101824'))
            painter.drawEllipse(position, 6 if name == self.selected else 4,
                                6 if name == self.selected else 4)
            painter.drawText(position+QPointF(9., -7.), tr(PART_LABELS[name]))
        painter.setPen(QColor('#d2cab7'))
        hint = (tr('Кликни по поверхности, чтобы поставить точку') if self.picking
                else tr('Клик — выбрать · Ручки — сдвинуть · ЛКМ и движение — вращать')
                if self.click_parts and self.gear_handles()
                else tr('Потяни за ручку детали · ЛКМ вне ручек — поворот') if self.gear_handles()
                else tr('Клик — выбрать деталь · ЛКМ и движение — поворот') if self.click_parts
                else tr('ЛКМ и движение — поворот · Колесо — масштаб'))
        painter.drawText(QRectF(14, self.height()-27, self.width()-28, 26), hint)

    def paint_tracks(self, painter):
        distance = (monotonic()-self.track_started)*1.6
        # The near belt helps judge fit without drawing guides through the far side of the hull.
        index = 0 if np.sin(self.yaw) < 0 else -1
        if not self.track_loops:
            return
        loop, lengths = self.track_loops[index]
        phase = (distance + np.linspace(0., lengths[-1], 27, endpoint=False)) % lengths[-1]
        markers = np.array([np.interp(phase, lengths, axis) for axis in loop.T]).T
        outline, _ = self.project(loop)
        xy, _ = self.project(markers)
        painter.setPen(QPen(QColor('#19463d'), 2))
        painter.drawPolyline(QPolygonF([self.point(p) for p in outline]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#a4f0c8'))
        for point in xy:
            painter.drawEllipse(self.point(point), 2.5, 2.5)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = np.array((event.position().x(), event.position().y()))
        for name, (origin, vector) in self.gear_handles().items():
            length = np.linalg.norm(vector)
            if length >= 7 and np.linalg.norm(position-(origin+vector/length*58)) <= 13:
                self.gear_drag = name, vector
                self.last = event.position()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                return
        if self.picking and len(self.mesh):
            hit = self.hit_test(position)
            if hit is not None:
                index, u, v = hit
                tri = self.mesh[index]
                self.picking = False
                self.picked.emit(tri[0]+u*(tri[1]-tri[0])+v*(tri[2]-tri[0]))
                self.update()
            return
        self.last = event.position()
        self.press_position = self.last

    def hit_test(self, position):
        xy, depth = self.project(self.mesh)
        origin, side, top = xy[:, 0], xy[:, 1]-xy[:, 0], xy[:, 2]-xy[:, 0]
        d = position-origin
        det = side[:, 0]*top[:, 1]-side[:, 1]*top[:, 0]
        valid = np.abs(det) > 1e-8
        safe = np.where(valid, det, 1.)
        u = (d[:, 0]*top[:, 1]-d[:, 1]*top[:, 0])/safe
        v = (side[:, 0]*d[:, 1]-side[:, 1]*d[:, 0])/safe
        hits = np.flatnonzero(valid & (u >= 0) & (v >= 0) & (u+v <= 1))
        if not len(hits):
            return None
        z = depth[:, 0]+u*(depth[:, 1]-depth[:, 0])+v*(depth[:, 2]-depth[:, 0])
        index = hits[np.argmax(z[hits])]
        return index, u[index], v[index]

    def mouseMoveEvent(self, event):
        if self.last is not None:
            delta = event.position()-self.last
            self.last = event.position()
            if self.gear_drag:
                name, vector = self.gear_drag
                self.gear_dragged.emit(name, float(np.dot((delta.x(), delta.y()), vector)/np.dot(vector, vector)))
            else:
                if self.press_position is not None:
                    if (event.position()-self.press_position).manhattanLength() < 5:
                        return
                    delta = event.position()-self.press_position
                    self.press_position = None
                self.yaw -= delta.x()*.008
                self.pitch = min(1.5, max(-1.5, self.pitch+delta.y()*.008))
            self.update()

    def mouseReleaseEvent(self, event):
        position = self.press_position
        self.last = None
        self.press_position = None
        self.gear_drag = None
        self.unsetCursor()
        if self.click_parts and position is not None and event.button() == Qt.MouseButton.LeftButton:
            hit = self.hit_test(np.array((position.x(), position.y())))
            if hit is not None:
                self.part_clicked.emit(('hull', 'turret', 'gun', 'gear')[self.parts[hit[0]]])

    def wheelEvent(self, event):
        self.zoom = min(5., max(.3, self.zoom*1.15**(event.angleDelta().y()/120)))
        self.update()


class Job(QObject):
    finished = Signal(object, str)

    def __init__(self, task):
        super().__init__()
        self.task = task

    @Slot()
    def run(self):
        try:
            result = self.task()
        except Exception as error:
            self.finished.emit(None, str(error))
        else:
            self.finished.emit(result, '')


class Editor(QMainWindow):
    def __init__(self, path=None, config=Path('runtime/manager.json'), *, language=None):
        super().__init__()
        self.setWindowTitle('Solbreaker Workshop')
        self.resize(1190, 760)
        self.setMinimumSize(840, 600)
        self.project = TankProject()
        self.path = None
        self.saved = self.project.to_bytes()
        self.refs = set()
        self.fields, self.coordinates, self.asset_labels = {}, {}, {}
        self.thread = None
        self.drive = None
        self.stopping_drive = None
        self.drive_status = ''
        self.drive_timer = QTimer(self, interval=200)
        self.drive_timer.timeout.connect(self.update_drive)
        from ..management import Settings
        self.config = Path(config).resolve()
        self.settings = QSettings('Solbreaker', 'Workshop')
        try:
            settings = Settings.load(self.config)
        except (OSError, ValueError):
            settings = Settings()
        QApplication.instance().setProperty('solbreaker_language', language or settings.gui_language)
        game = self.settings.value('gameDirectory', '', type=str) or settings.game
        self.game_path = Path(game).expanduser() if game else None
        self.loaded_gear = None
        self.gear_mesh = {}
        self.track_loops = []
        self.visual_mesh = {}
        self.collision_mesh = {}
        self.catalog_slot = 'hull'
        self.donor_icons = {}
        self.body_fallback = {}
        self.mounted_visual = {}
        self.mounted_collision = {}
        self.mount_cache = None
        self.undo_parts = []
        self.gear_center = None
        self.donor_available = False
        self.selected = 'turret'
        self.setStyleSheet(STYLE)
        surface = QWidget(objectName='surface')
        body = QVBoxLayout(surface)
        body.setContentsMargins(0, 0, 0, 0)
        toolbar = QFrame(objectName='toolbar')
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(18, 10, 16, 10)
        bar.addWidget(QLabel('Workshop', objectName='title'))
        bar.addStretch()
        bar.addWidget(button(tr('Новый'), self.new))
        bar.addWidget(button(tr('Открыть…'), self.open))
        bar.addWidget(button(tr('Сохранить'), self.save))
        install_button = QPushButton(tr('Установить…'))
        install_menu = QMenu(install_button)
        install_menu.addAction(tr('В клиент'), lambda: self.install('client'))
        install_menu.addAction(tr('На сервер'), lambda: self.install('state'))
        install_button.setMenu(install_menu)
        bar.addWidget(install_button)
        bar.addWidget(button(tr('Экспорт…'), self.export))
        self.drive_button = button(tr('Проверить в игре'), self.test_drive, primary=True)
        bar.addWidget(self.drive_button)
        body.addWidget(toolbar)
        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        self.pages.addWidget(self.start_panel())
        split = QSplitter()
        self.pages.addWidget(split)
        self.viewport = Viewport()
        self.viewport.setAcceptDrops(True)
        self.viewport.installEventFilter(self)
        self.viewport.picked.connect(self.place_point)
        self.viewport.gear_dragged.connect(self.drag_gear)
        self.viewport.part_clicked.connect(self.show_catalog)
        stage = QWidget()
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(0)
        caption = QWidget(objectName='surface')
        title = QHBoxLayout(caption)
        title.setContentsMargins(18, 10, 11, 10)
        self.project_title = QLabel(objectName='project-title')
        self.project_title.setMinimumWidth(0)
        self.project_title.setSizePolicy(self.project_title.sizePolicy().Policy.Ignored,
                                         self.project_title.sizePolicy().Policy.Preferred)
        title.addWidget(self.project_title, 1)
        for label, yaw in ((tr('Спереди'), 0.), (tr('Сбоку'), np.pi/2), (tr('Под углом'), .58)):
            title.addWidget(button(label, lambda yaw=yaw: self.set_view(yaw, .38 if yaw==.58 else 0.)))
        stage_layout.addWidget(caption)
        stage_layout.addWidget(self.viewport, 1)
        split.addWidget(stage)
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(350)
        self.catalog_tab = self.tabs.addTab(self.catalog_panel(), tr('Сборка'))
        self.tabs.addTab(self.attachment_panel(), tr('Точки'))
        self.tabs.addTab(self.parameter_panel(), tr('Параметры'))
        self.tabs.addTab(self.assets_panel(), tr('Ресурсы'))
        self.gear_tab = self.tabs.addTab(self.gear_panel(), tr('Траки'))
        self.tabs.currentChanged.connect(self.change_workspace)
        split.addWidget(self.tabs)
        split.setSizes([840, 350])
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self.status = QLabel(objectName='muted')
        self.status.setContentsMargins(16, 3, 16, 9)
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        self.setCentralWidget(surface)
        QShortcut(QKeySequence.StandardKey.Save, self, self.save)
        QShortcut(QKeySequence.StandardKey.Open, self, self.open)
        self.populate()
        if path is not None:
            self.load(Path(path))
        self.change_workspace(self.tabs.currentIndex())

    def start_panel(self):
        panel = QWidget()
        panel.setAcceptDrops(True)
        panel.installEventFilter(self)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(48, 28, 48, 28)
        outer.addStretch(1)
        content = QWidget()
        content.setMaximumWidth(610)
        layout = QVBoxLayout(content)
        layout.setSpacing(13)
        layout.addWidget(QLabel(tr('Собери танк'), objectName='start-title'))
        layout.addSpacing(16)
        layout.addWidget(QLabel(tr('Начни с модели из установленной игры'), objectName='muted'))
        self.stock_choice = QComboBox()
        self.stock_choice.setAccessibleName(tr('Готовый танк'))
        self.stock_choice.setMinimumWidth(175)
        self.stock_choice.setPlaceholderText(tr('Выбери папку игры'))
        row = QHBoxLayout()
        row.addWidget(self.stock_choice)
        self.stock_button = button(tr('Взять за основу'), self.start_stock, primary=True)
        row.addWidget(self.stock_button)
        row.addStretch()
        layout.addLayout(row)
        self.stock_source = button(tr('Найти папку игры…'), self.choose_gear_game)
        layout.addWidget(self.stock_source, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(10)
        layout.addWidget(QFrame(objectName='start-divider'))
        layout.addSpacing(10)
        layout.addWidget(QLabel(tr('Есть своя модель?'), objectName='muted'))
        layout.addWidget(button(tr('Импортировать GLB…'), self.import_glb), alignment=Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(content, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(2)
        return panel

    def start_stock(self):
        from uuid import uuid4
        key = self.stock_choice.currentData()
        if not key or not self.confirm_replace():
            return
        filename, _ = QFileDialog.getSaveFileName(self, tr('Сохранить проект танка'), 'tank.toml', 'Tank project (*.toml)')
        if not filename:
            return
        path = Path(filename).resolve()
        title = self.stock_choice.currentText()
        prefix = key.split('/')[-1].lower().replace('_', '-')
        self.start_job(lambda: stock_project(self.game_path, key, title, f'workshop.{prefix[:15]}-{uuid4().hex[:6]}'),
                       tr('Открываю модель из игры…'), lambda result: self.open_stock(path, result))

    def open_stock(self, path, project):
        project.save(path)
        self.load(path)
        self.tabs.setCurrentIndex(self.catalog_tab)

    def catalog_panel(self):
        panel = QWidget()
        page = QVBoxLayout(panel)
        page.setContentsMargins(12, 14, 12, 10)
        page.setSpacing(10)
        page.addWidget(QLabel(tr('Детали танка'), objectName='catalog-title'))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.slot_buttons = {}
        for index, (part, name) in enumerate(SLOT_LABELS.items()):
            control = button(tr(name), lambda checked=False, part=part: self.show_catalog(part))
            control.setCheckable(True)
            control.setObjectName('part-slot')
            self.slot_buttons[part] = control
            grid.addWidget(control, *divmod(index, 2))
        page.addLayout(grid)
        self.donor_search = QLineEdit(placeholderText=tr('Название танка или нация'))
        self.donor_search.setClearButtonEnabled(True)
        self.donor_search.setAccessibleName(tr('Поиск доноров'))
        self.donor_search.textChanged.connect(self.refresh_donors)
        page.addWidget(self.donor_search)
        fit = QFormLayout()
        fit.setContentsMargins(0, 0, 0, 0)
        self.catalog_coordinates = {}
        for axis, name in (('lift', tr('Высота')), ('shift', tr('Вдоль корпуса'))):
            spin = QDoubleSpinBox()
            spin.setDecimals(3)
            spin.setSingleStep(.02)
            spin.setSuffix(tr(' м'))
            spin.valueChanged.connect(lambda value, axis=axis: self.set_catalog_coordinate(axis, value))
            self.catalog_coordinates[axis] = spin
            fit.addRow(name, spin)
        page.addLayout(fit)
        self.mount_panel = QFrame(objectName='mount-panel')
        mount = QVBoxLayout(self.mount_panel)
        mount.setContentsMargins(9, 7, 9, 7)
        mount.setSpacing(5)
        self.mount_note = QLabel(objectName='mount-note')
        self.mount_note.setWordWrap(True)
        mount.addWidget(self.mount_note)
        actions = QHBoxLayout()
        self.mount_button = button(tr('Подогнать'), self.fit_selected_mount)
        actions.addWidget(self.mount_button)
        self.undo_mount_button = button(tr('Отменить замену'), self.undo_part)
        actions.addWidget(self.undo_mount_button)
        actions.addStretch(1)
        mount.addLayout(actions)
        page.addWidget(self.mount_panel)
        self.donor_list = QListWidget(objectName='donor-list')
        self.donor_list.setIconSize(QSize(88, 50))
        self.donor_list.setAccessibleName(tr('Доноры выбранной детали'))
        self.donor_list.currentItemChanged.connect(self.apply_donor)
        page.addWidget(self.donor_list, 1)
        self.donor_empty = QLabel(objectName='muted')
        self.donor_empty.setWordWrap(True)
        page.addWidget(self.donor_empty)
        self.donor_source = button(tr('Выбрать папку игры…'), self.choose_gear_game)
        page.addWidget(self.donor_source)
        return panel

    def show_catalog(self, part):
        self.catalog_slot = part
        self.refresh_donors()
        self.update_handles()

    def set_catalog_coordinate(self, axis, value):
        slot = self.catalog_slot
        current = (self.project.gear_fit[axis] if slot == 'gear'
                   else self.project.points[slot][1 if axis == 'lift' else 2])
        self.drag_gear(axis, value-current)

    def sync_catalog_coordinates(self):
        slot = self.catalog_slot
        for axis, spin in self.catalog_coordinates.items():
            gear = slot == 'gear'
            spin.blockSignals(True)
            spin.setRange(*((-5., 5.) if gear else (-50., 50.)))
            spin.setEnabled(bool(self.gear_mesh) if gear else MESH_NAMES[slot] in self.mounted_visual)
            spin.setValue(self.project.gear_fit[axis] if gear else self.project.points[slot][1 if axis == 'lift' else 2])
            spin.blockSignals(False)

    def donor_icon(self, key):
        cache_key = self.game_path, key
        if cache_key not in self.donor_icons:
            icon = QIcon()
            try:
                _, settings = donor_parameters(self.game_path, key)
                relative = settings['resourcesPath']['bigIconPath'].removeprefix('~res:/')
                texture = self.game_path/'Data'/(relative+'.packed.webp.dvpl')
                picture = QPixmap()
                if picture.loadFromData(dvpl.read(texture)):
                    icon = QIcon(picture)
            except (OSError, ValueError, KeyError):
                pass
            self.donor_icons[cache_key] = icon
        return self.donor_icons[cache_key]

    def update_slots(self):
        titles = {key: tr(title) for key, title, _ in CATALOG}
        part = self.catalog_slot
        active = self.project.part_donors | {'gear': self.project.running_gear}
        for key, control in self.slot_buttons.items():
            source = active.get(key)
            if not source and self.project.stock_model:
                source = self.project.base_vehicle
            control.setChecked(key == part)
            title = titles.get(source, source or tr('Из модели'))
            control.setText(f'{tr(SLOT_LABELS[key])}\n{title}')
        return active.get(part) or ''

    def refresh_donors(self):
        chosen = self.update_slots()
        query = self.donor_search.text().strip()
        self.donor_list.blockSignals(True)
        self.donor_list.clear()
        donors = catalog_matches(query) if self.donor_available else ()
        if not query:
            original = QListWidgetItem(tr('Из исходной модели'))
            original.setData(Qt.ItemDataRole.UserRole, '')
            original.setSizeHint(QSize(0, 37))
            self.donor_list.addItem(original)
            if not chosen:
                self.donor_list.setCurrentItem(original)
        for key, title, nation in donors:
            item = QListWidgetItem(self.donor_icon(key), f'{tr(title)}\n{tr(nation)}')
            item.setSizeHint(QSize(0, 61))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.donor_list.addItem(item)
            if key == chosen:
                self.donor_list.setCurrentItem(item)
        self.donor_list.blockSignals(False)
        self.donor_source.setVisible(not self.donor_available)
        self.donor_empty.setText(tr('Укажи папку игры, чтобы открыть каталог.') if not self.donor_available
                                 else tr('По этому запросу ничего не найдено.') if query and not donors else '')
        self.donor_empty.setVisible(bool(self.donor_empty.text()))

    def apply_donor(self, item, previous=None):
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        part = self.catalog_slot
        current = self.project.running_gear if part == 'gear' else self.project.part_donors.get(part, '')
        if key == current:
            return
        previous_project = self.collect()
        self.project = replace(previous_project, points=previous_project.points.copy(),
                               part_donors=previous_project.part_donors.copy())
        try:
            if part == 'gear':
                self.project.running_gear = key
                self.project.gear_fit = FIT_DEFAULTS.copy()
                self.load_gear_mesh()
                if key and set(TRACK_NAMES) <= self.visual_mesh.keys():
                    candidate = fit_to_model(self.visual_mesh, self.gear_mesh)
                    try:
                        replace(self.project, gear_fit=candidate).validate()
                    except ValueError:
                        pass
                    else:
                        self.project.gear_fit = candidate
                self.show_gear_values()
            else:
                select_donor_part(self.game_path, self.project, part, key, self.body_fallback)
                _, collision = self.body_meshes()
                targets = {'hull': ('turret',), 'turret': ('turret', 'gun'), 'gun': ('gun',)}
                for target in targets[part]:
                    seat_mount(self.game_path, self.project, target, collision)
                self.viewport.points = self.project.points
                self.select_part(PARTS.index(part))
            self.preview_model()
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.project = previous_project
            self.viewport.points = self.project.points
            self.load_gear_mesh()
            self.fail(str(error))
            self.refresh_donors()
            self.preview_model()
        else:
            self.undo_parts.append((part, previous_project))
            del self.undo_parts[:-20]
            self.update_slots()
            self.update_mount_status()

    def undo_part(self):
        if not self.undo_parts:
            return
        part, previous = self.undo_parts.pop()
        current = self.collect()
        self.project = replace(current, part_donors=previous.part_donors.copy(),
                               points=previous.points.copy(), original_points=previous.original_points.copy(),
                               running_gear=previous.running_gear, gear_fit=previous.gear_fit.copy())
        self.viewport.points = self.project.points
        self.select_part(PARTS.index(self.selected))
        self.catalog_slot = part
        self.populate_gear()

    def fit_selected_mount(self):
        part = self.catalog_slot
        if part not in MOUNTS:
            return
        old_points = self.project.points
        self.project.points = old_points.copy()
        if seat_mount(self.game_path, self.project, part, self.mounted_collision):
            self.capture_original_mounts(old_points)
            self.viewport.points = self.project.points
            self.select_part(PARTS.index(part))
            self.preview_model()

    def capture_original_mounts(self, points):
        if not self.project.stock_model and not self.project.original_points:
            self.project.original_points = points.copy()

    def update_mount_status(self):
        part = self.catalog_slot
        self.viewport.mount_marker = None
        self.mount_button.hide()
        self.undo_mount_button.setVisible(bool(self.undo_parts))
        self.mount_note.hide()
        self.mount_panel.setVisible(bool(self.undo_parts))
        if part not in MOUNTS or self.tabs.currentIndex() != self.catalog_tab or not self.mounted_visual:
            return
        self.mount_panel.show()
        if self.mount_cache is None or self.mount_cache[0] != part:
            fit = check_mount(self.game_path, self.project, part, self.mounted_collision)
            barrel = (barrel_in_hull(self.project, self.mounted_visual, self.mounted_collision)
                      if part == 'gun' else None)
            self.mount_cache = part, fit, barrel
        else:
            _, fit, barrel = self.mount_cache
        if barrel is not None:
            message, label, point = tr('Ствол заходит в корпус. Подними орудие.'), tr('Ствол в корпусе'), barrel
        elif fit.state in ('gap', 'overlap'):
            labels = {
                'gap': {'turret': 'Зазор у погона', 'gun': 'Зазор у маски'},
                'overlap': {'turret': 'Пересечение у погона', 'gun': 'Пересечение у маски'},
            }
            label, point = tr(labels[fit.state][part]), fit.position
            message = tr('{label} ≈ {distance:.0f} см', label=label, distance=abs(fit.offset)*100)
        else:
            label, point = '', None
            message = (tr('Посадка в допуске') if fit.state == 'seated'
                       else tr('Мало данных для проверки посадки'))
        self.mount_button.setVisible(fit.state in ('gap', 'overlap') and abs(fit.offset) <= MAX_CORRECTION[part])
        self.mount_note.setText(message)
        self.mount_note.setProperty('warning', point is not None)
        self.mount_note.style().unpolish(self.mount_note)
        self.mount_note.style().polish(self.mount_note)
        self.mount_note.show()
        if point is not None:
            self.viewport.mount_marker = point, label

    def gear_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(15, 14, 15, 14)
        heading = QLabel(tr('Ходовая из игры'))
        heading.setStyleSheet('font-size: 17px; font-weight: 650;')
        layout.addWidget(heading)
        self.gear_source = QLabel(objectName='muted')
        self.gear_source.setWordWrap(True)
        layout.addWidget(self.gear_source)
        layout.addWidget(button(tr('Выбрать папку игры…'), self.choose_gear_game))
        self.gear_choice = QComboBox()
        self.gear_choice.currentIndexChanged.connect(self.select_gear)
        layout.addWidget(self.gear_choice)
        self.gear_fit_button = button(tr('Подогнать под модель'), self.fit_gear, primary=True)
        self.gear_fit_button.setToolTip(tr('Подгоняет донора по паре гусениц из импортированной модели.'))
        layout.addWidget(self.gear_fit_button)
        form = QFormLayout()
        self.gear_fields = {}
        for key, title in dict(width=tr('Колея'), length=tr('Длина'), height=tr('Высота'),
                               lift=tr('По высоте'), shift=tr('Вдоль корпуса')).items():
            field = QDoubleSpinBox()
            offset = key in ('lift', 'shift')
            field.setRange(*((-5., 5.) if offset else (.25, 4.)))
            field.setSuffix(tr(' м') if offset else ' ×')
            field.setDecimals(3)
            field.setSingleStep(.02)
            field.valueChanged.connect(self.gear_changed)
            self.gear_fields[key] = field
            form.addRow(title, field)
        layout.addLayout(form)
        self.track_preview = QCheckBox(tr('Показать ход траков'))
        self.track_preview.setToolTip(tr('Показывает ход траков по контуру донора при движении вперёд.'))
        self.track_preview.toggled.connect(self.viewport.animate_tracks)
        layout.addWidget(self.track_preview)
        layout.addStretch()
        return panel

    def change_workspace(self, index):
        self.viewport.show_points = index not in (self.catalog_tab, self.gear_tab)
        self.viewport.click_parts = index == self.catalog_tab
        self.viewport.gear_controls = index in (self.catalog_tab, self.gear_tab)
        self.viewport.picking = False
        self.viewport.animate_tracks(index == self.gear_tab and self.track_preview.isChecked())
        self.update_handles()

    def set_view(self, yaw, pitch):
        self.viewport.yaw, self.viewport.pitch = yaw, pitch
        self.viewport.update()

    def choose_gear_game(self):
        name = QFileDialog.getExistingDirectory(self, tr('Папка игры {build}', build=BUILD_ID),
                                                str(self.game_path or ''))
        if not name:
            return
        try:
            game = Path(name).resolve(strict=True)
            donor_catalog(game)
        except (OSError, ValueError) as error:
            self.fail(str(error))
            return
        previous = self.game_path
        self.game_path = game
        if self.project.stock_model:
            try:
                self.refresh_model()
            except (OSError, ValueError, KeyError) as error:
                self.game_path = previous
                self.fail(str(error))
                return
        self.settings.setValue('gameDirectory', str(game))
        self.populate_gear()

    def populate_gear(self):
        self.gear_choice.blockSignals(True)
        self.gear_choice.clear()
        self.gear_choice.addItem(tr('Ходовая из модели'), '')
        try:
            catalog = donor_catalog(self.game_path) if self.game_path else ()
        except (OSError, ValueError):
            catalog = ()
        self.donor_available = bool(catalog)
        self.gear_source.setText(tr('Доноры из локальной установки') if catalog else tr('Укажи папку совместимой игры.'))
        for key, title, _ in CATALOG if catalog else ():
            self.gear_choice.addItem(tr(title), key)
        selected = self.stock_choice.currentData()
        self.stock_choice.clear()
        for key, title in catalog:
            self.stock_choice.addItem(tr(title), key)
        self.stock_choice.setCurrentIndex(max(0, self.stock_choice.findData(selected)))
        self.stock_button.setEnabled(bool(catalog))
        self.stock_source.setVisible(not catalog)
        key = self.project.running_gear
        index = self.gear_choice.findData(key)
        if index < 0:
            self.gear_choice.addItem(key + tr(' · источник недоступен'), key)
            index = self.gear_choice.count()-1
        self.gear_choice.setCurrentIndex(index)
        self.gear_choice.blockSignals(False)
        self.show_gear_values()
        try:
            self.load_gear_mesh()
        except (OSError, ValueError, KeyError) as error:
            self.gear_source.setText(tr(str(error)))
            self.loaded_gear = None
            self.gear_mesh = {}
            self.track_loops = []
        self.preview_model()
        self.refresh_donors()

    def select_gear(self, index):
        key = self.gear_choice.itemData(index)
        previous = self.project.running_gear, self.project.gear_fit
        self.project.running_gear, self.project.gear_fit = key, FIT_DEFAULTS.copy()
        try:
            self.load_gear_mesh()
        except (OSError, ValueError, KeyError) as error:
            self.project.running_gear, self.project.gear_fit = previous
            self.populate_gear()
            self.fail(str(error))
            return
        self.show_gear_values()
        self.preview_model()
        self.refresh_donors()

    def load_gear_mesh(self):
        key = self.project.running_gear
        selection = self.game_path, key
        if self.loaded_gear != selection:
            if self.game_path and key:
                mesh = donor_preview(*selection)
                self.track_loops = track_loops(*selection, mesh)
                self.gear_mesh = mesh
            else:
                self.gear_mesh = {}
                self.track_loops = []
            self.loaded_gear = selection

    def show_gear_values(self):
        for key, spin in self.gear_fields.items():
            spin.blockSignals(True)
            spin.setValue(self.project.gear_fit[key])
            spin.blockSignals(False)

    def gear_changed(self):
        self.project.gear_fit = {key: spin.value() for key, spin in self.gear_fields.items()}
        self.preview_model()

    def drag_gear(self, axis, distance):
        slot = self.catalog_slot if self.tabs.currentIndex() == self.catalog_tab else 'gear'
        if slot == 'gear':
            field = self.gear_fields[axis]
            field.setValue(field.value()+distance)
            return
        if abs(distance) < 1e-8:
            return
        coordinate = 1 if axis == 'lift' else 2
        start = self.project.points[slot][coordinate]
        distance = min(50., max(-50., start+distance))-start
        old_points = self.project.points.copy()
        shift = np.zeros(3)
        shift[coordinate] = distance
        if translate_mount(self.project, slot, shift):
            self.capture_original_mounts(old_points)
            self.select_part(PARTS.index(slot))
            self.preview_model()

    def fit_gear(self):
        try:
            settings = fit_to_model(self.visual_mesh, self.gear_mesh)
            replace(self.project, gear_fit=settings).validate()
        except (KeyError, ValueError) as error:
            self.fail(str(error))
            return
        self.project.gear_fit = settings
        self.show_gear_values()
        self.preview_model()
        self.status.setText(tr('Ходовая подогнана по габаритам исходной пары гусениц.'))

    def preview_model(self):
        has_donor = bool(self.gear_mesh)
        self.gear_fit_button.setEnabled(has_donor and set(TRACK_NAMES) <= self.visual_mesh.keys())
        self.track_preview.setEnabled(has_donor)
        if not has_donor:
            self.track_preview.setChecked(False)
        for spin in self.gear_fields.values():
            spin.setEnabled(has_donor)
        visible, collision = self.body_meshes()
        self.mounted_visual, self.mounted_collision = visible, collision
        self.mount_cache = None
        if has_donor:
            visible = fitted_parts(visible, self.gear_mesh, self.project.gear_fit)
        scale, offset = fit_vectors(self.project.gear_fit)
        self.viewport.set_track_loops([loop*scale+offset for loop in self.track_loops])
        self.gear_center = None
        if has_donor:
            points = np.concatenate([visible[key].reshape(-1, 3) for key in TRACK_NAMES])
            self.gear_center = (points.min(axis=0)+points.max(axis=0))/2
        self.viewport.set_models(visible, collision)
        self.update_handles()
        self.update_project_title()
        self.drive_button.setEnabled(bool(self.visual_mesh) and self.drive is None and self.stopping_drive is None)

    def body_meshes(self):
        if self.project.part_donors or self.project.stock_model or self.project.original_points:
            return body_preview(self.game_path, self.project, self.visual_mesh, self.collision_mesh)
        return self.visual_mesh, self.collision_mesh

    def update_handles(self):
        self.sync_catalog_coordinates()
        slot = self.catalog_slot if self.tabs.currentIndex() == self.catalog_tab else 'gear'
        if slot == 'gear':
            self.viewport.gear_origin = self.gear_center
        elif MESH_NAMES[slot] in self.mounted_visual:
            self.viewport.gear_origin = np.asarray(self.project.points[slot])
        else:
            self.viewport.gear_origin = None
        self.update_mount_status()
        self.viewport.update()

    def update_project_title(self):
        self.project_title.setText(self.project_title.fontMetrics().elidedText(
            self.project.name, Qt.TextElideMode.ElideRight, self.project_title.width()))
        self.project_title.setToolTip(self.project.name)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.update_project_title)

    def attachment_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(15, 14, 15, 14)
        self.parts = QListWidget()
        for key in PARTS:
            self.parts.addItem(tr(PART_LABELS[key]))
            self.parts.item(self.parts.count()-1).setForeground(QColor(PART_COLORS[key]))
        self.parts.setFixedHeight(111)
        self.parts.currentRowChanged.connect(self.select_part)
        layout.addWidget(self.parts)
        form = QFormLayout()
        for axis, label in zip('xyz', (tr('X · вправо, м'), tr('Y · вверх, м'), tr('Z · вперёд, м'))):
            spin = QDoubleSpinBox()
            spin.setRange(-50., 50.)
            spin.setDecimals(4)
            spin.setSingleStep(.05)
            spin.valueChanged.connect(self.point_changed)
            self.coordinates[axis] = spin
            form.addRow(label, spin)
        layout.addLayout(form)
        layout.addWidget(button(tr('Поставить на модели'), self.pick_point))
        self.show_collision = QCheckBox(tr('Показать коллизии'))
        self.show_collision.toggled.connect(self.toggle_collision)
        layout.addWidget(self.show_collision)
        layout.addStretch()
        return panel

    def parameter_panel(self):
        pane = QScrollArea()
        pane.setWidgetResizable(True)
        panel = QWidget(objectName='surface')
        form = QFormLayout(panel)
        form.setContentsMargins(15, 14, 15, 14)
        for key, label in FIELD_LABELS.items():
            if key == 'vehicle_class':
                widget = QComboBox()
                for title, value in zip((tr('Лёгкий'), tr('Средний'), tr('Тяжёлый'), tr('ПТ-САУ')), VEHICLE_CLASSES):
                    widget.addItem(title, value)
            elif key in NUMBER_FIELDS:
                widget = QDoubleSpinBox() if key in ('forward_kmh', 'reverse_kmh', 'reload_seconds') else QSpinBox()
                widget.setRange(*NUMBER_FIELDS[key])
                if isinstance(widget, QDoubleSpinBox):
                    widget.setDecimals(2)
                elif key.startswith('armor_'):
                    widget.setSpecialValueText(tr('От базового танка'))
            else:
                widget = QLineEdit()
            self.fields[key] = widget
            form.addRow(tr(label), widget)
        pane.setWidget(panel)
        return pane

    def assets_panel(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(15, 14, 15, 14)
        layout.addWidget(button(tr('Импорт из Blender · GLB'), self.import_glb))
        for scene, title in (('visual_scene', 'Импорт визуальной модели…'),
                             ('collision_scene', 'Импорт коллизий…')):
            layout.addWidget(button(tr(title), lambda key=scene: self.import_model(key)))
            label = QLabel(tr('Не импортировано'), objectName='muted')
            label.setWordWrap(True)
            self.asset_labels[scene] = label
            layout.addWidget(label)
        layout.addWidget(QLabel(tr('Текстуры материалов')))
        self.textures = QListWidget()
        self.textures.setHorizontalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.textures.itemDoubleClicked.connect(self.import_texture)
        self.textures.currentItemChanged.connect(self.show_texture)
        layout.addWidget(self.textures, 1)
        self.texture_preview = QLabel(objectName='muted')
        self.texture_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.texture_preview.setMinimumHeight(92)
        layout.addWidget(self.texture_preview)
        layout.addWidget(button(tr('Назначить текстуру…'), self.import_texture))
        return panel

    def select_part(self, index):
        if index < 0:
            return
        self.selected = PARTS[index]
        self.viewport.selected = self.selected
        for spin, coordinate in zip(self.coordinates.values(), self.project.points[self.selected]):
            spin.blockSignals(True)
            spin.setValue(coordinate)
            spin.blockSignals(False)
        self.viewport.update()

    def point_changed(self):
        self.project.points[self.selected] = tuple(spin.value() for spin in self.coordinates.values())
        self.viewport.points = self.project.points
        self.preview_model()

    def place_point(self, point):
        self.project.points[self.selected] = tuple(map(float, point))
        self.select_part(PARTS.index(self.selected))
        self.preview_model()

    def pick_point(self):
        if not len(self.viewport.mesh):
            self.status.setText(tr('Сначала импортируй визуальную модель.'))
            return
        self.viewport.picking = True
        self.viewport.update()

    def toggle_collision(self, enabled):
        self.viewport.overlay = enabled
        self.viewport.update()

    def populate(self):
        for key, widget in self.fields.items():
            value = getattr(self.project, key)
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(widget.findData(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(value)
            else:
                widget.setValue(value)
        self.parts.setCurrentRow(PARTS.index(self.selected))
        self.select_part(PARTS.index(self.selected))
        self.refresh_model()
        self.populate_gear()

    def refresh_model(self):
        from .project import local_asset
        models = {}
        self.refs = set()
        for key in self.asset_labels:
            value = getattr(self.project, key)
            self.asset_labels[key].setText(value or tr('Не импортировано'))
            self.asset_labels[key].setToolTip(value)
            if value and self.path is not None:
                scene = local_asset(self.path.parent, value)
                geom = local_asset(self.path.parent, getattr(self.project, key.replace('scene', 'geometry')))
                models[key], _ = model_preview(scene, geom)
                if key == 'visual_scene':
                    self.refs = texture_refs(dava_scene.scene(scene))
        self.refresh_textures()
        if self.project.stock_model:
            if self.game_path is None:
                raise ValueError(tr('Выбери папку игры, чтобы открыть эту модель.'))
            source, hit = stock_assets(self.game_path, self.project.base_vehicle)
            turret, gun = stock_parts(self.game_path, self.project.base_vehicle)
            for target, scene in (('visual_scene', source), ('collision_scene', hit)):
                meshes, _ = preview_pair(scene)
                selected = {'hull': 'hull', turret: 'turret_01', gun: 'gun_01'}
                models[target] = {selected.get(name, name): mesh for name, mesh in meshes.items()
                                  if name in selected or name.startswith('chassis_') and 'crash' not in name}
            from .parts import donor_parts
            self.body_fallback = donor_parts(self.game_path, self.project.base_vehicle)[-1]
            self.asset_labels['visual_scene'].setText(tr('Модель из игры'))
            self.asset_labels['collision_scene'].setText(tr('Коллизии из игры'))
        self.viewport.points = self.project.points
        self.visual_mesh = models.get('visual_scene', {})
        self.collision_mesh = models.get('collision_scene', {})
        if not self.project.stock_model and not self.project.part_donors:
            self.body_fallback = self.project.points.copy()
        self.preview_model()
        self.pages.setCurrentIndex(int(bool(self.visual_mesh or self.project.visual_scene)))

    def refresh_textures(self):
        self.textures.clear()
        for name in sorted(self.refs):
            self.textures.addItem(f'{name}\n{self.project.textures.get(name, tr("Выбери файл"))}')
            self.textures.item(self.textures.count()-1).setData(Qt.ItemDataRole.UserRole, name)

    def show_texture(self, item, previous=None):
        self.texture_preview.clear()
        if item is None or self.path is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        source = self.project.textures.get(name)
        if source is None:
            self.texture_preview.setText(tr('Текстура не назначена'))
            return
        if source.endswith(('.dds', '.dds.dvpl')):
            self.texture_preview.setText(tr('Готовая текстура DDS'))
            return
        from PySide6.QtGui import QImageReader
        from .project import local_asset
        try:
            path = local_asset(self.path.parent, source)
        except (OSError, ValueError):
            self.texture_preview.setText(tr('Файл текстуры отсутствует'))
            return
        reader = QImageReader(str(path))
        size = reader.size()
        size.scale(250, 90, Qt.AspectRatioMode.KeepAspectRatio)
        reader.setScaledSize(size)
        image = reader.read()
        if image.isNull():
            self.texture_preview.setText(tr('Не удалось открыть текстуру'))
        else:
            self.texture_preview.setPixmap(QPixmap.fromImage(image))

    def collect(self):
        fields = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QLineEdit):
                fields[key] = widget.text().strip()
            elif isinstance(widget, QComboBox):
                fields[key] = widget.currentData()
            else:
                fields[key] = widget.value()
        return replace(self.project, **fields)

    def confirm_replace(self):
        if self.thread is not None:
            return False
        try:
            unchanged = self.collect().to_bytes() == self.saved
        except ValueError:
            unchanged = False
        return unchanged or QMessageBox.question(self, tr('Несохранённые изменения'),
            tr('Закрыть проект без сохранения?'), QMessageBox.StandardButton.Discard |
            QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel
        ) == QMessageBox.StandardButton.Discard

    def new(self):
        if not self.confirm_replace():
            return
        self.project, self.path = TankProject(), None
        self.undo_parts.clear()
        self.saved = self.project.to_bytes()
        self.populate()
        self.status.clear()

    def open(self):
        if not self.confirm_replace():
            return
        name, _ = QFileDialog.getOpenFileName(self, tr('Открыть проект'), '', 'Tank project (*.toml)')
        if name:
            self.load(Path(name))

    def load(self, path):
        previous = self.project, self.path, self.saved, self.body_fallback, self.undo_parts
        try:
            project = TankProject.open(path)
            self.project, self.path, self.saved = project, path.resolve(), project.to_bytes()
            self.body_fallback = project.points.copy()
            self.undo_parts = []
            self.populate()
            self.status.setText(str(self.path))
            self.tabs.setCurrentIndex(self.catalog_tab)
        except Exception as error:
            self.project, self.path, self.saved, self.body_fallback, self.undo_parts = previous
            self.populate()
            self.fail(str(error))

    def save(self):
        try:
            project = self.collect()
            project.validate()
            path = self.path
            if path is None:
                name, _ = QFileDialog.getSaveFileName(self, tr('Сохранить проект'), 'tank.toml', 'Tank project (*.toml)')
                if not name:
                    return False
                path = Path(name).resolve()
            project.save(path)
            self.path, self.project, self.saved = path, project, project.to_bytes()
            self.status.setText(tr('Сохранено · {path}', path=path))
            return True
        except (OSError, ValueError) as error:
            self.fail(str(error))
            return False

    def copy_asset(self, source, slot):
        suffixes = ''.join(source.suffixes[-2:]) if source.suffix.lower() == '.dvpl' else source.suffix
        root = self.path.parent/'assets'
        root.mkdir(exist_ok=True)
        if root.is_symlink() or root.resolve() != root:
            raise ValueError(tr('Папка assets должна находиться рядом с проектом.'))
        from uuid import uuid4
        destination = root/f'{slot}-{uuid4().hex[:12]}{suffixes.lower()}'
        from .models import MAX_ASSET
        if source.stat().st_size > MAX_ASSET:
            raise ValueError(tr('Файл превышает 64 МиБ.'))
        shutil.copyfile(source, destination)
        return destination.relative_to(self.path.parent).as_posix()

    def import_model(self, key):
        if self.project.stock_model:
            self.fail(tr('Чтобы заменить модель из игры, импортируй полный GLB.'))
            return
        if self.path is None and not self.save():
            return
        filename, _ = QFileDialog.getOpenFileName(self, tr('Импорт модели SC2'), '', 'DAVA scene (*.sc2 *.sc2.dvpl)')
        if not filename:
            return
        scene = Path(filename)
        if scene.suffix.lower() == '.dvpl':
            geometry = scene.with_suffix('').with_suffix('.scg').with_suffix('.scg.dvpl')
        else:
            geometry = scene.with_suffix('.scg')
        geom_key = key.replace('scene', 'geometry')
        try:
            _, marks = model_preview(scene, geometry)
            refs = texture_refs(dava_scene.scene(scene)) if key == 'visual_scene' else set()
            setattr(self.project, key, self.copy_asset(scene, key))
            setattr(self.project, geom_key, self.copy_asset(geometry, geom_key))
            if key == 'visual_scene':
                self.project.textures = {ref: path for ref, path in self.project.textures.items() if ref in refs}
                for ref in sorted(refs - self.project.textures.keys()):
                    for suffix in ('.dx11.dds.dvpl', '.png', '.jpg', '.dds'):
                        texture = scene.parent/Path(ref).with_suffix(suffix)
                        if texture.is_file():
                            self.project.textures[ref] = self.copy_asset(texture, 'texture')
                            break
                if POINT_NODE in marks:
                    self.project.points['muzzle'] = tuple(map(float, marks[POINT_NODE]))
                    self.select_part(PARTS.index(self.selected))
            self.refresh_model()
            self.status.setText(tr('Импортировано · {name}', name=scene.name))
        except Exception as error:
            self.fail(str(error))

    def eventFilter(self, watched, event):
        from PySide6.QtCore import QEvent
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.Drop) and self.thread is None:
            files = event.mimeData().urls()
            if len(files) == 1 and files[0].isLocalFile() and Path(files[0].toLocalFile()).suffix.lower() == '.glb':
                event.acceptProposedAction()
                if event.type() == QEvent.Type.Drop:
                    self.import_glb(files[0].toLocalFile())
                return True
        return super().eventFilter(watched, event)

    def import_glb(self, filename=None):
        if not filename:
            filename, _ = QFileDialog.getOpenFileName(self, tr('Импорт GLB'), '', 'glTF binary (*.glb)')
        if not filename:
            return
        from uuid import uuid4
        from .glb import import_glb
        if self.path is None:
            path, _ = QFileDialog.getSaveFileName(self, tr('Сохранить проект танка'), 'tank.toml', 'Tank project (*.toml)')
            if not path:
                return
            self.path = Path(path).resolve()
            self.project = replace(self.project, id=f'workshop.tank-{uuid4().hex[:10]}',
                                   name=Path(filename).stem.strip()[:48] or tr('Новый танк'))
            self.populate()
        directory = self.path.parent/'assets'
        if directory.is_symlink():
            self.fail(tr('Папка assets должна находиться рядом с проектом.'))
            return
        destination = directory/f'glb-{uuid4().hex[:12]}'
        self.start_job(lambda: import_glb(Path(filename), destination), tr('Импортируем GLB…'),
                       lambda result: self.glb_imported(destination, result))

    def glb_imported(self, destination, result):
        points, textures = result
        prefix = destination.relative_to(self.path.parent)
        self.project.points.update(points)
        self.project.stock_model = False
        self.project.part_donors = {}
        self.project.original_points = {}
        self.undo_parts.clear()
        self.project.textures = {ref: (prefix/path).as_posix() for ref, path in textures.items()}
        for field in ASSET_FIELDS:
            kind, role = field.split('_')
            extension = 'sc2' if role == 'scene' else 'scg'
            setattr(self.project, field, (prefix/f'{kind}.{extension}.dvpl').as_posix())
        self.refresh_model()
        self.select_part(PARTS.index(self.selected))
        self.tabs.setCurrentIndex(0)
        self.save()
        self.status.setText(tr('GLB импортирован. Проверь точки крепления.'))

    def import_texture(self, *unused):
        item = self.textures.currentItem()
        if item is None:
            self.status.setText(tr('Выбери материал из списка.'))
            return
        name, _ = QFileDialog.getOpenFileName(self, tr('Выбрать текстуру'), '', 'Textures (*.png *.jpg *.jpeg *.dds *.dds.dvpl)')
        if not name:
            return
        try:
            ref = item.data(Qt.ItemDataRole.UserRole)
            self.project.textures[ref] = self.copy_asset(Path(name), 'texture')
            self.refresh_textures()
            for index in range(self.textures.count()):
                if self.textures.item(index).data(Qt.ItemDataRole.UserRole) == ref:
                    self.textures.setCurrentRow(index)
                    break
        except (OSError, ValueError) as error:
            self.fail(str(error))

    def export(self):
        if self.thread is not None or not self.save():
            return
        name, _ = QFileDialog.getSaveFileName(self, tr('Экспорт пакета'), self.project.id+'.tank.zip', 'Tank pack (*.zip)')
        if not name:
            return
        project, root = self.project, self.path.parent
        self.start_job(lambda: export(project, root, Path(name)), tr('Собираем пакет…'),
                       lambda result: self.status.setText(tr('Пакет готов · SHA-256 {digest}', digest=result)))

    def install(self, target):
        if self.thread is not None:
            return
        name, _ = QFileDialog.getOpenFileName(self, tr('Выбрать пакет танка'), '', 'Tank pack (*.zip)')
        if not name:
            return
        title = tr('Папка подготовленного клиента') if target == 'client' else tr('Папка состояния сервера')
        directory = QFileDialog.getExistingDirectory(self, title)
        if not directory:
            return
        from .install import install
        self.start_job(lambda: install(Path(name), **{target: Path(directory)}), tr('Устанавливаем пакет…'),
                       lambda result: self.status.setText(tr('Установлено · {tank} · {path}', **result)))

    def test_drive(self):
        if (self.thread is not None or self.drive is not None or self.stopping_drive is not None
                or not self.save()):
            return
        from ..management import Settings
        from .install import install_test_vehicle
        try:
            settings = Settings.load(self.config)
            if not (Path(settings.state)/'manifest.json').is_file() or not (Path(settings.client)/'manifest.json').is_file():
                raise ValueError(tr('Открой лаунчер Solbreaker и подготовь локальный сервер и клиент.'))
        except (OSError, ValueError) as error:
            self.fail(str(error))
            return
        project, root = self.project, self.path.parent
        self.start_job(lambda: install_test_vehicle(settings, project, root), tr('Собираю и устанавливаю танк…'),
                       lambda result: self.launch_drive(settings, result))

    def launch_drive(self, settings, result):
        from ..management import Manager
        project, ident = result
        self.project = project
        self.fields['vehicle_id'].setValue(project.vehicle_id)
        if not self.save():
            return
        self.drive = Manager(replace(settings, play_mode='local', sandbox=True), workshop_vehicle=ident)
        try:
            self.drive.run('play')
        except Exception:
            self.drive = None
            raise
        self.drive_button.setEnabled(False)
        self.drive_status = ''
        self.status.setText(tr('Запускаю локальный сервер и клиент для Полигона…'))
        self.drive_timer.start()

    def update_drive(self):
        if self.stopping_drive is not None:
            if not self.stopping_drive.server.running:
                self.stopping_drive = None
                self.drive_timer.stop()
                self.drive_button.setEnabled(bool(self.visual_mesh))
            return
        drive = self.drive
        lines = drive.drain()
        for line in lines:
            if not line.startswith('{'):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') == 'workshop_drive_started':
                self.drive_status = tr('Полигон: загружаю выбранный танк.')
            elif event.get('event') == 'workshop_drive_failed':
                self.drive_status = tr('Не удалось открыть Полигон. {reason}', reason=tr(event.get('reason', '')))
        if drive.client is None and not drive._play_when_ready:
            drive.stop_server()
            self.drive = None
            if drive.server is not None and drive.server.running:
                self.stopping_drive = drive
            else:
                self.drive_timer.stop()
                self.drive_button.setEnabled(bool(self.visual_mesh))
            self.status.setText(tr(drive.notice) if 'ошибк' in drive.notice or 'Не удалось' in drive.notice
                                else tr('Тестовый заезд завершён.'))
        elif drive.client is not None:
            self.status.setText(self.drive_status or tr('Клиент запущен. Если появится вход, войди в Solbreaker.'))

    def start_job(self, task, message, complete):
        self.thread = QThread(self)
        self.job = Job(task)
        self.job_complete = complete
        self.job.moveToThread(self.thread)
        self.thread.started.connect(self.job.run)
        self.job.finished.connect(self.job_done)
        self.job.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.job.deleteLater)
        self.thread.finished.connect(self.thread_done)
        self.centralWidget().setEnabled(False)
        self.status.setText(message)
        self.thread.start()

    @Slot(object, str)
    def job_done(self, result, error):
        if error:
            self.fail(error)
        else:
            try:
                self.job_complete(result)
            except Exception as error:
                self.fail(str(error))

    @Slot()
    def thread_done(self):
        self.thread.deleteLater()
        self.thread = None
        self.centralWidget().setEnabled(True)

    def fail(self, error):
        error = tr(error)
        self.status.setText(error)
        QMessageBox.warning(self, 'Solbreaker Workshop', error)

    def closeEvent(self, event):
        if not self.confirm_replace():
            event.ignore()
            return
        if self.drive is not None:
            self.drive.stop_server()


def run(project=None, config=Path('runtime/manager.json'), *, language=None):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setFont(QFont('Inter', 10))
    editor = Editor(project, config, language=language)
    editor.show()
    return app.exec()
