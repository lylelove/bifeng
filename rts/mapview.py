"""地图视图控件：地形渲染、相机平移缩放、点选/框选、画笔式路径下达。

交互约定
    左键点击兵团        选中（Shift/Ctrl 加选）
    左键空地拖拽        框选
    右键点可见敌军      指定攻击（有选中时）
    右键点击/拖拽       按 order_mode 下达移动／攻移／巡逻
    中键拖拽 / 方向键   平移视角
    滚轮                缩放
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient,
                           QMouseEvent, QPainter, QPainterPath, QPen, QPixmap,
                           QPolygonF, QRadialGradient, QWheelEvent)
from PySide6.QtWidgets import QWidget

from .terrain import TERRAIN_INFO, TILE, GameMap, Terrain
from .units import (ENTRENCH_TIME, FACTION_COLOR, FACTION_NAME, MORALE_ROUT,
                    UNIT_TYPES, Faction, Unit)
from .world import World

MIN_ZOOM, MAX_ZOOM = 0.35, 3.0
ATTACK_PING_TTL = 0.38          # 指定攻击下令确认红圈时长（秒）


def _qcolor(rgb: tuple[int, int, int], alpha: int = 255) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], alpha)


def _hash01(x: int, y: int, seed: int) -> float:
    """确定性格点哈希 → [0,1)，用于颜色抖动与细节撒点（与地图种子绑定）。"""
    h = (x * 73856093) ^ (y * 19349663) ^ (seed * 83492791)
    h = ((h ^ (h >> 13)) * 0x5BD1E995) & 0xFFFFFFFF
    return ((h >> 8) & 0xFFFF) / 65536.0


# 阵营用色板：兵牌主体渐变（亮→基色）、深色描边、战场轨迹线
FACTION_STYLE = {
    Faction.PLAYER: {
        "hi":   QColor(126, 190, 255),    # 渐变亮部（受光面）
        "base": QColor(*FACTION_COLOR[Faction.PLAYER]),
        "lo":   QColor(38, 92, 168),      # 渐变暗部
        "edge": QColor(16, 42, 84),       # 兵牌描边
        "trace": QColor(150, 205, 255),   # 交战轨迹
    },
    Faction.ENEMY: {
        "hi":   QColor(255, 138, 122),
        "base": QColor(*FACTION_COLOR[Faction.ENEMY]),
        "lo":   QColor(150, 38, 32),
        "edge": QColor(74, 14, 12),
        "trace": QColor(255, 150, 120),
    },
}


class MapView(QWidget):
    selection_changed = Signal()
    hover_changed = Signal(str)
    edited = Signal()                 # 编辑模式下发生一次改动（供编辑器刷新计数）

    def __init__(self, world: World, parent: QWidget | None = None):
        super().__init__(parent)
        self.world = world
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(640, 480)

        self.zoom = 1.0
        self.cam_x = 0.0
        self.cam_y = 0.0

        self._terrain_cache: QPixmap | None = None
        self._minimap_cache: QPixmap | None = None      # 预缩放的小地图地形
        self._territory_cache: QPixmap | None = None    # 势力色块位图缓存
        self._territory_version = -1                    # 对应 world.field_version
        self._frontline: list[tuple[float, float, float, float]] = []  # 前线线段（世界坐标）
        self._vis_cache: dict[int, bool] | None = None  # 本帧可见性缓存 uid→bool
        self._fog_img: QImage | None = None             # 迷雾离屏画布（复用）
        self._range_cache: dict[tuple[str, int], QPixmap] = {}  # 射程圈贴图 (兵种,量化缩放)
        self._sparkles: list[tuple[float, float, float, float]] = []  # 波光点 (x,y,len,相位)
        self._vignette: QPixmap | None = None           # 屏幕暗角（随视口尺寸缓存）
        self._dragging_cam = False
        self._last_mouse = QPoint()
        self._select_origin: QPointF | None = None
        self._select_current: QPointF | None = None
        self._brush: list[tuple[float, float]] = []
        self._drawing = False
        self.order_mode: str = "move"     # move / attack_move / patrol（右键下达的指令类型）
        self.formation_lock: bool = False  # 编队锁定开关：右键下令时是否同步限速保队形
        self._show_grid = False
        self._show_territory = True
        self._show_fog = True             # 战争迷雾：视野外的敌军不可见
        self.hovered: Unit | None = None
        # 指定攻击下令确认：(世界x, 世界y, 剩余秒)
        self._attack_pings: list[tuple[float, float, float]] = []
        self._attack_cursor = False       # 当前是否显示攻击十字光标

        # 编辑模式：由 EditorPage 打开并注入 MapEditor；战斗态下恒为 False
        self.edit_mode = False
        self.editor = None
        self._painting = False
        self._edit_hover: tuple[float, float] | None = None
        # 编辑态右键：拖动=平移视角，轻点（未拖动）=擦除光标下兵团
        self._edit_pan = False
        self._edit_pan_moved = False
        self._edit_pan_from: tuple[float, float] = (0.0, 0.0)

    # ------------------------------------------------------------ 坐标变换
    def to_world(self, p: QPointF | QPoint) -> tuple[float, float]:
        return self.cam_x + p.x() / self.zoom, self.cam_y + p.y() / self.zoom

    def to_screen(self, wx: float, wy: float) -> QPointF:
        return QPointF((wx - self.cam_x) * self.zoom, (wy - self.cam_y) * self.zoom)

    def clamp_camera(self) -> None:
        gm = self.world.map
        vw, vh = self.width() / self.zoom, self.height() / self.zoom
        self.cam_x = max(min(self.cam_x, gm.pixel_width - vw), min(0.0, gm.pixel_width - vw))
        self.cam_y = max(min(self.cam_y, gm.pixel_height - vh), min(0.0, gm.pixel_height - vh))

    def center_on(self, wx: float, wy: float) -> None:
        self.cam_x = wx - self.width() / (2 * self.zoom)
        self.cam_y = wy - self.height() / (2 * self.zoom)
        self.clamp_camera()

    def center_on_selection(self) -> None:
        sel = self.world.selected or self.world.units_of(Faction.PLAYER)
        if sel:
            self.center_on(sum(u.x for u in sel) / len(sel), sum(u.y for u in sel) / len(sel))
            self.update()

    # ------------------------------------------------------------ 地形缓存
    def invalidate_terrain(self) -> None:
        self._terrain_cache = None
        self._minimap_cache = None
        self._territory_cache = None
        self._territory_version = -1
        self.update()

    def _terrain_pixmap(self) -> QPixmap:
        if self._terrain_cache is not None:
            return self._terrain_cache
        gm = self.world.map
        seed = gm.seed
        img = QImage(gm.width, gm.height, QImage.Format_RGB32)
        for y in range(gm.height):
            row = gm.tiles[y]
            erow = gm.elevation[y]
            for x in range(gm.width):
                t = Terrain(row[x])
                r, g, b = TERRAIN_INFO[t].color
                s = gm.shade_at(x, y)
                if t is Terrain.LAKE:          # 湖水随深度加深，湖心近墨
                    depth = min(1.0, max(0.0, (0.30 - erow[x]) / 0.30))
                    s *= 1.0 - 0.38 * depth
                # 每格 ±4% 明度抖动，打破大片纯色的塑料感
                s *= 0.96 + 0.08 * _hash01(x, y, seed)
                img.setPixel(x, y, (min(255, int(r * s)) << 16)
                             | (min(255, int(g * s)) << 8) | min(255, int(b * s)))
        # 放大到世界像素尺寸，平滑插值让地形边界柔和；细节再烘焙到放大图上
        big = img.scaled(gm.pixel_width, gm.pixel_height,
                         Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self._bake_details(big)
        self._terrain_cache = QPixmap.fromImage(big)
        return self._terrain_cache

    def _bake_details(self, img: QImage) -> None:
        """把树冠、山棱、等高线、岸线一次性画进地形图，并登记水面波光点。

        全部细节只在建缓存时绘制一次，帧内仍是整图一次 drawPixmap。
        """
        gm = self.world.map
        seed = gm.seed
        water = (Terrain.LAKE, Terrain.RIVER)

        def is_land(tx: int, ty: int) -> bool:
            return (0 <= tx < gm.width and 0 <= ty < gm.height
                    and Terrain(gm.tiles[ty][tx]) not in water)

        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        sparkles: list[tuple[float, float, float, float]] = []
        shore_pen = QPen(QColor(198, 222, 230, 100), 1.6)
        contour_pen = QPen(QColor(112, 100, 66, 95), 1.2)
        for ty in range(gm.height):
            row = gm.tiles[ty]
            for tx in range(gm.width):
                t = Terrain(row[tx])
                if t is Terrain.PLAIN:
                    continue
                h0 = _hash01(tx, ty, seed)
                px, py = tx * TILE, ty * TILE
                if t is Terrain.FOREST:
                    # 树冠：2~3 个深绿圆点 + 右下投影，位置/大小由哈希确定
                    for i in range(2 + (h0 > 0.55)):
                        ox = 2.5 + _hash01(tx * 3 + i, ty, seed ^ 0x51) * (TILE - 6)
                        oy = 2.5 + _hash01(tx, ty * 3 + i, seed ^ 0x37) * (TILE - 6)
                        rr = 1.9 + 1.5 * _hash01(tx + i, ty + i, seed ^ 0x99)
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(16, 40, 22, 105))
                        p.drawEllipse(QPointF(px + ox + 1.1, py + oy + 1.3), rr, rr)
                        g0 = 66 + int(26 * _hash01(tx * 7 + i, ty, seed))
                        p.setBrush(QColor(30, g0, 36, 235))
                        p.drawEllipse(QPointF(px + ox, py + oy), rr, rr)
                elif t is Terrain.MOUNTAIN:
                    # 山棱：左受光右背光的小尖峰；最高处点雪
                    e = gm.elevation[ty][tx]
                    cx = px + TILE * (0.30 + 0.40 * h0)
                    cy = py + TILE * (0.30 + 0.40 * _hash01(ty, tx, seed ^ 0x11))
                    w0 = TILE * (0.28 + 0.24 * _hash01(tx, ty, seed ^ 0x77))
                    hh = w0 * 1.2
                    tip = QPointF(cx, cy - hh * 0.6)
                    foot = QPointF(cx, cy + hh * 0.25)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(154, 146, 136, 150))
                    p.drawPolygon(QPolygonF([tip, QPointF(cx - w0, cy + hh * 0.4), foot]))
                    p.setBrush(QColor(74, 66, 60, 150))
                    p.drawPolygon(QPolygonF([tip, QPointF(cx + w0, cy + hh * 0.4), foot]))
                    if e > 0.85:
                        p.setBrush(QColor(242, 246, 250, 205))
                        p.drawPolygon(QPolygonF([
                            tip,
                            QPointF(cx - w0 * 0.32, cy - hh * 0.22),
                            QPointF(cx + w0 * 0.32, cy - hh * 0.22)]))
                elif t is Terrain.HILL:
                    # 稀疏的等高线弧，暗一号的土色
                    if h0 < 0.45:
                        p.setPen(contour_pen)
                        p.setBrush(Qt.NoBrush)
                        rr = TILE * (0.55 + 0.5 * _hash01(tx, ty, seed ^ 0x5))
                        p.drawArc(QRectF(px + TILE * 0.10, py + TILE * 0.22, rr, rr * 0.6),
                                  30 * 16, 120 * 16)
                else:                       # 河流 / 湖泊
                    # 岸线：贴着陆地的边描一道浅色水线
                    p.setPen(shore_pen)
                    p.setBrush(Qt.NoBrush)
                    if is_land(tx, ty - 1):
                        p.drawLine(QPointF(px, py + 0.8), QPointF(px + TILE, py + 0.8))
                    if is_land(tx, ty + 1):
                        p.drawLine(QPointF(px, py + TILE - 0.8),
                                   QPointF(px + TILE, py + TILE - 0.8))
                    if is_land(tx - 1, ty):
                        p.drawLine(QPointF(px + 0.8, py), QPointF(px + 0.8, py + TILE))
                    if is_land(tx + 1, ty):
                        p.drawLine(QPointF(px + TILE - 0.8, py),
                                   QPointF(px + TILE - 0.8, py + TILE))
                    # 波光候选：低概率登记一条短亮线（绘制时按相位闪烁）
                    if h0 < 0.26:
                        sparkles.append((
                            px + TILE * (0.2 + 0.6 * _hash01(tx, ty, seed ^ 0x2A)),
                            py + TILE * (0.2 + 0.6 * _hash01(ty, tx, seed ^ 0x4C)),
                            1.6 + 2.2 * _hash01(tx, ty, seed ^ 0x6E),
                            _hash01(tx, ty, seed ^ 0x8F) * math.tau))
        p.end()
        self._sparkles = sparkles

    def toggle_grid(self) -> None:
        self._show_grid = not self._show_grid
        self.update()

    def toggle_territory(self) -> None:
        self._show_territory = not self._show_territory
        self.update()

    def toggle_fog(self) -> None:
        self._show_fog = not self._show_fog
        self.update()

    # ------------------------------------------------------------ 战争迷雾
    def fog_active(self) -> bool:
        """迷雾是否生效：编辑模式下永远全图可见。"""
        return self._show_fog and not self.edit_mode

    def unit_visible(self, u: Unit) -> bool:
        """迷雾开启时，敌军只有被我方发现才画出来（每帧结果缓存）。"""
        if not self.fog_active() or u.faction is Faction.PLAYER:
            return True
        cache = self._vis_cache
        if cache is None:
            cache = self._vis_cache = {}
        v = cache.get(u.uid)
        if v is None:
            v = cache[u.uid] = self.world.visible_to(Faction.PLAYER, u)
        return v

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:
        self._vis_cache = None            # 可见性按帧缓存，进帧先失效
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(18, 20, 24))

        gm = self.world.map
        vw, vh = self.width() / self.zoom, self.height() / self.zoom
        src = QRectF(self.cam_x, self.cam_y, vw, vh)
        dst = QRectF(0, 0, self.width(), self.height())
        p.drawPixmap(dst, self._terrain_pixmap(), src)

        self._draw_sparkles(p, src)
        if self._show_territory:
            self._draw_territory(p)
        if self._show_grid:
            self._draw_grid(p, src)
        if self.fog_active():
            self._draw_fog(p)
        self._draw_paths(p)
        self._draw_attack_ranges(p)
        self._draw_combat(p)
        self._draw_memory_ghosts(p)
        for u in self.world.units:
            if self.unit_visible(u):
                self._draw_unit(p, u)
        self._draw_attack_pings(p)
        self._draw_brush(p)
        self._draw_selection_rect(p)
        if self.edit_mode:
            self._draw_edit_cursor(p)
        self._draw_vignette(p)
        self._draw_minimap(p)
        p.end()

    # ------------------------------------------------------------ 氛围图层
    def _draw_sparkles(self, p: QPainter, src: QRectF) -> None:
        """水面波光：预登记的稀疏亮线按各自相位闪烁，只画进入视口的。"""
        if not self._sparkles:
            return
        t = self.world.elapsed
        z = self.zoom
        ox, oy = self.cam_x, self.cam_y
        l, r0 = src.left() - 8, src.right() + 8
        tp, bt = src.top() - 8, src.bottom() + 8
        pen = QPen(QColor(225, 242, 250, 0), max(1.0, 1.1 * z), Qt.SolidLine, Qt.RoundCap)
        pen.setCosmetic(False)
        for wx, wy, ln, phase in self._sparkles:
            if not (l <= wx <= r0 and tp <= wy <= bt):
                continue
            a = 0.5 + 0.5 * math.sin(t * 1.7 + phase)
            if a < 0.35:                       # 大半时间熄灭，闪烁更像粼光
                continue
            c = QColor(225, 242, 250, int(150 * (a - 0.35) / 0.65))
            pen.setColor(c)
            p.setPen(pen)
            sx, sy = (wx - ox) * z, (wy - oy) * z
            p.drawLine(QPointF(sx - ln * z * 0.5, sy), QPointF(sx + ln * z * 0.5, sy))

    def _draw_vignette(self, p: QPainter) -> None:
        """屏幕四角轻微压暗，让视线聚焦战场中央（按视口尺寸缓存）。"""
        w, h = self.width(), self.height()
        if (self._vignette is None or self._vignette.width() != w
                or self._vignette.height() != h):
            pm = QPixmap(w, h)
            pm.fill(Qt.transparent)
            q = QPainter(pm)
            g = QRadialGradient(QPointF(w / 2, h / 2), math.hypot(w, h) * 0.62)
            g.setColorAt(0.0, QColor(0, 0, 0, 0))
            g.setColorAt(0.72, QColor(0, 0, 0, 0))
            g.setColorAt(1.0, QColor(8, 10, 16, 96))
            q.fillRect(0, 0, w, h, QBrush(g))
            q.end()
            self._vignette = pm
        p.drawPixmap(0, 0, self._vignette)

    # ------------------------------------------------------------ 占领区图层
    def _territory_pixmap(self) -> QPixmap | None:
        """把势力归属烙成一张小位图（1 势力格 = 1 像素），随 field_version 缓存。

        每帧只需一次带缩放的 drawPixmap，替代原先上千次 drawRect；
        前线线段也在这里一并提取（世界坐标），绘制时再变换。
        """
        field = self.world.field
        if field is None:
            return None
        if (self._territory_cache is not None
                and self._territory_version == self.world.field_version):
            return self._territory_cache

        gw, gh = field.gw, field.gh
        owner = field.owner
        pr, pg, pb = FACTION_COLOR[Faction.PLAYER]
        er, eg, eb = FACTION_COLOR[Faction.ENEMY]
        pfill = (60 << 24) | (pr << 16) | (pg << 8) | pb
        efill = (60 << 24) | (er << 16) | (eg << 8) | eb
        img = QImage(gw, gh, QImage.Format_ARGB32)
        img.fill(0)
        for gy in range(gh):
            orow = owner[gy]
            for gx in range(gw):
                o = orow[gx]
                if o:
                    img.setPixel(gx, gy, pfill if o > 0 else efill)

        # 前线：只在「我方格 ↔ 敌方格」相邻处记一条线段（世界坐标）
        step = field.step_px
        segs: list[tuple[float, float, float, float]] = []
        for gy in range(gh):
            orow = owner[gy]
            for gx in range(gw):
                o = orow[gx]
                if o == 0:
                    continue
                if field.owner_at(gx + 1, gy) == -o:            # 右邻为敌
                    x = (gx + 1) * step
                    segs.append((x, gy * step, x, (gy + 1) * step))
                if field.owner_at(gx, gy + 1) == -o:            # 下邻为敌
                    y = (gy + 1) * step
                    segs.append((gx * step, y, (gx + 1) * step, y))
        self._frontline = segs
        self._territory_cache = QPixmap.fromImage(img)
        self._territory_version = self.world.field_version
        return self._territory_cache

    def _draw_territory(self, p: QPainter) -> None:
        """硬边领土：缓存位图整张贴上（关闭平滑保持硬边），再描前线。"""
        pm = self._territory_pixmap()
        if pm is None:
            return
        field = self.world.field
        step = field.step_px
        z = self.zoom
        # 视口对应的源矩形（单位：势力格 = 位图像素）
        src = QRectF(self.cam_x / step, self.cam_y / step,
                     self.width() / z / step, self.height() / z / step)
        smooth = p.testRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)   # 最近邻，保持硬边
        p.drawPixmap(QRectF(0, 0, self.width(), self.height()), pm, src)
        p.setRenderHint(QPainter.SmoothPixmapTransform, smooth)

        # 前线：只画进入视口的线段
        pen = QPen(QColor(255, 244, 214, 235), 2.4)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        ox, oy = self.cam_x, self.cam_y
        w, h = self.width(), self.height()
        for x0, y0, x1, y1 in self._frontline:
            sx0, sy0 = (x0 - ox) * z, (y0 - oy) * z
            sx1, sy1 = (x1 - ox) * z, (y1 - oy) * z
            if max(sx0, sx1) < 0 or min(sx0, sx1) > w:
                continue
            if max(sy0, sy1) < 0 or min(sy0, sy1) > h:
                continue
            p.drawLine(QPointF(sx0, sy0), QPointF(sx1, sy1))



    # ------------------------------------------------------------ 战争迷雾图层
    FOG_SCALE = 4      # 迷雾在 1/4 分辨率下绘制再放大（柔和图层，看不出差别）

    def _draw_fog(self, p: QPainter) -> None:
        """我军视野之外罩一层暗雾。

        低分辨率离屏图上铺满雾色，用 DestinationOut 把视野圆「擦」出来，
        再平滑放大贴回——大圆填充成本降约 FOG_SCALE² 倍，边缘自然柔化。
        """
        w, h = self.width(), self.height()
        s = self.FOG_SCALE
        fw, fh = max(1, w // s), max(1, h // s)
        if self._fog_img is None or self._fog_img.width() != fw or self._fog_img.height() != fh:
            self._fog_img = QImage(fw, fh, QImage.Format_ARGB32_Premultiplied)
        img = self._fog_img
        img.fill(QColor(10, 12, 18, 120))
        fp = QPainter(img)
        fp.setRenderHint(QPainter.Antialiasing, True)
        fp.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        fp.setPen(Qt.NoPen)
        gm = self.world.map
        for u in self.world.units_of(Faction.PLAYER):
            info = TERRAIN_INFO[gm.terrain_at(u.x, u.y)]
            r = u.spec.vision * info.vision * self.zoom
            c = self.to_screen(u.x, u.y)
            if (c.x() + r < 0 or c.x() - r > w or c.y() + r < 0 or c.y() - r > h):
                continue
            # 径向渐变擦除：视野中心全亮，靠边缘渐暗，雾界柔和无硬圆
            cc = QPointF(c.x() / s, c.y() / s)
            g = QRadialGradient(cc, r / s)
            g.setColorAt(0.0, QColor(0, 0, 0, 255))
            g.setColorAt(0.78, QColor(0, 0, 0, 255))
            g.setColorAt(1.0, QColor(0, 0, 0, 0))
            fp.setBrush(QBrush(g))
            fp.drawEllipse(cc, r / s, r / s)
        fp.end()
        sm = p.testRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(QRectF(0, 0, w, h), img)
        p.setRenderHint(QPainter.SmoothPixmapTransform, sm)

    def _draw_grid(self, p: QPainter, src: QRectF) -> None:
        p.setPen(QPen(QColor(255, 255, 255, 26), 1))
        step = TILE * 4
        x = math.floor(src.left() / step) * step
        while x < src.right():
            sx = (x - self.cam_x) * self.zoom
            p.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
            x += step
        y = math.floor(src.top() / step) * step
        while y < src.bottom():
            sy = (y - self.cam_y) * self.zoom
            p.drawLine(QPointF(0, sy), QPointF(self.width(), sy))
            y += step

    def tick_fx(self, dt: float) -> None:
        """衰减 UI 特效（攻击下令红圈等）；由 BattlePage 每帧调用。"""
        if dt <= 0.0 or not self._attack_pings:
            return
        self._attack_pings = [(x, y, t - dt) for x, y, t in self._attack_pings if t - dt > 0.0]

    def push_attack_ping(self, wx: float, wy: float) -> None:
        """指定攻击下达后在目标处闪一圈红。"""
        self._attack_pings.append((wx, wy, ATTACK_PING_TTL))

    def _draw_attack_pings(self, p: QPainter) -> None:
        if not self._attack_pings:
            return
        for wx, wy, left in self._attack_pings:
            life = left / ATTACK_PING_TTL
            c = self.to_screen(wx, wy)
            r = (18.0 + 22.0 * (1.0 - life)) * self.zoom
            alpha = int(220 * life)
            p.setPen(QPen(QColor(255, 70, 55, alpha), max(1.8, 2.2 * self.zoom)))
            p.setBrush(QColor(255, 60, 40, int(55 * life)))
            p.drawEllipse(c, r, r)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 90, 70, int(180 * life)))
            p.drawEllipse(c, 3.5 * self.zoom, 3.5 * self.zoom)

    def _draw_memory_ghosts(self, p: QPainter) -> None:
        """主地图：曾见但当前迷雾外的敌军淡影（按记忆年龄淡出）。"""
        if self.edit_mode or not self.fog_active():
            return
        for wx, wy, age, _uid in self.world.player_memory_ghosts():
            c = self.to_screen(wx, wy)
            if not (-20 <= c.x() <= self.width() + 20
                    and -20 <= c.y() <= self.height() + 20):
                continue
            fade = 1.0 - age
            alpha = int(28 + 70 * fade)
            r = max(5.0, 11.0 * self.zoom)
            p.setPen(QPen(QColor(255, 120, 100, int(alpha * 0.9)),
                          max(1.0, 1.2 * self.zoom), Qt.DashLine))
            p.setBrush(QColor(200, 60, 50, alpha))
            p.drawEllipse(c, r, r)

    def _draw_paths(self, p: QPainter) -> None:
        for u in self.world.units:
            if u.faction is not Faction.PLAYER:
                continue
            if not u.selected and u is not self.hovered:
                continue
            # 指定攻击：红虚线连向锁定目标
            if u.order == "attack" and u.target_uid is not None:
                tgt = self.world.unit_by_uid(u.target_uid)
                if tgt is not None and self.unit_visible(tgt):
                    a = self.to_screen(u.x, u.y)
                    b = self.to_screen(tgt.x, tgt.y)
                    pen = QPen(QColor(255, 110, 90, 200), 2.0, Qt.DashLine)
                    pen.setCosmetic(True)
                    p.setPen(pen)
                    p.setBrush(Qt.NoBrush)
                    p.drawLine(a, b)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 90, 70, 210))
                    p.drawEllipse(b, 4.5, 4.5)
            if u.path:
                path = QPainterPath(self.to_screen(u.x, u.y))
                for wx, wy in u.path:
                    path.lineTo(self.to_screen(wx, wy))
                pen = QPen(QColor(120, 220, 255, 150), 2.0, Qt.DashLine)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPath(path)
                end = self.to_screen(*u.path[-1])
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(120, 220, 255, 190))
                p.drawEllipse(end, 3.5, 3.5)
            # Shift 指令队列：琥珀虚线连接后续路点
            if u.selected and u.order_queue:
                prev = (u.path[-1] if u.path
                        else (u.x, u.y))
                for entry in u.order_queue:
                    kind = entry.get("kind", "move")
                    if kind == "attack":
                        t = self.world.unit_by_uid(entry.get("target_uid"))
                        if t is None:
                            continue
                        a = self.to_screen(*prev)
                        b = self.to_screen(t.x, t.y)
                        pen = QPen(QColor(255, 180, 90, 160), 1.6, Qt.DotLine)
                        pen.setCosmetic(True)
                        p.setPen(pen)
                        p.drawLine(a, b)
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(255, 160, 70, 180))
                        p.drawEllipse(b, 4.0, 4.0)
                        prev = (t.x, t.y)
                    else:
                        pts = list(entry.get("points") or [])
                        if kind == "move":
                            pts = [(entry["x"], entry["y"])]
                        if not pts:
                            continue
                        qp = QPainterPath(self.to_screen(*prev))
                        for wx, wy in pts:
                            qp.lineTo(self.to_screen(wx, wy))
                        pen = QPen(QColor(255, 190, 100, 140), 1.8, Qt.DotLine)
                        pen.setCosmetic(True)
                        p.setPen(pen)
                        p.setBrush(Qt.NoBrush)
                        p.drawPath(qp)
                        end = self.to_screen(*pts[-1])
                        p.setPen(Qt.NoPen)
                        p.setBrush(QColor(255, 190, 100, 170))
                        p.drawEllipse(end, 3.0, 3.0)
                        prev = pts[-1]

    # ------------------------------------------------------------ 射程圈
    def _range_pixmap(self, type_key: str) -> QPixmap:
        """射程圈贴图：渐变填充+虚线圈渲染一次，按 (兵种, 量化缩放) 缓存。

        渐变大圆每帧现画很贵；缩放量化到 5% 一档，缩放动画期间圈的尺寸
        误差肉眼不可辨，缓存命中率却接近 100%。
        """
        zq = max(1, round(self.zoom * 20))          # 量化：0.05 一档
        key = (type_key, zq)
        pm = self._range_cache.get(key)
        if pm is not None:
            return pm
        rr = UNIT_TYPES[type_key].attack_range * (zq / 20.0)
        pad = 3.0
        size = int((rr + pad) * 2) + 2
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing, True)
        c = QPointF(size / 2, size / 2)
        style = FACTION_STYLE[Faction.PLAYER]
        base, hi = style["base"], style["hi"]
        g = QRadialGradient(c, rr)
        g.setColorAt(0.0, QColor(base.red(), base.green(), base.blue(), 8))
        g.setColorAt(0.82, QColor(base.red(), base.green(), base.blue(), 16))
        g.setColorAt(0.97, QColor(base.red(), base.green(), base.blue(), 44))
        g.setColorAt(1.0, QColor(base.red(), base.green(), base.blue(), 0))
        q.setPen(Qt.NoPen)
        q.setBrush(QBrush(g))
        q.drawEllipse(c, rr, rr)
        pen = QPen(QColor(hi.red(), hi.green(), hi.blue(), 170), 1.6, Qt.DashLine)
        pen.setDashPattern([5.0, 4.0])
        q.setPen(pen)
        q.setBrush(Qt.NoBrush)
        q.drawEllipse(c, rr, rr)
        q.end()
        if len(self._range_cache) > 24:             # 缩放跨度大时防积攒
            self._range_cache.clear()
        self._range_cache[key] = pm
        return pm

    def _draw_attack_ranges(self, p: QPainter) -> None:
        """选中的己方兵团在地图上画出攻击范围（缓存贴图，逐单位平移贴上）。"""
        w, h = self.width(), self.height()
        for u in self.world.units:
            if not u.selected or not u.alive or u.faction is not Faction.PLAYER:
                continue
            c = self.to_screen(u.x, u.y)
            rr = u.spec.attack_range * self.zoom
            # 视口裁剪：整个圈都在屏幕外则跳过
            if (c.x() + rr < 0 or c.x() - rr > w or
                    c.y() + rr < 0 or c.y() - rr > h):
                continue
            pm = self._range_pixmap(u.type_key)
            half = pm.width() / 2
            p.drawPixmap(QPointF(c.x() - half, c.y() - half), pm)

    # ------------------------------------------------------------ 交战表现
    def _draw_combat(self, p: QPainter) -> None:
        """接战可视化：远程画曳光弹道，近战画交锋闪光，让前线一眼可辨。"""
        t = self.world.elapsed
        for u in self.world.units:
            if not u.alive or u.target_uid is None:
                continue
            if not self.unit_visible(u):
                continue
            tgt = self.world.unit_by_uid(u.target_uid)
            if tgt is None or not tgt.alive or not self.unit_visible(tgt):
                continue
            if u.distance_to(tgt) > u.spec.attack_range:
                continue
            a = self.to_screen(u.x, u.y)
            b = self.to_screen(tgt.x, tgt.y)
            # 双端都在屏幕外则跳过
            m = 60
            if not (QRectF(-m, -m, self.width() + 2 * m, self.height() + 2 * m)
                    .contains(a) or
                    QRectF(-m, -m, self.width() + 2 * m, self.height() + 2 * m)
                    .contains(b)):
                continue
            trace = FACTION_STYLE[u.faction]["trace"]
            if u.role == "ranged":
                # 曳光：短亮段沿弹道循环飞行
                dx, dy = b.x() - a.x(), b.y() - a.y()
                dist = math.hypot(dx, dy)
                if dist < 1e-6:
                    continue
                # 暗淡的完整弹道
                pen = QPen(QColor(trace.red(), trace.green(), trace.blue(), 46), 1.2)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawLine(a, b)
                # 循环飞行的亮段（每单位相位错开，避免整齐划一）
                phase = (t * 2.2 + u.uid * 0.37) % 1.0
                seg = 0.16
                k0, k1 = phase, min(1.0, phase + seg)
                pen = QPen(QColor(trace.red(), trace.green(), trace.blue(), 220),
                           max(1.6, 2.0 * self.zoom), Qt.SolidLine, Qt.RoundCap)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawLine(QPointF(a.x() + dx * k0, a.y() + dy * k0),
                           QPointF(a.x() + dx * k1, a.y() + dy * k1))
            else:
                # 近战：接触点画脉动的交锋闪光
                mx, my = (a.x() + b.x()) / 2, (a.y() + b.y()) / 2
                pulse = 0.5 + 0.5 * math.sin(t * 9.0 + u.uid * 1.7)
                rr = (3.0 + 2.5 * pulse) * max(0.6, self.zoom)
                g = QRadialGradient(QPointF(mx, my), rr * 2.2)
                g.setColorAt(0.0, QColor(255, 240, 190, int(150 + 80 * pulse)))
                g.setColorAt(0.55, QColor(255, 180, 90, 90))
                g.setColorAt(1.0, QColor(255, 140, 60, 0))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(g))
                p.drawEllipse(QPointF(mx, my), rr * 2.2, rr * 2.2)
                # 十字火花
                pen = QPen(QColor(255, 244, 210, int(140 + 100 * pulse)),
                           max(1.0, 1.3 * self.zoom), Qt.SolidLine, Qt.RoundCap)
                pen.setCosmetic(True)
                p.setPen(pen)
                ang = t * 5.0 + u.uid
                for i in range(3):
                    aa = ang + i * math.pi / 3
                    p.drawLine(QPointF(mx - math.cos(aa) * rr, my - math.sin(aa) * rr),
                               QPointF(mx + math.cos(aa) * rr, my + math.sin(aa) * rr))

    @staticmethod
    def _shape_polygon(kind: str, cx: float, cy: float, r: float,
                       facing: float) -> QPolygonF | None:
        """按兵种给出外形多边形；圆形返回 None（由调用方画椭圆）。"""
        if kind == "circle":
            return None
        if kind == "triangle":       # 骑兵：指向朝向的箭头
            tip = QPointF(cx + math.cos(facing) * r * 1.35,
                          cy + math.sin(facing) * r * 1.35)
            a1 = facing + 2.5
            a2 = facing - 2.5
            return QPolygonF([tip,
                              QPointF(cx + math.cos(a1) * r * 1.15,
                                      cy + math.sin(a1) * r * 1.15),
                              QPointF(cx + math.cos(a2) * r * 1.15,
                                      cy + math.sin(a2) * r * 1.15)])
        if kind == "diamond":        # 弓兵：菱形（轴向固定，稳定好认）
            rr = r * 1.2
            return QPolygonF([QPointF(cx, cy - rr), QPointF(cx + rr, cy),
                              QPointF(cx, cy + rr), QPointF(cx - rr, cy)])
        # hexagon 炮兵：六边形（重装、机械感）
        pts = []
        for i in range(6):
            a = math.radians(-90 + i * 60)
            pts.append(QPointF(cx + math.cos(a) * r * 1.12, cy + math.sin(a) * r * 1.12))
        return QPolygonF(pts)

    def _draw_unit(self, p: QPainter, u: Unit) -> None:
        c = self.to_screen(u.x, u.y)
        r = max(4.0, u.spec.radius * self.zoom)
        if not (-r * 3 <= c.x() <= self.width() + r * 3 and -r * 3 <= c.y() <= self.height() + r * 3):
            return

        style = FACTION_STYLE[u.faction]
        kind = u.spec.shape
        t = self.world.elapsed

        def draw_shape(dr: float = 0.0, dx: float = 0.0, dy: float = 0.0) -> None:
            """按当前画笔/画刷画本兵种外形（dr 为半径增量，dx/dy 偏移）。"""
            pl = self._shape_polygon(kind, c.x() + dx, c.y() + dy, r + dr, u.facing)
            if pl is None:
                p.drawEllipse(QPointF(c.x() + dx, c.y() + dy), r + dr, r + dr)
            else:
                p.drawPolygon(pl)

        # 落地阴影：让兵牌浮在战场上，有棋子感
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 80))
        draw_shape(0.5, r * 0.14, r * 0.20)

        # 交战警示环：正在开火的兵牌外圈泛红光，前线一目了然
        in_combat = u.target_uid is not None
        if in_combat:
            tgt = self.world.unit_by_uid(u.target_uid)
            if tgt is not None and u.distance_to(tgt) <= u.spec.attack_range * 1.05:
                pulse = 0.5 + 0.5 * math.sin(t * 7.0 + u.uid * 1.3)
                g = QRadialGradient(c, r + 7 + 3 * pulse)
                g.setColorAt(0.55, QColor(255, 120, 60, 0))
                g.setColorAt(0.85, QColor(255, 130, 70, int(60 + 60 * pulse)))
                g.setColorAt(1.0, QColor(255, 120, 60, 0))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(g))
                p.drawEllipse(c, r + 7 + 3 * pulse, r + 7 + 3 * pulse)

        # 选中高亮：金色双环 + 同形描边
        if u.selected:
            p.setPen(QPen(QColor(255, 220, 110, 70), max(4.0, 5.0 * self.zoom)))
            p.setBrush(Qt.NoBrush)
            draw_shape(5)
            p.setPen(QPen(QColor(255, 236, 140, 235), max(1.6, 1.9 * self.zoom)))
            draw_shape(5)

        # 据守光环：静止越久越明显，青绿虚线工事圈
        ent = u.still_time
        if ent > ENTRENCH_TIME * 0.4 and not u.moving:
            glow = min(1.0, ent / ENTRENCH_TIME)
            pen = QPen(QColor(110, 235, 175, int(100 + 110 * glow)),
                       max(1.5, 1.9 * self.zoom))
            pen.setStyle(Qt.DashLine)
            pen.setDashPattern([3.2, 2.2])
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r + 3.5, r + 3.5)

        # 主体：阵营色立体渐变（左上受光），受击时整体提亮；溃兵去饱和发灰
        grad = QLinearGradient(c.x() - r, c.y() - r, c.x() + r * 0.6, c.y() + r)
        hi, base, lo = style["hi"], style["base"], style["lo"]
        if u.flash > 0:
            hi, base, lo = hi.lighter(160), base.lighter(170), lo.lighter(170)
        if u.routing:
            hi, base, lo = (QColor.fromHsv(x.hue(), x.saturation() // 3,
                                           min(255, x.value()))
                            for x in (hi, base, lo))
        grad.setColorAt(0.0, hi)
        grad.setColorAt(0.45, base)
        grad.setColorAt(1.0, lo)
        p.setPen(QPen(style["edge"], max(1.2, 1.8 * self.zoom)))
        p.setBrush(QBrush(grad))
        draw_shape()

        # 内圈高光：一圈细亮描边，增强「军棋兵牌」质感
        p.setPen(QPen(QColor(255, 255, 255, 70), max(0.8, 1.0 * self.zoom)))
        p.setBrush(Qt.NoBrush)
        draw_shape(-max(1.5, r * 0.22))

        # 朝向指示（三角形本身即指向，故略过）：短楔形，比细线更醒目
        if kind != "triangle":
            fx, fy = math.cos(u.facing), math.sin(u.facing)
            tip = QPointF(c.x() + fx * r * 1.55, c.y() + fy * r * 1.55)
            a1, a2 = u.facing + 2.75, u.facing - 2.75
            wedge = QPolygonF([
                tip,
                QPointF(c.x() + math.cos(a1) * r * 0.42 + fx * r * 0.9,
                        c.y() + math.sin(a1) * r * 0.42 + fy * r * 0.9),
                QPointF(c.x() + math.cos(a2) * r * 0.42 + fx * r * 0.9,
                        c.y() + math.sin(a2) * r * 0.42 + fy * r * 0.9)])
            p.setPen(QPen(style["edge"], max(0.8, 1.0 * self.zoom)))
            p.setBrush(QColor(255, 255, 255, 190))
            p.drawPolygon(wedge)

        # 托管标识：右上角一枚青色小圆点（AI 正在代管此团）
        if u.auto:
            dot = QPointF(c.x() + r * 0.95, c.y() - r * 0.95)
            p.setPen(QPen(QColor(10, 30, 30, 220), 1.0))
            p.setBrush(QColor(90, 230, 220, 235))
            p.drawEllipse(dot, max(2.2, r * 0.24), max(2.2, r * 0.24))

        # 溃逃标识：头顶一面摇晃的小白旗
        if u.routing:
            wob = math.sin(t * 10.0 + u.uid) * 0.25
            px, py = c.x() - r * 0.2, c.y() - r - max(6.0, r * 0.9)
            pole_h = max(7.0, r * 0.95)
            p.setPen(QPen(QColor(60, 50, 40, 230), max(1.2, 1.4 * self.zoom)))
            p.drawLine(QPointF(px, py + pole_h), QPointF(px, py))
            flag_w = max(6.0, r * 0.8)
            p.setPen(QPen(QColor(120, 120, 120, 200), 0.8))
            p.setBrush(QColor(245, 245, 240, 235))
            p.drawPolygon(QPolygonF([
                QPointF(px, py),
                QPointF(px + flag_w, py + flag_w * (0.28 + wob)),
                QPointF(px, py + flag_w * 0.55)]))

        # 老练度：左上角一到三道金色军功纹（老练/精锐/禁卫）
        lvl = u.vet_level
        if lvl > 0 and self.zoom > 0.55:
            p.setPen(QPen(QColor(255, 214, 96, 235), max(1.3, 1.6 * self.zoom),
                          Qt.SolidLine, Qt.RoundCap))
            w0 = max(3.0, r * 0.5)
            bx, by = c.x() - r * 1.15, c.y() - r * 0.95
            for i in range(lvl):
                yy = by + i * max(2.4, r * 0.28)
                p.drawLine(QPointF(bx, yy + w0 * 0.4), QPointF(bx + w0 * 0.5, yy))
                p.drawLine(QPointF(bx + w0 * 0.5, yy), QPointF(bx + w0, yy + w0 * 0.4))

        # 兵种角标（高缩放时）：带描影，任何地形上都清晰
        if self.zoom > 0.7:
            f = QFont()
            f.setStyleHint(QFont.SansSerif)
            f.setFamilies(["Microsoft YaHei", "SimHei", "Noto Sans CJK SC"])
            f.setPixelSize(max(8, int(r * 1.05)))
            f.setBold(True)
            p.setFont(f)
            box = QRectF(c.x() - r, c.y() - r, r * 2, r * 2)
            p.setPen(QColor(0, 0, 0, 150))
            p.drawText(box.translated(1, 1), Qt.AlignCenter, u.spec.letter)
            p.setPen(QColor(255, 255, 255, 245))
            p.drawText(box, Qt.AlignCenter, u.spec.letter)

        # 血条：暗底 + 分段刻度 + 阵营描边，掉血才显示
        ratio = u.hp_ratio
        if ratio < 1.0:
            bw, bh = r * 2.4, max(2.5, 3.4 * self.zoom)
            bx, by = c.x() - bw / 2, c.y() - r - bh - 6
            p.setPen(QPen(QColor(0, 0, 0, 200), 1.0))
            p.setBrush(QColor(14, 16, 20, 200))
            p.drawRect(QRectF(bx - 1, by - 1, bw + 2, bh + 2))
            col = QColor(110, 220, 110) if ratio > 0.5 else (
                QColor(238, 200, 80) if ratio > 0.25 else QColor(235, 86, 70))
            grad2 = QLinearGradient(bx, by, bx, by + bh)
            grad2.setColorAt(0.0, col.lighter(135))
            grad2.setColorAt(1.0, col.darker(120))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad2))
            p.drawRect(QRectF(bx, by, bw * ratio, bh))
            # 四分之一刻度线
            p.setPen(QPen(QColor(0, 0, 0, 110), 1.0))
            for i in range(1, 4):
                x = bx + bw * i / 4
                p.drawLine(QPointF(x, by), QPointF(x, by + bh))

        # 士气条：血条下方一条更细的紫金条，动摇（<70%）才显示
        if u.morale < 0.7 or u.routing:
            mw, mh = r * 2.4, max(1.8, 2.2 * self.zoom)
            mx = c.x() - mw / 2
            my = c.y() - r - mh - (11 if ratio < 1.0 else 5)
            p.setPen(QPen(QColor(0, 0, 0, 190), 1.0))
            p.setBrush(QColor(14, 16, 20, 190))
            p.drawRect(QRectF(mx - 1, my - 1, mw + 2, mh + 2))
            mcol = QColor(240, 240, 240) if u.routing else (
                QColor(196, 150, 255) if u.morale > MORALE_ROUT else QColor(255, 120, 90))
            p.setPen(Qt.NoPen)
            p.setBrush(mcol)
            p.drawRect(QRectF(mx, my, mw * max(0.03, u.morale), mh))


    def _draw_brush(self, p: QPainter) -> None:
        if len(self._brush) < 2:
            return
        gm = self.world.map
        # 障碍高亮：笔迹穿过的不可通行格画红色块、被挡需绕行的相邻段画橙色虚线，
        # 让玩家在画线时就能看到哪里会受阻、会被自动绕行。
        for i in range(len(self._brush) - 1):
            x0, y0 = self._brush[i]
            x1, y1 = self._brush[i + 1]
            if not gm.line_clear(x0, y0, x1, y1):
                a = self.to_screen(x0, y0)
                b = self.to_screen(x1, y1)
                pen = QPen(QColor(255, 140, 60, 210), 3.0, Qt.DashLine)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.drawLine(a, b)
        for wx, wy in self._brush:
            if not gm.passable(wx, wy):
                c = self.to_screen(wx, wy)
                half = TILE * self.zoom * 0.5
                p.setPen(QPen(QColor(255, 70, 70, 220), 1.4))
                p.setBrush(QColor(255, 70, 70, 70))
                p.drawRect(QRectF(c.x() - half, c.y() - half, half * 2, half * 2))
        # 笔迹本体（黄）
        path = QPainterPath(self.to_screen(*self._brush[0]))
        for wx, wy in self._brush[1:]:
            path.lineTo(self.to_screen(wx, wy))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 240, 150, 90), 9.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)
        p.setPen(QPen(QColor(255, 252, 220, 230), 2.4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPath(path)

    def _draw_selection_rect(self, p: QPainter) -> None:
        if self._select_origin is None or self._select_current is None:
            return
        a, b = self._select_origin, self._select_current
        rect = QRectF(self.to_screen(a.x(), a.y()), self.to_screen(b.x(), b.y())).normalized()
        p.setPen(QPen(QColor(255, 236, 140, 210), 1.4, Qt.DashLine))
        p.setBrush(QColor(255, 236, 140, 34))
        p.drawRect(rect)

    def _draw_edit_cursor(self, p: QPainter) -> None:
        """编辑模式下在光标处画一个提示：地形/擦除画笔刷footprint，摆兵画幽灵兵团。"""
        ed = self.editor
        if ed is None or self._edit_hover is None:
            return
        wx, wy = self._edit_hover
        gm = self.world.map
        if ed.tool == "unit":
            c = self.to_screen(wx, wy)
            r = max(4.0, UNIT_TYPES[ed.unit_type].radius * self.zoom)
            col = _qcolor(FACTION_COLOR[ed.faction])
            p.setPen(QPen(QColor(255, 255, 255, 200), 1.5))
            p.setBrush(QColor(col.red(), col.green(), col.blue(), 120))
            p.drawEllipse(c, r, r)
            return
        # 地形笔刷 / 擦除：画覆盖到的格子块
        tx, ty = gm.tile_at(wx, wy)
        rad = ed.cursor_radius()
        top = self.to_screen((tx - rad) * TILE, (ty - rad) * TILE)
        size = (2 * rad + 1) * TILE * self.zoom
        rect = QRectF(top, top)
        rect.setWidth(size)
        rect.setHeight(size)
        if ed.tool == "erase":
            edge, fill = QColor(230, 96, 84, 235), QColor(230, 96, 84, 40)
        else:
            r0, g0, b0 = TERRAIN_INFO[ed.terrain].color
            edge, fill = QColor(255, 255, 255, 220), QColor(r0, g0, b0, 110)
        p.setPen(QPen(edge, 1.6))
        p.setBrush(fill)
        p.drawRect(rect)

    def _draw_minimap(self, p: QPainter) -> None:
        gm = self.world.map
        size = 172
        scale = size / max(gm.pixel_width, gm.pixel_height)
        mw, mh = gm.pixel_width * scale, gm.pixel_height * scale
        ox, oy = self.width() - mw - 12, self.height() - mh - 12
        self._minimap_rect = QRectF(ox, oy, mw, mh)

        # 底座：圆角深色渐变 + 双层描边，像一块嵌进 HUD 的罗盘
        frame = self._minimap_rect.adjusted(-5, -5, 5, 5)
        g = QLinearGradient(frame.topLeft(), frame.bottomLeft())
        g.setColorAt(0.0, QColor(30, 35, 46, 235))
        g.setColorAt(1.0, QColor(14, 17, 23, 235))
        p.setPen(QPen(QColor(96, 108, 130, 200), 1.2))
        p.setBrush(QBrush(g))
        p.drawRoundedRect(frame, 6, 6)
        p.setPen(QPen(QColor(0, 0, 0, 200), 1.6))
        p.setBrush(Qt.NoBrush)
        p.drawRect(self._minimap_rect.adjusted(-1, -1, 1, 1))
        # 小地图地形：预缩放一次缓存，避免每帧从全尺寸 pixmap 重采样
        if (self._minimap_cache is None
                or self._minimap_cache.width() != int(mw)
                or self._minimap_cache.height() != int(mh)):
            self._minimap_cache = self._terrain_pixmap().scaled(
                max(1, int(mw)), max(1, int(mh)),
                Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        p.drawPixmap(self._minimap_rect.topLeft(), self._minimap_cache)

        # 敌情记忆：迷雾外曾见敌军的淡红点（先于实心点，可见单位会盖住）
        if self.fog_active():
            p.setPen(Qt.NoPen)
            for wx, wy, age, _uid in self.world.player_memory_ghosts():
                fade = 1.0 - age
                p.setBrush(QColor(220, 70, 55, int(40 + 120 * fade)))
                p.drawEllipse(QPointF(ox + wx * scale, oy + wy * scale), 2.0, 2.0)

        # 交战点：闪烁的火光标记，先画在兵点下面
        t = self.world.elapsed
        blink = 0.5 + 0.5 * math.sin(t * 6.0)
        p.setPen(Qt.NoPen)
        for u in self.world.units:
            if u.target_uid is None or not self.unit_visible(u):
                continue
            p.setBrush(QColor(255, 170, 60, int(70 + 110 * blink)))
            p.drawEllipse(QPointF(ox + u.x * scale, oy + u.y * scale), 3.6, 3.6)

        for u in self.world.units:
            if not self.unit_visible(u):
                continue
            mc = QPointF(ox + u.x * scale, oy + u.y * scale)
            p.setPen(QPen(QColor(0, 0, 0, 200), 0.8))
            p.setBrush(FACTION_STYLE[u.faction]["hi"] if u.selected
                       else FACTION_STYLE[u.faction]["base"])
            p.drawEllipse(mc, 2.2, 2.2)

        view = QRectF(ox + self.cam_x * scale, oy + self.cam_y * scale,
                      self.width() / self.zoom * scale, self.height() / self.zoom * scale)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 190), 2.4))     # 视口框：黑衬底 + 白细线
        p.drawRect(view)
        p.setPen(QPen(QColor(255, 255, 255, 220), 1.0))
        p.drawRect(view)

    # ------------------------------------------------------------ 输入
    def _on_minimap(self, pos: QPointF) -> bool:
        return getattr(self, "_minimap_rect", QRectF()).contains(pos)

    def _minimap_to_world(self, pos: QPointF) -> tuple[float, float]:
        gm = self.world.map
        r = self._minimap_rect
        return ((pos.x() - r.x()) / r.width() * gm.pixel_width,
                (pos.y() - r.y()) / r.height() * gm.pixel_height)

    def mousePressEvent(self, e: QMouseEvent) -> None:
        pos = e.position()
        if e.button() == Qt.LeftButton and self._on_minimap(pos):
            self.center_on(*self._minimap_to_world(pos))
            self.update()
            return

        if self.edit_mode:
            if self._on_minimap(pos):
                return                                    # 小地图区域不当作画布
            wx, wy = self.to_world(pos)
            if e.button() == Qt.LeftButton:
                self._painting = True
                self._after_edit(self.editor.on_press(wx, wy))
            elif e.button() == Qt.RightButton:
                # 右键按下先当作「可能的平移」；松手时若没拖动过再执行擦除
                self._edit_pan = True
                self._edit_pan_moved = False
                self._edit_pan_from = (wx, wy)
                self._last_mouse = pos.toPoint()
                self.setCursor(Qt.ClosedHandCursor)
            elif e.button() == Qt.MiddleButton:
                self._dragging_cam = True
                self._last_mouse = pos.toPoint()
                self.setCursor(Qt.ClosedHandCursor)
            return

        wx, wy = self.to_world(pos)
        if e.button() == Qt.LeftButton:
            unit = self.world.unit_at(wx, wy)
            additive = bool(e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
            if (unit is not None and unit.faction is Faction.PLAYER
                    and not unit.routing):
                if not additive:
                    self.world.clear_selection()
                unit.selected = not unit.selected if additive else True
                self.selection_changed.emit()
            else:
                if not additive:
                    self.world.clear_selection()
                    self.selection_changed.emit()
                self._select_origin = QPointF(wx, wy)
                self._select_current = QPointF(wx, wy)
        elif e.button() == Qt.RightButton:
            if self.world.selected:
                self._drawing = True
                self._brush = [(wx, wy)]
        elif e.button() == Qt.MiddleButton:
            self._dragging_cam = True
            self._last_mouse = pos.toPoint()
            self._attack_cursor = False
            self.setCursor(Qt.ClosedHandCursor)
        self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        pos = e.position()
        wx, wy = self.to_world(pos)

        if self._dragging_cam:
            d = pos.toPoint() - self._last_mouse
            self.cam_x -= d.x() / self.zoom
            self.cam_y -= d.y() / self.zoom
            self._last_mouse = pos.toPoint()
            self.clamp_camera()
            self.update()
            return

        if self.edit_mode:
            if self._edit_pan:                       # 右键拖动 = 平移视角
                d = pos.toPoint() - self._last_mouse
                if d.manhattanLength() > 2:
                    self._edit_pan_moved = True
                self.cam_x -= d.x() / self.zoom
                self.cam_y -= d.y() / self.zoom
                self._last_mouse = pos.toPoint()
                self.clamp_camera()
                self.update()
                return
            self._edit_hover = (wx, wy)
            if self._painting and (e.buttons() & Qt.LeftButton):
                self._after_edit(self.editor.on_drag(wx, wy))
            self.hover_changed.emit(self._edit_hover_text(wx, wy))
            self.update()
            return

        if self._select_origin is not None:
            self._select_current = QPointF(wx, wy)
        elif self._drawing:
            lx, ly = self._brush[-1]
            if math.hypot(wx - lx, wy - ly) > 4.0 / self.zoom:
                self._brush.append((wx, wy))

        self.hovered = self.world.unit_at(wx, wy)
        if self.hovered is not None and not self.unit_visible(self.hovered):
            self.hovered = None       # 迷雾里的敌军：悬停也不暴露
        self._update_order_cursor()
        self.hover_changed.emit(self._hover_text(wx, wy))
        self.update()

    def _foe_under_cursor(self) -> Unit | None:
        """有选中且悬停在可见、非溃逃敌军上时返回该敌军。"""
        u = self.hovered
        if (u is not None and u.faction is Faction.ENEMY and not u.routing
                and self.world.selected and self.unit_visible(u)):
            return u
        return None

    def _update_order_cursor(self) -> None:
        """有选中悬停敌军 → 攻击十字；平移中不改；其余恢复箭头。"""
        if self._dragging_cam or self.edit_mode:
            return
        want = self._foe_under_cursor() is not None
        if want and not self._attack_cursor:
            self.setCursor(Qt.CrossCursor)
            self._attack_cursor = True
        elif not want and self._attack_cursor:
            self.unsetCursor()
            self._attack_cursor = False

    def _hover_text(self, wx: float, wy: float) -> str:
        gm = self.world.map
        info = gm.info_at(wx, wy)
        tx, ty = gm.tile_at(wx, wy)
        text = (f"({tx},{ty}) {info.name} — 速度×{info.speed:.2f} 防御×{info.defense:.2f} "
                f"攻击×{info.attack:.2f}｜{info.desc}")
        if self.hovered is not None:
            u = self.hovered
            tags = []
            if u.routing:
                tags.append("溃逃中")
            if u.vet_level > 0:
                tags.append(u.vet_name)
            if u.still_time > ENTRENCH_TIME * 0.4 and not u.moving:
                tags.append(f"据守+{(u.entrench - 1) * 100:.0f}%")
            if u.order == "attack" and u.target_uid is not None:
                tags.append("指定攻击")
            if u.moving and not u.routing:
                tags.append("行军中")
            if u.auto:
                tags.append("托管")
            tag = ("  [" + " ".join(tags) + "]") if tags else ""
            side = "我军" if u.faction is Faction.PLAYER else "敌军"
            unit_line = (f"{side}·{u.spec.name}#{u.uid} "
                         f"HP {u.hp:.0f}/{u.max_hp:.0f} 士气 {u.morale * 100:.0f}%{tag}")
            if (u.faction is Faction.ENEMY and not u.routing
                    and self.world.selected):
                unit_line = f"右键＝指定攻击 · {unit_line}"
                # 选中我军 vs 悬停敌军：克制 / 齐射区间 / TTK
                atk = [s for s in self.world.selected
                       if s.alive and not s.routing and s.faction is Faction.PLAYER]
                if atk:
                    prev = self.world.combat_preview(atk, u)
                    if prev:
                        n = int(prev["n"])
                        ttk = prev["ttk"]
                        ttk_s = f"{ttk:.0f}s" if ttk < 900 else "—"
                        focus = f"{n}团集火 · " if n > 1 else ""
                        unit_line += (
                            f"  │  {focus}{prev['counter']}  "
                            f"下一轮 ~{prev['volley_lo']:.0f}–{prev['volley_hi']:.0f}  "
                            f"TTK≈{ttk_s}")
            text = unit_line + "    ｜  " + text
        return text

    # ------------------------------------------------------------ 编辑模式辅助
    def _after_edit(self, terrain_changed: bool) -> None:
        """一次编辑动作之后：地形变了就重建缓存，并通知外部刷新计数。"""
        if terrain_changed:
            self.invalidate_terrain()     # 内部已 update()
        else:
            self.update()
        self.edited.emit()

    def _edit_hover_text(self, wx: float, wy: float) -> str:
        gm = self.world.map
        info = gm.info_at(wx, wy)
        tx, ty = gm.tile_at(wx, wy)
        ed = self.editor
        if ed is None:
            return f"({tx},{ty}) {info.name}"
        if ed.tool == "terrain":
            n = 2 * ed.brush + 1
            tool = f"地形笔刷 · {TERRAIN_INFO[ed.terrain].name}（{n}×{n}）"
        elif ed.tool == "unit":
            tool = f"放置 · {FACTION_NAME[ed.faction]}{UNIT_TYPES[ed.unit_type].name}"
        else:
            tool = "擦除兵团（右键轻点/左键，右键拖拽平移）"
        return (f"编辑：{tool}    ｜  ({tx},{ty}) {info.name} "
                f"速×{info.speed:.2f} 防×{info.defense:.2f}")

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if self.edit_mode:
            if e.button() == Qt.LeftButton:
                self._painting = False
            elif e.button() == Qt.RightButton and self._edit_pan:
                self._edit_pan = False
                self.unsetCursor()
                if not self._edit_pan_moved:          # 没拖动过 → 视为轻点，擦除
                    self._after_edit(self.editor.on_secondary(*self._edit_pan_from))
            elif e.button() == Qt.MiddleButton:
                self._dragging_cam = False
                self.unsetCursor()
            self.update()
            return
        if e.button() == Qt.LeftButton and self._select_origin is not None:
            a, b = self._select_origin, self._select_current or self._select_origin
            if math.hypot(b.x() - a.x(), b.y() - a.y()) > 4.0:
                for u in self.world.units_in_rect(a.x(), a.y(), b.x(), b.y(), Faction.PLAYER):
                    if not u.routing:
                        u.selected = True
                self.selection_changed.emit()
            self._select_origin = self._select_current = None
        elif e.button() == Qt.RightButton and self._drawing:
            self._drawing = False
            sel = self.world.selected
            if sel:
                fm = self.formation_lock
                # Shift+右键 = 追加指令队列（不打断当前行动）
                queue = bool(e.modifiers() & Qt.ShiftModifier)
                qtag = "排队·" if queue else ""
                if len(self._brush) < 2:                      # 单击 = 向该点下令
                    wx, wy = self.to_world(e.position())
                    # 右键点在可见敌军上 → 指定攻击（锁定追击）
                    foe = self.world.unit_at(wx, wy)
                    if (foe is not None and foe.faction is Faction.ENEMY
                            and not foe.routing and self.unit_visible(foe)):
                        self.world.issue_attack(sel, foe, formation=fm, queue=queue)
                        self.push_attack_ping(foe.x, foe.y)
                        n = len([u for u in sel if u.alive and not u.routing])
                        label = (f"{qtag}{n} 团集火 {foe.spec.name}#{foe.uid}"
                                 if n > 1
                                 else f"{qtag}攻击 {foe.spec.name}#{foe.uid}")
                        self.hover_changed.emit(label)
                        self._brush = []
                        self.update()
                        return
                    pts = [(wx, wy)]
                else:                                         # 拖拽 = 画笔路线
                    pts = self._brush
                mode = self.order_mode
                if mode == "attack_move":
                    self.world.issue_attack_move(sel, pts, formation=fm, queue=queue)
                    self.hover_changed.emit(f"{qtag}攻击移动")
                elif mode == "patrol":
                    self.world.issue_patrol(sel, pts, formation=fm, queue=queue)
                    self.hover_changed.emit(f"{qtag}巡逻")
                elif len(pts) < 2:
                    self.world.issue_move(sel, pts[0][0], pts[0][1],
                                          formation=fm, queue=queue)
                    if queue:
                        self.hover_changed.emit("排队·移动")
                else:
                    self.world.issue_path(sel, pts, formation=fm, queue=queue)
                    if queue:
                        self.hover_changed.emit("排队·画笔路线")
            self._brush = []
        elif e.button() == Qt.MiddleButton:
            self._dragging_cam = False
            self._attack_cursor = False
            self.unsetCursor()
            self._update_order_cursor()
        self.update()

    def wheelEvent(self, e: QWheelEvent) -> None:
        pos = e.position()
        wx, wy = self.to_world(pos)
        factor = 1.15 ** (e.angleDelta().y() / 120.0)
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom * factor))
        if abs(new_zoom - self.zoom) < 1e-9:
            return
        self.zoom = new_zoom
        # 保持鼠标下的世界坐标不动
        self.cam_x = wx - pos.x() / self.zoom
        self.cam_y = wy - pos.y() / self.zoom
        self.clamp_camera()
        self.update()

    def keyPressEvent(self, e) -> None:
        step = 60 / self.zoom
        k = e.key()
        # Ctrl/Alt 组合留给上层快捷键（如 Ctrl+A 全选），避免 WASD 抢键
        mods = e.modifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            super().keyPressEvent(e)
            return
        # 仅方向键平移视角；WASD / A/P/M 留给指令快捷键，避免冲突
        if k == Qt.Key_Left:
            self.cam_x -= step
        elif k == Qt.Key_Right:
            self.cam_x += step
        elif k == Qt.Key_Up:
            self.cam_y -= step
        elif k == Qt.Key_Down:
            self.cam_y += step
        elif k == Qt.Key_Escape:
            # 编辑态 / 战斗态的 Esc 由上层（回菜单/回编辑器）处理
            super().keyPressEvent(e)
            return
        else:
            super().keyPressEvent(e)
            return
        self.clamp_camera()
        self.update()
