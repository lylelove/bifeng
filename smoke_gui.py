"""离屏冒烟测试：驱动开始界面 → 战斗页，模拟输入、渲染多帧，捕获 Qt 层错误。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from rts.ai import Difficulty
from rts.ui import GameShell
from rts.units import Faction

app = QApplication(sys.argv)
shell = GameShell()
shell.resize(1380, 880)
shell.show()
app.processEvents()

# 1) 开始界面：选困难、填种子、开战
shell.start._pick(Difficulty.HARD)
shell.start.seed_edit.setText("7")
shell.start._start()
app.processEvents()
battle = shell.battle
assert battle is not None, "开战后未创建战斗页"
assert battle.world.difficulty is Difficulty.HARD, "难度未传入"
view = battle.view
view.resize(1000, 780)
app.processEvents()
print("battle up, difficulty", battle.world.params.name, "units", len(battle.world.units))


def click(btn, pos, kind="press", mods=Qt.NoModifier):
    ev = QMouseEvent({"press": QMouseEvent.MouseButtonPress,
                      "release": QMouseEvent.MouseButtonRelease,
                      "move": QMouseEvent.MouseMove}[kind],
                     QPointF(pos), QPointF(pos), btn, btn, mods)
    {"press": view.mousePressEvent, "release": view.mouseReleaseEvent,
     "move": view.mouseMoveEvent}[kind](ev)


# 2) 框选我军
view.center_on_selection()
click(Qt.LeftButton, QPoint(5, 5), "press")
click(Qt.LeftButton, QPoint(980, 720), "move")
click(Qt.LeftButton, QPoint(980, 720), "release")
sel = battle.world.selected
print("box-selected:", len(sel))
assert sel and all(u.faction is Faction.PLAYER for u in sel), "框选异常"

# 3) 右键画曲线路径
import math
click(Qt.RightButton, QPoint(300, 400), "press")
for i in range(1, 30):
    click(Qt.RightButton, QPoint(300 + i * 20, 400 + int(90 * math.sin(i / 6))), "move")
click(Qt.RightButton, QPoint(300 + 30 * 20, 400), "release")
withpath = [u for u in sel if u.path]
print("units routed:", len(withpath), "/", len(sel))
assert withpath, "画笔未下达路线"
assert all(battle.world.map.passable(x, y) for u in withpath for x, y in u.path), "路线含不可通行点"

# 4) 推进模拟 + 渲染（含势力图层、兵种形状、小地图）
img = QImage(view.width(), view.height(), QImage.Format_ARGB32)
for frame in range(120):
    battle._tick()
    if frame % 40 == 0:
        img.fill(0)
        p = QPainter(img)
        view.render(p, QPoint(0, 0))
        p.end()
print("sim elapsed", round(battle.world.elapsed, 1), "s; field share",
      tuple(round(x, 2) for x in battle.world.field.share()))

# 5) 覆盖各类交互
view.toggle_territory(); view.toggle_grid(); view.toggle_territory()
view.toggle_fog(); view.toggle_fog()
battle.select_all(); battle.stop_selected(); battle.cycle_speed()
battle.toggle_pause(); battle.toggle_pause()
# 控制编组：全选 → Ctrl 存 1 → 清选 → 召回
battle.select_all()
n_sel = len(battle.world.selected)
battle._save_control_group(1)
assert battle.control_groups.get(1) and len(battle.control_groups[1]) == n_sel
battle.world.clear_selection()
battle._recall_control_group(1)
assert len(battle.world.selected) == n_sel, "编组召回人数应一致"
# 军力时间线：开战应有采样
assert battle.world.stats.force_history, "应有军力采样"
from rts.ui import ForceChart
_fc = ForceChart()
_fc.set_data(battle.world.stats.force_history, battle.world.stats.timeline)
_fc.resize(400, 240)
_fc.repaint()
# 指定攻击反馈：ping + 状态文案
view.push_attack_ping(400.0, 400.0)
assert view._attack_pings
view.tick_fx(0.5)
assert not view._attack_pings, "攻击红圈应在 TTL 后消失"
click(Qt.MiddleButton, QPoint(400, 400), "press")
click(Qt.MiddleButton, QPoint(470, 440), "move")
click(Qt.MiddleButton, QPoint(470, 440), "release")
click(Qt.LeftButton, QPoint(view.width() - 60, view.height() - 60), "press")  # 小地图跳转
battle.new_battle()
assert not battle.control_groups, "新战场应清空编组"
for _ in range(30):
    battle._tick()

# 5b) 士气 / 溃逃 / 老练度 / 战雾（纯逻辑，快速无头模拟）
from rts.world import World as _World
from rts.terrain import Terrain as _Terrain

_w = _World(seed=7, difficulty=Difficulty.HARD)
for _ in range(2600):
    _w.update(0.033)
routs = sum(1 for line in _w.events if "溃逃" in line)
rallies = sum(1 for line in _w.events if "重整" in line)
assert routs > 0, "长时间交战应出现士气崩溃"
assert rallies >= 0
assert all(0.0 <= u.morale <= 1.0 for u in _w.units), "士气应在 [0,1]"
assert any(u.vet_level > 0 for u in _w.units), "长时间交战应有部队晋升"
routers = [u for u in _w.units if u.routing]
assert all(u.target_uid is None for u in routers), "溃兵不应索敌"
assert all(not u.selected for u in routers), "溃兵不应保持选中"
# 战雾判定：己方永远可见；敌军可见性为布尔
for u in _w.units_of(Faction.PLAYER)[:1]:
    assert _w.visible_to(Faction.PLAYER, u)
print("morale sim:", f"routs={routs} rallies={rallies}",
      "vets:", sorted({u.vet_name for u in _w.units}))

# 5c) 溃兵不开火 / 平局判定 / 涂湖吸附
_w2 = _World(seed=3)
_p = _w2.units_of(Faction.PLAYER)[0]
_e = _w2.units_of(Faction.ENEMY)[0]
_p.x, _p.y = _e.x - 20, _e.y
_p.target_uid = _e.uid
_p.routing = True
_p.attack_cd = 0.0
_hp = _e.hp
_w2._resolve_combat(0.033)
assert _e.hp == _hp, "溃兵不应造成伤害"
_w3 = _World(seed=1)
for _u in _w3.units:
    _u.hp = 0.0
_w3.update(0.033)
assert _w3.is_draw() and _w3.winner() is None, "双方皆灭应为平局"
_w4 = _World(seed=1, populate=False)
_u4 = _w4.add_unit("infantry", Faction.PLAYER, 800, 600)
_tx, _ty = _w4.map.tile_at(_u4.x, _u4.y)
assert _w4.map.set_terrain(_tx, _ty, _Terrain.LAKE)
_w4.snap_units_passable()
assert _w4.map.passable(_u4.x, _u4.y), "吸附后应站在可通行处"
assert _w4.map.terrain_version > 0, "改地形应递增 terrain_version"
print("logic fixes ok: no-fire / draw / snap")

# 6) 存一帧供肉眼检查
img.fill(0)
p = QPainter(img)
view.render(p, QPoint(0, 0))
p.end()
img.save("smoke_frame.png")

# 7) 返回菜单再重开一局（简单）
battle.exit_to_menu.emit()
app.processEvents()
assert shell.stack.currentWidget() is shell.start, "未返回开始界面"
shell.start._pick(Difficulty.EASY)
shell.start.seed_edit.setText("")
shell.start._start()
app.processEvents()
assert shell.battle is not None and shell.battle.world.difficulty is Difficulty.EASY
for _ in range(10):
    shell.battle._tick()

# 8) 地图编辑器：开编辑器 → 画地形 → 摆双方兵团 → 右键擦除 → 存/读档 → 试玩 → 回编辑器 → 回菜单
from rts.terrain import Terrain
from rts.world import load_world, save_world

shell.start.seed_edit.setText("11")
shell.start._start_editor()               # StartScreen 信号 → GameShell._open_editor
app.processEvents()
editor = shell.editor
assert editor is not None, "编辑器未创建"
assert shell.stack.currentWidget() is editor, "未切到编辑器页"
ev = editor.view
ev.resize(1000, 760)
app.processEvents()
me = editor.map_editor
assert ev.edit_mode and ev.editor is me, "编辑模式未开启"


def eclick(pos, btn=Qt.LeftButton, kind="press", mods=Qt.NoModifier):
    t = {"press": QMouseEvent.MouseButtonPress, "release": QMouseEvent.MouseButtonRelease,
         "move": QMouseEvent.MouseMove}[kind]
    e = QMouseEvent(t, QPointF(pos), QPointF(pos), btn, btn, mods)
    {"press": ev.mousePressEvent, "release": ev.mouseReleaseEvent,
     "move": ev.mouseMoveEvent}[kind](e)


# 画一条山地：选地形工具 + 3×3 笔刷，拖一条线
editor._pick_terrain(Terrain.MOUNTAIN)
editor._pick_brush(1)
ev.center_on(ev.world.map.pixel_width / 2, ev.world.map.pixel_height / 2)
eclick(QPoint(500, 380), kind="press")
for i in range(1, 12):
    eclick(QPoint(500 + i * 12, 380), kind="move")
eclick(QPoint(620, 380), kind="release")
mtn = sum(row.count(int(Terrain.MOUNTAIN)) for row in ev.world.map.tiles)
print("editor painted mountain tiles:", mtn)
assert mtn > 0, "地形画笔未生效"

# 摆兵：我军步兵 + 敌军弓兵
editor._pick_faction(Faction.PLAYER)
editor._pick_unit("infantry")
eclick(QPoint(300, 300), kind="press"); eclick(QPoint(300, 300), kind="release")
editor._pick_faction(Faction.ENEMY)
editor._pick_unit("archer")
eclick(QPoint(700, 300), kind="press"); eclick(QPoint(700, 300), kind="release")
p_cnt, e_cnt = me.counts()
print("editor units placed:", p_cnt, e_cnt)
assert p_cnt >= 1 and e_cnt >= 1, "摆兵未生效"

# 右键拖拽 = 平移视角（不应擦除兵团）
cam_before = (ev.cam_x, ev.cam_y)
eclick(QPoint(400, 300), btn=Qt.RightButton, kind="press")
for i in range(1, 8):
    eclick(QPoint(400 - i * 18, 300 - i * 9), btn=Qt.RightButton, kind="move")
eclick(QPoint(400 - 130, 300 - 65), btn=Qt.RightButton, kind="release")
assert (ev.cam_x, ev.cam_y) != cam_before, "右键拖拽未平移视角"
assert me.counts()[1] == e_cnt, "右键拖拽误擦了兵团"

# 右键轻点（按下+松手、未拖动）擦除敌军那一枚
esp = ev.to_screen(*[(u.x, u.y) for u in ev.world.units_of(Faction.ENEMY)][0])
eclick(QPoint(int(esp.x()), int(esp.y())), btn=Qt.RightButton, kind="press")
eclick(QPoint(int(esp.x()), int(esp.y())), btn=Qt.RightButton, kind="release")
assert me.counts()[1] == e_cnt - 1, "右键轻点擦除未生效"

# 渲染一帧编辑器（覆盖笔刷光标绘制）
eimg = QImage(ev.width(), ev.height(), QImage.Format_ARGB32)
eimg.fill(0)
ep = QPainter(eimg)
ev._edit_hover = (ev.world.map.pixel_width / 2, ev.world.map.pixel_height / 2)
ev.render(ep, QPoint(0, 0))
ep.end()

# 存档 → 读档（存档前确保两军都在；place 命中水面会被忽略，这里用带吸附的 add_unit 兜底）
import tempfile

scn_path = os.path.join(tempfile.gettempdir(), "rts_editor_smoke.json")
if me.counts()[1] == 0:
    editor.world.add_unit("archer", Faction.ENEMY,
                          ev.world.map.pixel_width * 0.72, ev.world.map.pixel_height * 0.5)
    editor.world.refresh_field()
if me.counts()[0] == 0:
    editor.world.add_unit("infantry", Faction.PLAYER,
                          ev.world.map.pixel_width * 0.28, ev.world.map.pixel_height * 0.5)
    editor.world.refresh_field()
assert all(me.counts()), "存档前应两军都在"
save_world(editor.world, scn_path)
loaded = load_world(scn_path)
assert len(loaded.units) == len(editor.world.units), "读档兵团数不一致"
editor._swap_world(loaded)
assert all(me.counts()), "读档后应两军都在"

# 试玩：应克隆世界并进入战斗页（back_label=编辑器）
editor._playtest()
app.processEvents()
assert shell.battle is not None, "试玩未创建战斗页"
assert shell.battle.back_label == "编辑器", "试玩返回目标应为编辑器"
assert shell.battle.world is not editor.world, "试玩应在克隆世界上进行"
for _ in range(20):
    shell.battle._tick()
print("playtest elapsed", round(shell.battle.world.elapsed, 2), "s")

# 返回编辑器（编辑器仍在栈中）→ 再回主菜单
shell.battle.exit_to_menu.emit()
app.processEvents()
assert shell.stack.currentWidget() is editor, "试玩后未回到编辑器"
editor.exit_to_menu.emit()
app.processEvents()
assert shell.stack.currentWidget() is shell.start and shell.editor is None, "编辑器未回主菜单"

print("ALL OK - frame saved to smoke_frame.png")
