"""UI 层：开始界面、战斗页、侧栏，以及在两者之间切换的外壳窗口。"""
from __future__ import annotations

import time
import zlib

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QIcon, QImage,
                           QKeySequence, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QVBoxLayout, QWidget)

from .ai import DIFFICULTIES, Difficulty
from .editor import (TOOL_ERASE, TOOL_TERRAIN, TOOL_UNIT, BRUSH_RADII, MapEditor)
from .mapview import MapView
from .terrain import TERRAIN_INFO, GameMap, Terrain
from .units import FACTION_COLOR, FACTION_NAME, UNIT_TYPES, Faction
from .world import World, army_strength, load_world, save_world

TICK_MS = 33  # ≈30 FPS

# 兵种外形速记字符（图例与编辑器按钮共用）
UNIT_GLYPH = {"infantry": "●", "cavalry": "▲", "archer": "◆", "artillery": "⬢"}


def _section_header(text: str) -> QLabel:
    """侧栏/面板统一的分区标题：金色小竖条 + 加亮文字。"""
    lb = QLabel(text)
    lb.setObjectName("sectionhdr")
    return lb


def _vseparator() -> QFrame:
    """工具栏用的细竖分隔线，把按钮按功能分组。"""
    s = QFrame()
    s.setFrameShape(QFrame.VLine)
    s.setObjectName("toolsep")
    return s


