"""地图编辑器控制器（不依赖 Qt）。

`MapEditor` 持有一个 `World` 与当前的编辑工具状态（地形画笔 / 兵团摆放 / 擦除，
以及笔刷大小、阵营、兵种），把「在某个世界坐标点按下 / 拖动 / 右键」翻译成对
地图与兵团的具体改动。视图层（MapView）在编辑模式下只需把鼠标坐标交给这里，
UI 层（EditorPage）只需切换工具状态——真正的读写都收敛在本类，逻辑层保持可测。
"""
from __future__ import annotations

import random

from .terrain import GameMap, Terrain
from .units import Faction
from .world import World

# 工具种类（用字符串而非枚举，避免 mapview 反向依赖本模块）
TOOL_TERRAIN = "terrain"     # 地形画笔：按住左键涂地形
TOOL_UNIT = "unit"           # 兵团摆放：左键点一下放一个
TOOL_ERASE = "erase"         # 擦除：左键点/拖删除光标下的兵团

# 笔刷大小 → 半径（格）：1×1 / 3×3 / 5×5
BRUSH_RADII = (0, 1, 2)


class MapEditor:
    def __init__(self, world: World):
        self.world = world
        self.tool: str = TOOL_TERRAIN
        self.terrain: Terrain = Terrain.PLAIN
        self.faction: Faction = Faction.PLAYER
        self.unit_type: str = "infantry"
        self.brush: int = 0                 # 半径（格），见 BRUSH_RADII

    # ------------------------------------------------------------ 鼠标事件转发
    def on_press(self, wx: float, wy: float) -> bool:
        """左键按下。返回是否改动了地形（供视图决定是否重建地形缓存）。"""
        if self.tool == TOOL_TERRAIN:
            return self.paint(wx, wy)
        if self.tool == TOOL_UNIT:
            self.place(wx, wy)
        elif self.tool == TOOL_ERASE:
            self.erase(wx, wy)
        return False

    def on_drag(self, wx: float, wy: float) -> bool:
        """左键拖动：仅地形与擦除工具连续生效；兵团摆放不跟随拖动。"""
        if self.tool == TOOL_TERRAIN:
            return self.paint(wx, wy)
        if self.tool == TOOL_ERASE:
            self.erase(wx, wy)
        return False

    def on_secondary(self, wx: float, wy: float) -> bool:
        """右键：无论当前工具，一律擦除光标下的兵团（顺手的老习惯）。"""
        self.erase(wx, wy)
        return False

    # ------------------------------------------------------------ 具体动作
    def paint(self, wx: float, wy: float) -> bool:
        gm = self.world.map
        tx, ty = gm.tile_at(wx, wy)
        r = self.brush
        changed = False
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if gm.set_terrain(tx + dx, ty + dy, self.terrain):
                    changed = True
        if changed:
            # 可能把兵团脚下涂成水面，立刻吸附到最近可通行处，避免卡死
            self.world.snap_units_passable()
            self.world.refresh_field()      # 通行区变了，势力预览跟着更新
        return changed

    def place(self, wx: float, wy: float) -> bool:
        """在可通行处放一个当前阵营/兵种的兵团。落在水面则忽略。"""
        if not self.world.map.passable(wx, wy):
            return False
        self.world.add_unit(self.unit_type, self.faction, wx, wy)
        self.world.refresh_field()
        return True

    def erase(self, wx: float, wy: float) -> bool:
        u = self.world.unit_at(wx, wy, slack=8.0)
        if u is None:
            return False
        self.world.remove_unit(u)
        self.world.refresh_field()
        return True

    # ------------------------------------------------------------ 整图操作
    def fill(self, terrain: Terrain) -> None:
        self.world.map.fill(terrain)
        self.world.snap_units_passable()
        self.world.refresh_field()

    def clear_units(self) -> None:
        self.world.clear_units()
        self.world.refresh_field()

    def regenerate(self, seed: int | None = None) -> int:
        """按新种子重新生成自然地形（保留已摆的兵团）。返回实际使用的种子。"""
        gm = self.world.map
        if seed is None:
            seed = random.randrange(1 << 30)
        self.world.map = GameMap(gm.width, gm.height, seed)
        # 新地形可能把原位置变成湖泊，把兵团吸附到可通行格
        self.world.snap_units_passable()
        self.world.refresh_field()
        return self.world.map.seed

    # ------------------------------------------------------------ 查询
    def counts(self) -> tuple[int, int]:
        """(我军团数, 敌军团数)。"""
        return (len(self.world.units_of(Faction.PLAYER)),
                len(self.world.units_of(Faction.ENEMY)))

    def is_terrain_tool(self) -> bool:
        return self.tool == TOOL_TERRAIN

    def cursor_radius(self) -> int:
        """光标footprint 半径（格）：地形笔刷用笔刷大小，其余为单格。"""
        return self.brush if self.tool == TOOL_TERRAIN else 0