# ==================================================================== 军力对比条
class StrengthBar(QWidget):
    """蓝/红军力天平：按双方兵团有效战力实时对比，交锋线平滑推移。

    与旧版「控制区域」对比不同，这里衡量的是**军队实力**——每团按当前 HP、
    老练加成、难度系数折算，溃逃部队只算三成。战损、溃逃都会立即压低一侧。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.p = 0.5              # 目标占比（我方）
        self._disp = 0.5          # 显示占比：向目标缓动，天平慢慢倾斜更有沉浸感

    def set_values(self, p: float, e: float) -> None:
        total = p + e
        self.p = (p / total) if total > 0 else 0.5
        # 每帧向目标推进一小步（≈30FPS 下约 0.4s 收敛），首帧直接对齐
        self._disp += (self.p - self._disp) * 0.18
        if abs(self.p - self._disp) < 0.0015:
            self._disp = self.p
        self.update()

    def paintEvent(self, _evt) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 13, 18))
        p.drawRoundedRect(0, 0, w, h, 7, 7)

        share = max(0.0, min(1.0, self._disp))
        pw = (w - 4) * share
        pr, pg, pb = FACTION_COLOR[Faction.PLAYER]
        er, eg, eb = FACTION_COLOR[Faction.ENEMY]
        # 双方色带：向交锋线渐亮，像两股兵锋在中间相抵
        gp = QLinearGradient(2, 0, 2 + pw, 0)
        gp.setColorAt(0.0, QColor(pr, pg, pb).darker(150))
        gp.setColorAt(1.0, QColor(pr, pg, pb).lighter(122))
        p.setBrush(QBrush(gp))
        p.drawRoundedRect(QRectF(2, 2, max(0.0, pw), h - 4), 5, 5)
        ge = QLinearGradient(2 + pw, 0, w - 2, 0)
        ge.setColorAt(0.0, QColor(er, eg, eb).lighter(122))
        ge.setColorAt(1.0, QColor(er, eg, eb).darker(150))
        p.setBrush(QBrush(ge))
        p.drawRoundedRect(QRectF(2 + pw, 2, max(0.0, w - 4 - pw), h - 4), 5, 5)
        # 顶部高光：一层薄玻璃质感
        gloss = QLinearGradient(0, 2, 0, h * 0.55)
        gloss.setColorAt(0.0, QColor(255, 255, 255, 46))
        gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(gloss))
        p.drawRoundedRect(QRectF(2, 2, w - 4, h * 0.5), 5, 5)
        # 中线刻度：50% 均势参考
        p.setPen(QPen(QColor(238, 244, 252, 70), 1.0, Qt.DashLine))
        p.drawLine(QPointF(w / 2, 3), QPointF(w / 2, h - 3))
        # 交锋线：金色亮刃 + 微光，与主图前线呼应
        glow = QRadialGradient(QPointF(2 + pw, h / 2), h * 0.9)
        glow.setColorAt(0.0, QColor(255, 226, 150, 90))
        glow.setColorAt(1.0, QColor(255, 226, 150, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(2 + pw, h / 2), h * 0.9, h * 0.9)
        p.setBrush(QColor(255, 240, 200, 245))
        p.drawRoundedRect(QRectF(2 + pw - 1.3, 1.5, 2.6, h - 3), 1.2, 1.2)
        p.setPen(QPen(QColor(66, 78, 102, 230), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 7, 7)

        f = QFont(); f.setBold(True); f.setPixelSize(12)
        p.setFont(f)
        for dx, dy, col in ((1, 1, QColor(0, 0, 0, 170)),
                            (0, 0, QColor(255, 255, 255, 240))):
            p.setPen(col)
            p.drawText(8 + dx, dy, w - 16, h, Qt.AlignLeft | Qt.AlignVCenter,
                       f"我军 {share*100:.0f}%")
            p.drawText(8 + dx, dy, w - 16, h, Qt.AlignRight | Qt.AlignVCenter,
                       f"{(1-share)*100:.0f}% 敌军")


# ==================================================================== 军力折线 + 时间线
_TIMELINE_STYLE = {
    "first_blood": (QColor(255, 210, 90), "首"),
    "kill": (QColor(230, 100, 90), "歼"),
    "rout": (QColor(200, 140, 255), "溃"),
    "rally": (QColor(110, 210, 150), "整"),
    "escape": (QColor(160, 160, 170), "散"),
}


class ForceChart(QWidget):
    """军力变化折线 + 关键事件时间刻度（只读绘制 World.stats 历史）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._history: list[tuple[float, float, float]] = []
        self._timeline: list[tuple[float, str, str]] = []
        self.setMinimumSize(520, 280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, history: list[tuple[float, float, float]],
                 timeline: list[tuple[float, str, str]] | None = None) -> None:
        self._history = list(history or [])
        self._timeline = list(timeline or [])
        self.update()

    def paintEvent(self, _evt) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(14, 17, 24))
        ml, mr, mt, mb = 48, 16, 28, 36
        plot = QRectF(ml, mt, max(1.0, w - ml - mr), max(1.0, h - mt - mb))

        hist = self._history
        if len(hist) < 1:
            p.setPen(QColor(140, 150, 165))
            p.drawText(self.rect(), Qt.AlignCenter, "尚无军力采样")
            return

        t0 = hist[0][0]
        t1 = hist[-1][0]
        if t1 <= t0:
            t1 = t0 + 1.0
        ymax = max(1.0, max(max(ps, es) for _, ps, es in hist))
        # 略留顶边
        ymax *= 1.08

        def sx(t: float) -> float:
            return plot.left() + (t - t0) / (t1 - t0) * plot.width()

        def sy(v: float) -> float:
            return plot.bottom() - (v / ymax) * plot.height()

        # 网格
        p.setPen(QPen(QColor(40, 48, 62), 1.0))
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            val = ymax * (1.0 - i / 4)
            p.setPen(QColor(120, 132, 150))
            p.drawText(QRectF(2, y - 8, ml - 6, 16),
                       Qt.AlignRight | Qt.AlignVCenter, f"{val:.0f}")
            p.setPen(QPen(QColor(40, 48, 62), 1.0))
        n_xt = min(6, max(2, int(plot.width() / 90)))
        for i in range(n_xt + 1):
            t = t0 + (t1 - t0) * i / n_xt
            x = sx(t)
            p.setPen(QPen(QColor(40, 48, 62), 1.0, Qt.DotLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            mm, ss = int(t) // 60, int(t) % 60
            p.setPen(QColor(120, 132, 150))
            p.drawText(QRectF(x - 28, plot.bottom() + 4, 56, 18),
                       Qt.AlignHCenter | Qt.AlignTop, f"{mm:02d}:{ss:02d}")

        # 面积填充 + 折线
        def build_path(idx: int) -> QPainterPath:
            path = QPainterPath()
            path.moveTo(sx(hist[0][0]), sy(hist[0][idx]))
            for t, ps, es in hist[1:]:
                path.lineTo(sx(t), sy(ps if idx == 1 else es))
            return path

        for idx, base, fill_a in (
                (1, FACTION_COLOR[Faction.PLAYER], 40),
                (2, FACTION_COLOR[Faction.ENEMY], 36)):
            line = build_path(idx)
            fill = QPainterPath(line)
            fill.lineTo(sx(hist[-1][0]), plot.bottom())
            fill.lineTo(sx(hist[0][0]), plot.bottom())
            fill.closeSubpath()
            c = QColor(*base)
            c.setAlpha(fill_a)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawPath(fill)
            pen = QPen(QColor(*base), 2.2)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(line)

        # 时间线事件刻度
        for t, kind, text in self._timeline:
            if t < t0 - 0.05 or t > t1 + 0.05:
                continue
            col, glyph = _TIMELINE_STYLE.get(kind, (QColor(200, 200, 200), "·"))
            x = sx(t)
            p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 160),
                          1.0, Qt.DashLine))
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QPointF(x, plot.top() + 6), 5.5, 5.5)
            p.setPen(QColor(20, 22, 28))
            f = QFont(); f.setPixelSize(9); f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(x - 8, plot.top() - 2, 16, 16),
                       Qt.AlignCenter, glyph)

        # 图例
        f = QFont(); f.setPixelSize(11)
        p.setFont(f)
        lx, ly = plot.left() + 8, plot.top() + 4
        for label, rgb in (("我军", FACTION_COLOR[Faction.PLAYER]),
                           ("敌军", FACTION_COLOR[Faction.ENEMY])):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(*rgb))
            p.drawRoundedRect(QRectF(lx, ly, 14, 4), 2, 2)
            p.setPen(QColor(210, 218, 230))
            p.drawText(int(lx + 18), int(ly + 8), label)
            lx += 64
        # 事件图例
        for kind, (col, glyph) in _TIMELINE_STYLE.items():
            p.setBrush(col)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(lx + 5, ly + 5), 4, 4)
            p.setPen(QColor(180, 188, 200))
            names = {"first_blood": "交火", "kill": "歼灭", "rout": "溃逃",
                     "rally": "重整", "escape": "溃散"}
            p.drawText(int(lx + 12), int(ly + 9), names.get(kind, kind))
            lx += 52

        p.setPen(QPen(QColor(70, 82, 100), 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(plot)


class BattleReportDialog(QDialog):
    """军力折线 + 时间线大图；战斗中可暂停查看，结算时复用。

    ``end_actions`` 为 True 时显示「再战 / 返回」而非单纯确定，
    ``choice`` 结果为 ``"again"`` / ``"menu"`` / ``""``。
    """

    def __init__(self, world: World, parent: QWidget | None = None,
                 title: str = "战况记录", summary_html: str = "",
                 end_actions: bool = False, back_label: str = "主菜单"):
        super().__init__(parent)
        self.choice = ""
        self.setWindowTitle(title)
        self.resize(780, 560)
        self.setStyleSheet(
            "QDialog { background:#12151c; color:#d8dee8; }"
            "QLabel { color:#c5ccd8; }"
            "QListWidget { background:#1a1f2a; border:1px solid #2a3344;"
            "  border-radius:6px; color:#c8d0dc; }"
            "QPushButton { background:#2a3344; color:#e8eef6; padding:6px 14px;"
            "  border-radius:6px; }"
            "QPushButton:hover { background:#364258; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 14)
        lay.setSpacing(8)
        if summary_html:
            head = QLabel(summary_html)
            head.setWordWrap(True)
            head.setTextFormat(Qt.RichText)
            lay.addWidget(head)
        st = world.stats
        # 结束瞬间再采一笔，保证曲线落到最终战力
        world.record_force_sample(force=True)
        self.chart = ForceChart()
        self.chart.set_data(st.force_history, st.timeline)
        lay.addWidget(self.chart, 3)

        lay.addWidget(_section_header("时间线"))
        self.tl_list = QListWidget()
        self.tl_list.setMaximumHeight(140)
        for t, kind, text in st.timeline:
            mm, ss = int(t) // 60, int(t) % 60
            col, glyph = _TIMELINE_STYLE.get(kind, (QColor(180, 180, 180), "·"))
            item = QListWidgetItem(f"[{mm:02d}:{ss:02d}] [{glyph}] {text}")
            item.setForeground(col)
            self.tl_list.addItem(item)
        if not st.timeline:
            self.tl_list.addItem(QListWidgetItem("（尚无关键事件）"))
        self.tl_list.scrollToBottom()
        lay.addWidget(self.tl_list, 1)

        n = len(st.force_history)
        dur = st.force_history[-1][0] if st.force_history else 0.0
        foot = QLabel(
            f"采样 {n} 点 · 跨度 {int(dur)//60:02d}:{int(dur)%60:02d} · "
            f"蓝=我军有效战力　红=敌军　虚线刻度=关键事件")
        foot.setObjectName("subnote")
        lay.addWidget(foot)

        row = QHBoxLayout()
        row.addStretch(1)
        if end_actions:
            btn_again = QPushButton("再战一场 (F5)")
            btn_menu = QPushButton(f"返回{back_label}")
            btn_again.clicked.connect(lambda: self._finish("again"))
            btn_menu.clicked.connect(lambda: self._finish("menu"))
            row.addWidget(btn_again)
            row.addWidget(btn_menu)
        else:
            btn_ok = QPushButton("继续")
            btn_ok.clicked.connect(self.accept)
            row.addWidget(btn_ok)
        lay.addLayout(row)

    def _finish(self, choice: str) -> None:
        self.choice = choice
        self.accept()


# ==================================================================== 侧栏
class SidePanel(QWidget):
    """侧栏；``open_report_requested`` 由 BattlePage 接住：暂停并弹战况图。"""

    open_report_requested = Signal()

    def __init__(self, world: World, parent: QWidget | None = None):
        super().__init__(parent)
        self.world = world
        self.setObjectName("sidepanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(304)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        layout.addWidget(self._header("军力对比"))
        self.strength_bar = StrengthBar()
        layout.addWidget(self.strength_bar)
        self.strength_note = QLabel()
        self.strength_note.setObjectName("subnote")
        layout.addWidget(self.strength_note)
        self.btn_report = QPushButton("战况记录 ▦")
        self.btn_report.setToolTip("暂停并查看军力折线与时间线（也可在结算时查看）")
        self.btn_report.clicked.connect(self.open_report_requested.emit)
        layout.addWidget(self.btn_report)

        layout.addWidget(self._header("战况统计"))
        self.stats_label = QLabel()
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)

        layout.addWidget(self._header("选中兵团"))
        self.unit_list = QListWidget()
        self.unit_list.setMinimumHeight(120)
        layout.addWidget(self.unit_list, 2)

        layout.addWidget(self._header("兵种 / 地形"))
        self.legend = QListWidget()
        self.legend.setMinimumHeight(120)
        self._fill_legend()
        layout.addWidget(self.legend, 2)

        layout.addWidget(self._header("战报"))
        self.log_list = QListWidget()
        layout.addWidget(self.log_list, 2)
        self._log_shown = 0

    def set_world(self, world: World) -> None:
        self.world = world
        self._log_shown = -1

    @staticmethod
    def _header(text: str) -> QLabel:
        return _section_header(text)

    def _fill_legend(self) -> None:
        # 兵种外形速记 + 克制关系
        shapes = {"infantry": "● 步兵　克骑兵，抗线中坚",
                  "cavalry": "▲ 骑兵　克弓/炮，冲锋+侧袭",
                  "archer": "◆ 弓兵　克步兵，远程风筝",
                  "artillery": "⬢ 炮兵　轰步兵，射程最远"}
        for key, label in shapes.items():
            self.legend.addItem(QListWidgetItem(label))
        self.legend.addItem(QListWidgetItem("士气跌破 25% 溃逃，脱战回稳可重整"))
        self.legend.addItem(QListWidgetItem("战功晋升：老练/精锐/禁卫（金纹角标）"))
        for terrain in Terrain:
            info = TERRAIN_INFO[terrain]
            pm = QPixmap(14, 14)
            pm.fill(QColor(*info.color))
            tag = "不可通行" if not info.passable else f"速{info.speed:.2f} 防{info.defense:.2f} 攻{info.attack:.2f}"
            item = QListWidgetItem(pm, f"{info.name}  {tag}")
            item.setToolTip(info.desc)
            self.legend.addItem(item)

    def refresh(self) -> None:
        w = self.world
        pu, eu = w.units_of(Faction.PLAYER), w.units_of(Faction.ENEMY)
        p, e = len(pu), len(eu)
        pr = sum(1 for u in pu if u.routing)
        er = sum(1 for u in eu if u.routing)
        p_tag = f"我军 {p} 团" + (f"（溃{pr}）" if pr else "")
        e_tag = f"敌军 {e} 团" + (f"（溃{er}）" if er else "")
        self.summary.setText(
            f"<b>时间</b> {int(w.elapsed)//60:02d}:{int(w.elapsed)%60:02d}　"
            f"<b>难度</b> {w.params.name}<br>"
            f"<span style='color:#48a0f0'>{p_tag}</span>　"
            f"<span style='color:#e05548'>{e_tag}</span>　"
            f"<span style='color:#9aa7b8'>种子 {w.map.seed}</span>")

        ps = army_strength(pu)
        es = army_strength(eu)
        self.strength_bar.set_values(ps, es)
        if ps >= es * 1.5:
            verdict = "⚔ 我军兵锋压倒"
        elif ps >= es * 1.15:
            verdict = "我军略占上风"
        elif es >= ps * 1.5:
            verdict = "⚠ 敌军兵锋压倒"
        elif es >= ps * 1.15:
            verdict = "敌军略占上风"
        else:
            verdict = "势均力敌"
        self.strength_note.setText(
            f"按存活兵力·老练度折算，溃兵折三成　<b>{verdict}</b>")

        sp = w.stats.side[Faction.PLAYER]
        se = w.stats.side[Faction.ENEMY]
        self.stats_label.setText(
            f"<span style='color:#48a0f0'>我军</span>　击破 {sp.kills} / 损失 {sp.losses}　"
            f"输出 {sp.dmg_dealt:.0f}<br>"
            f"<span style='color:#e05548'>敌军</span>　击破 {se.kills} / 损失 {se.losses}　"
            f"输出 {se.dmg_dealt:.0f}")

        self.unit_list.clear()
        # 多选指定同一目标时显示集火摘要
        focus_uids = [u.target_uid for u in w.selected
                      if u.order == "attack" and u.target_uid is not None]
        if len(focus_uids) >= 2 and len(set(focus_uids)) == 1:
            ft = w.unit_by_uid(focus_uids[0])
            if ft is not None:
                self.unit_list.addItem(
                    QListWidgetItem(
                        f"◎ {len(focus_uids)} 团集火 → {ft.spec.name}#{ft.uid}"))
        for u in w.selected:
            info = w.map.info_at(u.x, u.y)
            extra = "  行军中" if u.moving else (
                f"  据守+{(u.entrench-1)*100:.0f}%" if u.entrench > 1.01 else "")
            if u.vet_level > 0:
                extra += f"  [{u.vet_name}]"
            if u.order == "attack":
                extra += "  [攻击]"
            elif u.order == "attack_move":
                extra += "  [攻移]"
            elif u.order == "patrol":
                extra += "  [巡逻]"
            if u.retreating and u.path:
                extra += "  [撤退]"
            if u.order_queue:
                extra += f"  [队列×{len(u.order_queue)}]"
            if u.formation_lock:
                extra += "  [编队]"
            if u.auto:
                extra += "  [托管]"
            if u.kills:
                extra += f"  破{u.kills}团"
            item = QListWidgetItem(
                f"{UNIT_GLYPH[u.type_key]} {u.spec.name} #{u.uid}"
                f"  HP {u.hp:.0f}/{u.max_hp:.0f}"
                f"  士气{u.morale*100:.0f}%  {info.name}{extra}")
            # 状态着色：残血/动摇偏红警示，功勋部队金色
            if u.hp_ratio < 0.3 or u.morale < 0.4:
                item.setForeground(QColor(235, 130, 110))
            elif u.vet_level > 0:
                item.setForeground(QColor(232, 205, 130))
            self.unit_list.addItem(item)

        # 用累计计数判断有无新战报：events 截断保留末 200 条后长度不再变化
        if w.event_seq != self._log_shown:
            self.log_list.clear()
            for line in w.events[-60:]:
                it = QListWidgetItem(line)
                it.setForeground(self._event_color(line))
                self.log_list.addItem(it)
            self.log_list.scrollToBottom()
            self._log_shown = w.event_seq

    @staticmethod
    def _event_color(line: str) -> QColor:
        """战报按事件类型着色，一眼分清噩耗与捷报。"""
        if any(k in line for k in ("溃逃", "溃散", "覆灭", "败")):
            return QColor(230, 120, 105)
        if "重整" in line:
            return QColor(126, 214, 162)
        if any(k in line for k in ("晋升", "老练", "精锐", "禁卫")):
            return QColor(232, 198, 106)
        return QColor(154, 167, 184)


# ==================================================================== 战斗页
class BattlePage(QWidget):
    exit_to_menu = Signal()

    def __init__(self, difficulty: Difficulty, seed: int | None, parent: QWidget | None = None,
                 world: World | None = None, back_label: str = "主菜单"):
        super().__init__(parent)
        self.difficulty = difficulty
        self.world = world if world is not None else World(seed=seed, difficulty=difficulty)
        # 试玩/自定义剧本：保留开战时的快照，F5 再战从剧本重置而非残局
        self._scenario = self.world.to_dict() if world is not None else None
        self.back_label = back_label
        self.view = MapView(self.world)
        self.paused = False
        self.speed = 1.0
        self._ended = False
        # 控制编组 1..9：槽号 → uid 列表（死亡后召回时过滤）
        self.control_groups: dict[int, list[int]] = {}
        self._last_group_key: int | None = None
        self._last_group_t: float = 0.0

        self.panel = SidePanel(self.world)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(0)
        left.addWidget(self.view, 1)
        left.addWidget(self._build_toolbar())
        lw = QWidget(); lw.setLayout(left)
        content.addWidget(lw, 1)
        content.addWidget(self.panel)

        self.status = QLabel(
            "左键选择　·　右键点敌＝攻击　·　拖拽＝画笔　·　Shift+右键＝排队")
        self.status.setObjectName("statusline")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(content, 1)
        outer.addWidget(self.status)

        self.view.hover_changed.connect(self.status.setText)
        self.view.selection_changed.connect(self.panel.refresh)
        self.panel.open_report_requested.connect(self.show_battle_report)

        self._build_shortcuts()
        self.view.center_on_selection()
        self.panel.refresh()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    # ------------------------------------------------------------ UI
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("toolbar")
        # 不把按钮总宽度传导为窗口最小宽度：小屏/高缩放下窗口仍可正常打开，
        # 空间不足时优先压缩右侧提示文字，其次均匀压缩按钮
        bar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(5)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setToolTip("暂停 / 继续（空格）")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_speed = QPushButton("速度 ×1")
        self.btn_speed.clicked.connect(self.cycle_speed)
        btn_all = QPushButton("全选")
        btn_all.setToolTip("全选我军（Ctrl+A）")
        btn_all.clicked.connect(self.select_all)
        btn_stop = QPushButton("停止 (H)")
        btn_stop.setToolTip("停止选中：清空路线与攻击锁定")
        btn_stop.clicked.connect(self.stop_selected)
        btn_retreat = QPushButton("撤退 (R)")
        btn_retreat.setToolTip("战术撤退：向出生侧短撤，途中不主动接敌")
        btn_retreat.clicked.connect(self.retreat_selected)
        # 指令模式：移动 / 攻击移动 / 巡逻（互斥），决定右键下达的指令类型
        self.order_group = QButtonGroup(self)
        self.order_btns: dict[str, QPushButton] = {}
        for mode, label, tip in (
                ("move", "移动 (M)",
                 "右键点地/拖拽＝移动（接敌即停）\n右键点可见敌军＝指定攻击（锁定追击）"),
                ("attack_move", "攻移 (A)",
                 "右键点地/拖拽＝攻击移动（灭敌后续程）\n右键点可见敌军＝指定攻击（锁定追击）"),
                ("patrol", "巡逻 (P)",
                 "右键点地/拖拽＝巡逻往返\n右键点可见敌军＝指定攻击（锁定追击）")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(tip)
            b.setChecked(mode == "move")
            b.clicked.connect(lambda _=False, m=mode: self._set_order_mode(m))
            self.order_group.addButton(b)
            self.order_btns[mode] = b
        self.btn_formation = QPushButton("编队 (Q)")
        self.btn_formation.setCheckable(True)
        self.btn_formation.setToolTip("编队锁定：选中兵团以最慢者速度同步推进、保持队形不脱节。\n"
                                      "再次点击切换开关；下令后保持，直到切换或停止。")
        self.btn_formation.clicked.connect(self._toggle_formation)
        self.btn_auto = QPushButton("托管 (J)")
        self.btn_auto.setToolTip("把选中兵团交给 AI 指挥：协同推进、远程后置、残血撤退。\n"
                                 "再点一次或手动下令即解除。")
        self.btn_auto.clicked.connect(self.toggle_auto)
        btn_fog = QPushButton("战雾 (F)")
        btn_fog.setToolTip("战争迷雾：我军视野之外的敌军不可见。\n"
                           "森林里的敌人要贴近才能发现。")
        btn_fog.clicked.connect(self.view.toggle_fog)
        btn_terr = QPushButton("势力 (T)")
        btn_terr.setToolTip("势力（占领区）图层开关")
        btn_terr.clicked.connect(self.view.toggle_territory)
        btn_grid = QPushButton("网格 (G)")
        btn_grid.clicked.connect(self.view.toggle_grid)
        btn_new = QPushButton("新战场")
        btn_new.setToolTip("新战场（F5）：同难度、随机新地图")
        btn_new.clicked.connect(self.new_battle)
        btn_menu = QPushButton(f"{self.back_label}")
        btn_menu.setToolTip(f"返回{self.back_label}（Esc）")
        btn_menu.clicked.connect(self._back_to_menu)
        # 按功能分组：时间｜选择｜指令｜显示图层｜局面
        groups = ((self.btn_pause, self.btn_speed),
                  (btn_all, btn_stop, btn_retreat),
                  (self.order_btns["move"], self.order_btns["attack_move"],
                   self.order_btns["patrol"], self.btn_formation, self.btn_auto),
                  (btn_fog, btn_terr, btn_grid),
                  (btn_new, btn_menu))
        for i, grp in enumerate(groups):
            if i:
                lay.addWidget(_vseparator())
            for b in grp:
                lay.addWidget(b)
        lay.addStretch(1)
        hint = QLabel("Shift+右键排队　R撤退　Ctrl+1-9编组")
        hint.setStyleSheet("color:#8d99ab;")
        # 窗口不够宽时允许提示被压缩截断，不把最小宽度顶到工具栏上
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(hint, 1)
        return bar

    def _build_shortcuts(self) -> None:
        def act(seq: str, fn):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            a.triggered.connect(fn)
            self.addAction(a)

        act("Space", self.toggle_pause)
        act("Ctrl+A", self.select_all)
        act("H", self.stop_selected)
        act("R", self.retreat_selected)
        act("J", self.toggle_auto)
        act("M", lambda: self._set_order_mode("move"))
        act("A", lambda: self._set_order_mode("attack_move"))
        act("P", lambda: self._set_order_mode("patrol"))
        # 经按钮 click() 走与鼠标一致的路径：先翻转 checked 再进 _toggle_formation
        act("Q", self.btn_formation.click)
        act("F", self.view.toggle_fog)
        act("T", self.view.toggle_territory)
        act("G", self.view.toggle_grid)
        act("F5", self.new_battle)
        act("Tab", self.view.center_on_selection)
        act("Esc", self._back_to_menu)
        # 控制编组：Ctrl+1..9 存选中；1..9 召回（连按居中）
        for n in range(1, 10):
            act(f"Ctrl+{n}", lambda checked=False, k=n: self._save_control_group(k))
            act(f"{n}", lambda checked=False, k=n: self._recall_control_group(k))

    # ------------------------------------------------------------ 操作
    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.btn_pause.setText("继续" if self.paused else "暂停")

    def cycle_speed(self) -> None:
        self.speed = {1.0: 2.0, 2.0: 4.0, 4.0: 1.0}[self.speed]
        self.btn_speed.setText(f"速度 ×{int(self.speed)}")

    def select_all(self) -> None:
        for u in self.world.units_of(Faction.PLAYER):
            if not u.routing:               # 溃兵不受指挥，也不应进入选中
                u.selected = True
        self.panel.refresh()
        self.view.update()

    def stop_selected(self) -> None:
        self.world.stop(self.world.selected)
        self.view.update()

    def retreat_selected(self) -> None:
        """战术撤退：选中向出生侧短撤。"""
        sel = [u for u in self.world.selected if not u.routing]
        if not sel:
            self.status.setText("请先选中要撤退的兵团")
            return
        self.world.issue_retreat(sel)
        self.status.setText(f"撤退 {len(sel)} 个兵团 → 出生侧")
        self.panel.refresh()
        self.view.update()

    def toggle_auto(self) -> None:
        """托管选中兵团；若选中的已全部处于托管则解除。"""
        sel = [u for u in self.world.selected if not u.routing]
        if not sel:
            self.status.setText("请先选中要托管的兵团（左键点选 / 拖拽框选）")
            return
        enable = not all(u.auto for u in sel)
        self.world.set_auto(sel, enable)
        self.panel.refresh()
        self.view.update()

    # 指令模式 / 编队切换（工具栏按钮与快捷键共用）
    _ORDER_NAMES = {"move": "移动", "attack_move": "攻击移动", "patrol": "巡逻"}

    def _set_order_mode(self, mode: str) -> None:
        """切换右键下达的指令类型：移动 / 攻击移动 / 巡逻。"""
        self.view.order_mode = mode
        self.order_btns[mode].setChecked(True)
        fm = "·编队" if self.view.formation_lock else ""
        self.status.setText(
            f"指令模式：{self._ORDER_NAMES[mode]}{fm}　"
            f"点敌＝锁定　点地/拖拽＝{self._ORDER_NAMES[mode]}")
        self.view.update()

    def _toggle_formation(self) -> None:
        """开关编队锁定：下令时选中兵团以最慢者速度同步推进、保持队形。"""
        self.view.formation_lock = self.btn_formation.isChecked()
        fm = "·编队" if self.view.formation_lock else "·解除编队"
        mode = self.view.order_mode
        self.status.setText(
            f"指令模式：{self._ORDER_NAMES[mode]}{fm}　"
            f"点敌＝锁定　点地/拖拽＝{self._ORDER_NAMES[mode]}")
        self.view.update()

    def _save_control_group(self, n: int) -> None:
        """Ctrl+N：把当前选中（存活非溃逃我军）存入编组 N；空选中则清空该槽。"""
        uids = [u.uid for u in self.world.selected
                if u.alive and not u.routing and u.faction is Faction.PLAYER]
        if uids:
            self.control_groups[n] = uids
            self.status.setText(f"编组 {n} ← {len(uids)} 个兵团")
        else:
            self.control_groups.pop(n, None)
            self.status.setText(f"编组 {n} 已清空")
        self.view.update()

    def _recall_control_group(self, n: int) -> None:
        """N：召回编组；约 0.35s 内再按同一键则视角居中。"""
        now = time.monotonic()
        double = (self._last_group_key == n and now - self._last_group_t < 0.35)
        self._last_group_key = n
        self._last_group_t = now

        uids = self.control_groups.get(n, [])
        live = []
        for uid in uids:
            u = self.world.unit_by_uid(uid)
            if (u is not None and u.alive and not u.routing
                    and u.faction is Faction.PLAYER):
                live.append(u)
        if live:
            self.control_groups[n] = [u.uid for u in live]
        elif n in self.control_groups:
            self.control_groups.pop(n, None)

        if not live:
            self.status.setText(f"编组 {n} 为空")
            return
        self.world.clear_selection()
        for u in live:
            u.selected = True
        self.panel.refresh()
        self.status.setText(f"编组 {n} · {len(live)} 个兵团")
        if double:
            self.view.center_on_selection()
        self.view.update()

    def new_battle(self) -> None:
        # 试玩/自定义剧本：从开战快照重置；普通局则随机新图
        if self._scenario is not None:
            self.world = World.from_dict(self._scenario)
        else:
            self.world = World(difficulty=self.difficulty)
        self.view.world = self.world
        self.view._attack_pings.clear()
        self.control_groups.clear()
        self._last_group_key = None
        self.panel.set_world(self.world)
        self.view.invalidate_terrain()
        self.view.center_on_selection()
        self.panel.refresh()
        self._ended = False
        self.status.setText(f"新战场（{self.world.params.name}·种子 {self.world.map.seed}）")

    def _back_to_menu(self) -> None:
        self.timer.stop()
        self.exit_to_menu.emit()

    def stop(self) -> None:
        self.timer.stop()

    # ------------------------------------------------------------ 主循环
    def _tick(self) -> None:
        if self._ended:
            return                      # 结算弹窗期间不再空转刷新
        base = TICK_MS / 1000.0
        # UI 特效（攻击红圈）暂停时也按墙钟衰减，避免冻在屏幕上
        self.view.tick_fx(base)
        if not self.paused:
            # 倍速用单次较大 dt，避免 ×4 时完整模拟跑 4 遍（AI/A* 成本线性放大）
            self.world.update(base * self.speed)
            winner = self.world.winner()
            if winner is not None:
                self._ended = True
                self.panel.refresh()
                self.view.update()
                self._show_result(winner)
                return
            if self.world.is_draw():
                self._ended = True
                self.panel.refresh()
                self.view.update()
                self._show_draw()
                return
        self.panel.refresh()
        self.view.update()

    def show_battle_report(self, *, summary_html: str = "",
                           title: str = "战况记录",
                           pause: bool = True,
                           end_actions: bool = False) -> str:
        """弹出军力折线 + 时间线；默认暂停。返回结算 choice：again/menu/空。"""
        if pause and not self.paused and not self._ended:
            self.toggle_pause()
        dlg = BattleReportDialog(
            self.world, self, title=title, summary_html=summary_html,
            end_actions=end_actions, back_label=self.back_label)
        dlg.exec()
        return dlg.choice

    def _show_result(self, winner: Faction) -> None:
        who = "胜利！" if winner is Faction.PLAYER else "败北…"
        head = (f"<h3>{FACTION_NAME[winner]}取得胜利——你{who}</h3>"
                + self.world.battle_summary(winner))
        choice = self.show_battle_report(
            summary_html=head, title="战斗结束", pause=False, end_actions=True)
        if choice == "again":
            self.new_battle()
        else:
            self._back_to_menu()

    def _show_draw(self) -> None:
        head = "<h3>两败俱伤——平局</h3>" + self.world.battle_summary(Faction.PLAYER)
        choice = self.show_battle_report(
            summary_html=head, title="战斗结束 · 平局",
            pause=False, end_actions=True)
        if choice == "again":
            self.new_battle()
        else:
            self._back_to_menu()


# ==================================================================== 地图编辑器页
class EditorPage(QWidget):
    """地图 / 剧本编辑器：画地形、摆双方兵团，可保存/载入、一键试玩。"""

    exit_to_menu = Signal()
    playtest_requested = Signal(object)     # 传出一份克隆 World 供试玩

    def __init__(self, world: World, parent: QWidget | None = None):
        super().__init__(parent)
        self.world = world
        self.map_editor = MapEditor(world)

        self.view = MapView(world)
        self.view.edit_mode = True
        self.view.editor = self.map_editor
        self.view._show_territory = False    # 编辑时先看地形与兵，势力层默认关

        self.status = QLabel("左键画地形／摆兵，右键轻点擦除、右键拖拽平移　·　中键/方向键平移　·　滚轮缩放")
        self.status.setObjectName("statusline")

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        content.addWidget(self.view, 1)
        content.addWidget(self._build_panel())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(content, 1)
        outer.addWidget(self.status)

        self.view.hover_changed.connect(self.status.setText)
        self.view.edited.connect(self._refresh_info)
        self.view.center_on(world.map.pixel_width / 2, world.map.pixel_height / 2)
        self._refresh_info()

        # 快捷键：T 开关势力层（与面板按钮一致）
        a = QAction(self)
        a.setShortcut(QKeySequence("T"))
        a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        a.triggered.connect(self._toggle_territory)
        self.addAction(a)

    # ------------------------------------------------------------ 面板搭建
    def _build_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(304)
        root = QVBoxLayout(panel)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.info = QLabel()
        self.info.setWordWrap(True)
        root.addWidget(self.info)

        # —— 工具 ——
        root.addWidget(self._header("工具"))
        self.tool_group = QButtonGroup(self)
        self.tool_btns: dict[str, QPushButton] = {}
        trow = QHBoxLayout()
        for tool, label in ((TOOL_TERRAIN, "地形"), (TOOL_UNIT, "兵团"), (TOOL_ERASE, "擦除")):
            b = self._toggle(label)
            b.setChecked(tool == self.map_editor.tool)
            b.clicked.connect(lambda _=False, t=tool: self._set_tool(t))
            self.tool_group.addButton(b)
            self.tool_btns[tool] = b
            trow.addWidget(b)
        root.addLayout(trow)

        # —— 地形 ——
        root.addWidget(self._header("地形（画笔）"))
        self.terr_group = QButtonGroup(self)
        self.terr_btns: dict[Terrain, QPushButton] = {}
        grid = QGridLayout()
        grid.setSpacing(4)
        for i, terrain in enumerate(Terrain):
            info = TERRAIN_INFO[terrain]
            b = self._toggle(info.name)
            b.setIcon(QIcon(self._swatch(info.color)))
            b.setChecked(terrain == self.map_editor.terrain)
            b.setToolTip(info.desc)
            b.clicked.connect(lambda _=False, t=terrain: self._pick_terrain(t))
            self.terr_group.addButton(b)
            self.terr_btns[terrain] = b
            grid.addWidget(b, i // 2, i % 2)
        root.addLayout(grid)

        # —— 笔刷大小 ——
        root.addWidget(self._header("笔刷大小"))
        self.brush_group = QButtonGroup(self)
        brow = QHBoxLayout()
        for r in BRUSH_RADII:
            n = 2 * r + 1
            b = self._toggle(f"{n}×{n}")
            b.setChecked(r == self.map_editor.brush)
            b.clicked.connect(lambda _=False, rr=r: self._pick_brush(rr))
            self.brush_group.addButton(b)
            brow.addWidget(b)
        root.addLayout(brow)

        # —— 兵团 ——
        root.addWidget(self._header("兵团（摆放）"))
        self.fac_group = QButtonGroup(self)
        self.fac_btns: dict[Faction, QPushButton] = {}
        frow = QHBoxLayout()
        checked_qss = {
            Faction.PLAYER: "QPushButton:checked{background:#2b5c94;border-color:#4a90e2;color:#fff;}",
            Faction.ENEMY: "QPushButton:checked{background:#8f2f2b;border-color:#d64c44;color:#fff;}",
        }
        for fac in (Faction.PLAYER, Faction.ENEMY):
            b = self._toggle(FACTION_NAME[fac])
            b.setStyleSheet(checked_qss[fac])
            b.setChecked(fac == self.map_editor.faction)
            b.clicked.connect(lambda _=False, f=fac: self._pick_faction(f))
            self.fac_group.addButton(b)
            self.fac_btns[fac] = b
            frow.addWidget(b)
        root.addLayout(frow)

        self.unit_group = QButtonGroup(self)
        self.unit_btns: dict[str, QPushButton] = {}
        ugrid = QGridLayout()
        ugrid.setSpacing(4)
        for i, (key, spec) in enumerate(UNIT_TYPES.items()):
            b = self._toggle(f"{UNIT_GLYPH[key]} {spec.name}")
            b.setChecked(key == self.map_editor.unit_type)
            b.clicked.connect(lambda _=False, k=key: self._pick_unit(k))
            self.unit_group.addButton(b)
            self.unit_btns[key] = b
            ugrid.addWidget(b, i // 2, i % 2)
        root.addLayout(ugrid)

        # —— 整图 / 剧本 ——
        root.addWidget(self._header("地图 / 剧本"))
        r1 = QHBoxLayout()
        b_fill = QPushButton("清空为平原"); b_fill.clicked.connect(self._fill_plain)
        b_rand = QPushButton("随机地形"); b_rand.clicked.connect(self._random_terrain)
        r1.addWidget(b_fill); r1.addWidget(b_rand)
        root.addLayout(r1)
        r2 = QHBoxLayout()
        b_clear = QPushButton("清空兵团"); b_clear.clicked.connect(self._clear_units)
        b_terr = QPushButton("势力层 (T)"); b_terr.clicked.connect(self._toggle_territory)
        r2.addWidget(b_clear); r2.addWidget(b_terr)
        root.addLayout(r2)
        r3 = QHBoxLayout()
        b_save = QPushButton("保存剧本"); b_save.clicked.connect(self._save)
        b_load = QPushButton("载入剧本"); b_load.clicked.connect(self._load)
        r3.addWidget(b_save); r3.addWidget(b_load)
        root.addLayout(r3)

        root.addStretch(1)

        b_play = QPushButton("▶  试玩这张地图")
        b_play.setObjectName("startbtn")
        b_play.setMinimumHeight(42)
        b_play.clicked.connect(self._playtest)
        root.addWidget(b_play)
        b_menu = QPushButton("返回主菜单")
        b_menu.clicked.connect(self.exit_to_menu.emit)
        root.addWidget(b_menu)

        # 面板可能较高，套一层滚动区避免小窗口下被截断
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(322)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(panel)
        return scroll

    @staticmethod
    def _header(text: str) -> QLabel:
        return _section_header(text)

    @staticmethod
    def _toggle(text: str) -> QPushButton:
        b = QPushButton(text)
        b.setCheckable(True)
        return b

    @staticmethod
    def _swatch(color: tuple[int, int, int]) -> QPixmap:
        pm = QPixmap(14, 14)
        pm.fill(QColor(*color))
        return pm

    # ------------------------------------------------------------ 工具切换
    def _set_tool(self, tool: str) -> None:
        self.map_editor.tool = tool
        self.tool_btns[tool].setChecked(True)
        self.view.update()

    def _pick_terrain(self, terrain: Terrain) -> None:
        self.map_editor.terrain = terrain
        self._set_tool(TOOL_TERRAIN)          # 选地形即切到地形笔刷

    def _pick_brush(self, radius: int) -> None:
        self.map_editor.brush = radius
        self.view.update()

    def _pick_faction(self, faction: Faction) -> None:
        self.map_editor.faction = faction
        self.view.update()

    def _pick_unit(self, key: str) -> None:
        self.map_editor.unit_type = key
        self._set_tool(TOOL_UNIT)             # 选兵种即切到摆放

    # ------------------------------------------------------------ 整图 / 剧本操作
    def _fill_plain(self) -> None:
        self.map_editor.fill(Terrain.PLAIN)
        self.view.invalidate_terrain()
        self._refresh_info()

    def _random_terrain(self) -> None:
        self.map_editor.regenerate()
        self.view.invalidate_terrain()
        self._refresh_info()

    def _clear_units(self) -> None:
        self.map_editor.clear_units()
        self.view.update()
        self._refresh_info()

    def _toggle_territory(self) -> None:
        self.view.toggle_territory()

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存剧本", "scenario.json", "剧本 (*.json)")
        if not path:
            return
        try:
            save_world(self.world, path)
            self.status.setText(f"已保存剧本：{path}")
        except OSError as ex:
            QMessageBox.warning(self, "保存失败", str(ex))

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "载入剧本", "", "剧本 (*.json)")
        if not path:
            return
        try:
            new_world = load_world(path)
        except (OSError, ValueError, KeyError) as ex:
            QMessageBox.warning(self, "载入失败", f"无法读取剧本：\n{ex}")
            return
        self._swap_world(new_world)
        self.status.setText(f"已载入剧本：{path}")

    def _swap_world(self, world: World) -> None:
        self.world = world
        self.map_editor.world = world
        self.view.world = world
        self.view.invalidate_terrain()
        self.view.center_on(world.map.pixel_width / 2, world.map.pixel_height / 2)
        self._refresh_info()
        self.view.update()

    def _playtest(self) -> None:
        p, e = self.map_editor.counts()
        if p == 0 or e == 0:
            QMessageBox.warning(self, "无法试玩",
                                "请至少各放置 1 个我军与 1 个敌军兵团，双方都在场才能开打。")
            return
        self.playtest_requested.emit(self.world.clone())

    def _refresh_info(self) -> None:
        p, e = self.map_editor.counts()
        self.info.setText(
            f"<b>地图编辑器</b>　{self.world.map.width}×{self.world.map.height} 格<br>"
            f"<span style='color:#48a0f0'>我军 {p} 团</span>　"
            f"<span style='color:#e05548'>敌军 {e} 团</span>　"
            f"<span style='color:#9aa7b8'>种子 {self.world.map.seed}</span><br>"
            f"<span style='color:#8d99ab'>难度 {self.world.params.name}（试玩时敌军按此强度）</span>")


# ==================================================================== 开始界面
class StartScreen(QWidget):
    start_requested = Signal(object, object)   # (Difficulty, seed:int|None)
    editor_requested = Signal(object, object)  # (Difficulty, seed:int|None) → 打开编辑器

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.difficulty = Difficulty.EASY
        self._bg_src: QImage | None = None      # 随机战场的俯瞰图（原始分辨率）
        self._bg_scaled: QPixmap | None = None  # 按窗口尺寸缩放后的缓存

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addStretch(1)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(560)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(36, 30, 36, 30)
        cl.setSpacing(14)

        title = QLabel("笔锋 · 实时战略")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        cl.addWidget(title)
        sub = QLabel("选中兵团，在地图上挥笔画出前进路线，指挥你的军团。")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet("color:#9fb0c4;")
        cl.addWidget(sub)

        divider = QFrame()
        divider.setFixedHeight(2)
        divider.setStyleSheet(
            "border:none; background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 transparent, stop:0.5 #c9a54e, stop:1 transparent);")
        cl.addWidget(divider)

        cl.addSpacing(6)
        cl.addWidget(self._label("选择难度"))
        diff_row = QHBoxLayout()
        self.diff_group = QButtonGroup(self)
        self.diff_group.setExclusive(True)
        for d in (Difficulty.EASY, Difficulty.HARD):
            btn = QPushButton(DIFFICULTIES[d].name)
            btn.setCheckable(True)
            btn.setMinimumHeight(40)
            btn.setObjectName("diffbtn")
            btn.setChecked(d is self.difficulty)
            btn.clicked.connect(lambda _=False, dd=d: self._pick(dd))
            self.diff_group.addButton(btn, int(d))
            diff_row.addWidget(btn)
        cl.addLayout(diff_row)
        self.diff_desc = QLabel(DIFFICULTIES[self.difficulty].desc)
        self.diff_desc.setWordWrap(True)
        self.diff_desc.setStyleSheet("color:#9fb0c4;")
        cl.addWidget(self.diff_desc)

        cl.addSpacing(6)
        cl.addWidget(self._label("地图种子（留空＝随机）"))
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("输入整数，或留空随机生成")
        self.seed_edit.returnPressed.connect(self._start)
        cl.addWidget(self.seed_edit)

        cl.addSpacing(10)
        start = QPushButton("开始战斗")
        start.setObjectName("startbtn")
        start.setMinimumHeight(46)
        start.clicked.connect(self._start)
        cl.addWidget(start)

        edit_btn = QPushButton("地图编辑器")
        edit_btn.setObjectName("editorbtn")
        edit_btn.setMinimumHeight(40)
        edit_btn.clicked.connect(self._start_editor)
        cl.addWidget(edit_btn)

        guide = QLabel(
            "操作　左键点选／拖拽框选　·　右键拖拽＝画笔行军　·　右键单击＝移动\n"
            "　　　中键或方向键平移　·　滚轮缩放　·　空格暂停　·　T 势力图　·　F 战雾\n"
            "战场　兵种相克、侧袭背击、高地与据守；士气崩溃会溃逃，脱战可重整\n"
            "地图编辑器　画地形、摆双方兵团，可存/读档并一键试玩")
        guide.setAlignment(Qt.AlignCenter)
        guide.setStyleSheet("color:#79879b; font-size:11px;")
        cl.addWidget(guide)

        wrap = QHBoxLayout()
        wrap.addStretch(1); wrap.addWidget(card); wrap.addStretch(1)
        root.addLayout(wrap)
        root.addStretch(1)

    @staticmethod
    def _label(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet("font-weight:bold; color:#d8e2f0;")
        return lb

    # ------------------------------------------------------------ 沉浸背景
    def _bg_image(self) -> QImage:
        """随机生成一张战场俯瞰图当背景：压暗、微偏蓝，战前先「看见」战场。"""
        if self._bg_src is None:
            gm = GameMap(120, 90)
            img = QImage(gm.width, gm.height, QImage.Format_RGB32)
            for y in range(gm.height):
                row = gm.tiles[y]
                for x in range(gm.width):
                    r, g, b = TERRAIN_INFO[Terrain(row[x])].color
                    s = gm.shade_at(x, y) * 0.52
                    img.setPixel(x, y, (min(255, int(r * s)) << 16)
                                 | (min(255, int(g * s)) << 8)
                                 | min(255, int(b * s) + 10))
            self._bg_src = img
        return self._bg_src

    def paintEvent(self, _evt) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        if (self._bg_scaled is None or self._bg_scaled.width() < w
                or self._bg_scaled.height() < h):
            self._bg_scaled = QPixmap.fromImage(self._bg_image().scaled(
                w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
        pm = self._bg_scaled
        p.drawPixmap((w - pm.width()) // 2, (h - pm.height()) // 2, pm)
        # 暗角把视线收拢到中央卡片
        g = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.75)
        g.setColorAt(0.0, QColor(6, 8, 13, 40))
        g.setColorAt(1.0, QColor(4, 6, 10, 205))
        p.fillRect(0, 0, w, h, QBrush(g))

    def _pick(self, d: Difficulty) -> None:
        self.difficulty = d
        self.diff_desc.setText(DIFFICULTIES[d].desc)
        self.diff_group.button(int(d)).setChecked(True)

    def _parse_seed(self) -> int | None:
        text = self.seed_edit.text().strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            # 文字种子需跨进程可复现：hash() 受 PYTHONHASHSEED 随机化，改用 CRC32
            return zlib.crc32(text.encode("utf-8")) % (1 << 30)

    def _start(self) -> None:
        self.start_requested.emit(self.difficulty, self._parse_seed())

    def _start_editor(self) -> None:
        self.editor_requested.emit(self.difficulty, self._parse_seed())


# ==================================================================== 外壳
class GameShell(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("笔锋 · 实时战略")
        self.resize(1380, 880)
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.start = StartScreen()
        self.start.start_requested.connect(self._start_game)
        self.start.editor_requested.connect(self._open_editor)
        self.stack.addWidget(self.start)

        self.battle: BattlePage | None = None
        self.editor: EditorPage | None = None
        self.stack.setCurrentWidget(self.start)

    # ------------------------------------------------------------ 拆除
    def _destroy_battle(self) -> None:
        if self.battle is not None:
            self.battle.stop()
            self.stack.removeWidget(self.battle)
            self.battle.deleteLater()
            self.battle = None

    def _destroy_editor(self) -> None:
        if self.editor is not None:
            self.stack.removeWidget(self.editor)
            self.editor.deleteLater()
            self.editor = None

    # ------------------------------------------------------------ 战斗
    def _start_game(self, difficulty: Difficulty, seed) -> None:
        self._destroy_battle()
        self._destroy_editor()
        self.battle = BattlePage(difficulty, seed)
        self.battle.exit_to_menu.connect(self._to_menu)
        self.stack.addWidget(self.battle)
        self.stack.setCurrentWidget(self.battle)
        self.battle.view.setFocus()

    def _to_menu(self) -> None:
        self.stack.setCurrentWidget(self.start)
        self._destroy_battle()

    # ------------------------------------------------------------ 编辑器
    def _open_editor(self, difficulty: Difficulty, seed) -> None:
        self._destroy_battle()
        self._destroy_editor()
        world = World(seed=seed, difficulty=difficulty, populate=False)
        self.editor = EditorPage(world)
        self.editor.exit_to_menu.connect(self._editor_to_menu)
        self.editor.playtest_requested.connect(self._start_playtest)
        self.stack.addWidget(self.editor)
        self.stack.setCurrentWidget(self.editor)
        self.editor.view.setFocus()

    def _editor_to_menu(self) -> None:
        self.stack.setCurrentWidget(self.start)
        self._destroy_editor()

    def _start_playtest(self, world: World) -> None:
        # 编辑器留在栈中，试玩结束后原样回到编辑器继续调整
        self._destroy_battle()
        self.battle = BattlePage(world.difficulty, None, world=world, back_label="编辑器")
        self.battle.exit_to_menu.connect(self._playtest_to_editor)
        self.stack.addWidget(self.battle)
        self.stack.setCurrentWidget(self.battle)
        self.battle.view.setFocus()

    def _playtest_to_editor(self) -> None:
        self._destroy_battle()
        if self.editor is not None:
            self.stack.setCurrentWidget(self.editor)
            self.editor.view.setFocus()
        else:
            self.stack.setCurrentWidget(self.start)


DARK_QSS = """
/* —— 基底：夜战沙盘 —— */
QMainWindow, QWidget { background:#10131a; color:#cfd9e6;
    font-family:'Microsoft YaHei','Segoe UI'; font-size:12px; }
QLabel { background:transparent; }

/* 列表：内嵌暗槽，选中带阵营蓝 */
QListWidget { background:#151923; border:1px solid #272f3e; border-radius:8px;
    padding:3px; alternate-background-color:#181d28; }
QListWidget::item { padding:4px 7px; border-radius:5px; }
QListWidget::item:hover { background:#1f2634; }
QListWidget::item:selected { background:#26374f; color:#eaf1fb; }

QLineEdit { background:#151923; border:1px solid #333d50; border-radius:7px;
    padding:7px 11px; color:#e6edf5; selection-background-color:#2f6fb0; }
QLineEdit:focus { border:1px solid #5b9ce0; background:#171c27; }

/* 按钮：金属压边 + 渐变，checked 态染军蓝 */
QPushButton { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #262d3c, stop:1 #1e2431);
    border:1px solid #384256; border-radius:7px;
    padding:5px 12px; color:#d5dee9; }
QPushButton:hover { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #2f3849, stop:1 #252c3b); border-color:#4a5a78; color:#eef4fc; }
QPushButton:pressed { background:#181d28; }
QPushButton:checked { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #33689f, stop:1 #26507f);
    border:1px solid #5f9fe8; color:#ffffff; }
QPushButton:disabled { color:#5c6678; border-color:#2a3242; }

QToolTip { background:#1b202b; color:#dbe5f2; border:1px solid #3e4a63;
    padding:5px 8px; border-radius:4px; }

QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#2e3748; border-radius:4px; min-height:24px; }
QScrollBar::handle:vertical:hover { background:#415270; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar:horizontal { background:transparent; height:10px; margin:2px; }
QScrollBar::handle:horizontal { background:#2e3748; border-radius:4px; min-width:24px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }

QMessageBox { background:#171c26; }
QMessageBox QLabel { color:#dde6f2; }

/* 开始界面卡片：暗琥珀描边的「战书」 */
#card { background:rgba(13,17,25,0.92); border:1px solid #4a4634;
    border-radius:16px; }
#title { font-size:40px; font-weight:bold; color:#f0e6cc; letter-spacing:12px; }
#startbtn { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #3f86cc, stop:1 #2a5f9e);
    border:1px solid #6aa7ec; font-size:16px; font-weight:bold; color:#ffffff;
    letter-spacing:4px; }
#startbtn:hover { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #4f96dc, stop:1 #346cb0); }
#editorbtn { background:#222b39; border:1px solid #46617f; font-size:14px;
    font-weight:bold; color:#cfe0f2; letter-spacing:2px; }
#editorbtn:hover { background:#2c3a4e; }
#diffbtn { font-size:15px; font-weight:bold; }

/* 战斗页镶边 */
#toolbar { background:qlineargradient(x1:0,y1:0,x2:0,y2:1,
        stop:0 #181d27, stop:1 #141822);
    border-top:1px solid #2b3344; }
#toolbar QPushButton { padding:5px 8px; }
#toolsep { color:#2a3242; background:#2a3242; max-width:1px; border:none;
    margin:3px 6px; }
#statusline { background:#0d1016; color:#94a5bb; padding:5px 12px;
    border-top:1px solid #232a37; }
#sidepanel { background:#12161f; border-left:1px solid #262d3a; }
#sectionhdr { font-weight:bold; color:#e8d8a8; padding:3px 0 3px 9px;
    border-left:3px solid #c9a54e; margin-top:8px; letter-spacing:1px; }
#subnote { color:#8d99ab; padding:1px 2px; }
QStatusBar { background:#161a22; color:#9fb0c4; }
"""


def run() -> int:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(DARK_QSS)
    shell = GameShell()
    shell.show()
    return app.exec()
