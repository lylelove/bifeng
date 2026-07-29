"""势力（影响力）场：把每个兵团按距离衰减投射到一张粗网格上，
再按「谁的影响力更强」把每一格硬性划归一方——整张战场被一条清晰的接触线
一分为二，中间不留无主地带。用于渲染占领区图层和统计势力对比。

影响力在 INF_RADIUS 处硬截断：贡献 = presence * (1 - d²/R²)，超出半径为 0。
这保证活着的兵团必然控制自己脚下（远方大军的长尾无法「隔空」压过驻地），
双方都够不到的格子再由 BFS 从已归属格向外泛洪，把战场补成两色。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .terrain import TERRAIN_INFO, TILE, Terrain
from .units import Faction, Unit

INF_STEP = 3                 # 每个影响力格 = 3 个地形格（越小边界越精细）
INF_RADIUS = 150.0           # 单位影响力半径（像素），超出即无贡献

# 地形值 → 是否可通行（按 int 索引，避开热循环里的枚举与坐标换算）
_PASSABLE = [TERRAIN_INFO[t].passable for t in Terrain]


@dataclass
class InfluenceField:
    gw: int
    gh: int
    step_px: float
    owner: list[list[int]]         # 每格归属：+1 我方，-1 敌方（无中立）
    player_cells: int              # 我方控制的可通行格数
    enemy_cells: int               # 敌方控制的可通行格数

    @property
    def total_controlled(self) -> int:
        return self.player_cells + self.enemy_cells

    def share(self) -> tuple[float, float]:
        """我方 / 敌方 控制陆地面积的占比（0..1，和为 1）。"""
        tot = self.total_controlled
        if tot == 0:
            return 0.5, 0.5
        return self.player_cells / tot, self.enemy_cells / tot

    def owner_at(self, gx: int, gy: int) -> int:
        if 0 <= gx < self.gw and 0 <= gy < self.gh:
            return self.owner[gy][gx]
        return 0


def _presence(u: Unit) -> float:
    # 残血单位控制力下降；溃逃部队只保留三成影响力（与军力对比条一致）
    p = 0.5 + u.hp_ratio
    return p * 0.3 if u.routing else p


def _passable_cells(gm, gw: int, gh: int) -> list[list[bool]]:
    """每个势力格的代表点是否可通行；按 (map_uid, terrain_version) 缓存。

    地形不变时该表恒定，无需每次重算时全图扫地形——只有编辑器改动地形
    （terrain_version 变化）或整体换图（map_uid 变化）才重建。
    """
    key = (gm.map_uid, gm.terrain_version, gw, gh)
    cached = getattr(gm, "_inf_pass_cache", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    tiles = gm.tiles
    grid: list[list[bool]] = []
    for gy in range(gh):
        ty = min(gm.height - 1, gy * INF_STEP + INF_STEP // 2)
        trow = tiles[ty]
        grid.append([_PASSABLE[trow[min(gm.width - 1, gx * INF_STEP + INF_STEP // 2)]]
                     for gx in range(gw)])
    gm._inf_pass_cache = (key, grid)
    return grid


def _splat(grid: list[list[float]], gw: int, gh: int, step_px: float,
           units: list[Unit]) -> None:
    """把一批兵团的影响力散布（scatter）到网格：只写各自半径内的格子。"""
    half = step_px * 0.5
    r = INF_RADIUS
    inv_r2 = 1.0 / (r * r)
    for u in units:
        pp = _presence(u)
        gx0 = max(0, int((u.x - r) / step_px))
        gx1 = min(gw - 1, int((u.x + r) / step_px))
        gy0 = max(0, int((u.y - r) / step_px))
        gy1 = min(gh - 1, int((u.y + r) / step_px))
        for gy in range(gy0, gy1 + 1):
            dy = u.y - (gy * step_px + half)
            row = grid[gy]
            for gx in range(gx0, gx1 + 1):
                dx = u.x - (gx * step_px + half)
                w = 1.0 - (dx * dx + dy * dy) * inv_r2
                if w > 0.0:
                    row[gx] += pp * w


def compute_field(world) -> InfluenceField:
    gm = world.map
    gw = (gm.width + INF_STEP - 1) // INF_STEP
    gh = (gm.height + INF_STEP - 1) // INF_STEP
    step_px = INF_STEP * TILE

    players = world.units_of(Faction.PLAYER)
    enemies = world.units_of(Faction.ENEMY)

    owner = [[0] * gw for _ in range(gh)]

    # 一方被全歼：整图归另一方；双方皆无：全部中立
    if not players or not enemies:
        default = 1 if players else (-1 if enemies else 0)
        for row in owner:
            for gx in range(gw):
                row[gx] = default
    else:
        # 1) 散布影响力（有限半径，远方大军无法隔空施压）
        pv = [[0.0] * gw for _ in range(gh)]
        ev = [[0.0] * gw for _ in range(gh)]
        _splat(pv, gw, gh, step_px, players)
        _splat(ev, gw, gh, step_px, enemies)

        # 2) 有影响力的格子按强弱划归；同时作为 BFS 种子
        queue: deque[tuple[int, int]] = deque()
        for gy in range(gh):
            prow, erow, orow = pv[gy], ev[gy], owner[gy]
            for gx in range(gw):
                a, b = prow[gx], erow[gx]
                if a == 0.0 and b == 0.0:
                    continue
                if a > b:
                    orow[gx] = 1
                elif b > a:
                    orow[gx] = -1
                else:
                    orow[gx] = 1 if (gx + gy) & 1 else -1  # 均势交错，避免系统性偏我方
                queue.append((gx, gy))

        # 3) 双方都够不到的格子：从已归属格向外泛洪，填成两色不留空白
        while queue:
            gx, gy = queue.popleft()
            o = owner[gy][gx]
            for nx, ny in ((gx - 1, gy), (gx + 1, gy), (gx, gy - 1), (gx, gy + 1)):
                if 0 <= nx < gw and 0 <= ny < gh and owner[ny][nx] == 0:
                    owner[ny][nx] = o
                    queue.append((nx, ny))

    # 面积统计只算可通行格（水面无法据守，不计入领土对比）
    # 通行性表按地形版本缓存，地形不变时零地形查询
    pc = ec = 0
    passable = _passable_cells(gm, gw, gh)
    for gy in range(gh):
        orow, prow = owner[gy], passable[gy]
        for gx in range(gw):
            o = orow[gx]
            if o != 0 and prow[gx]:
                if o > 0:
                    pc += 1
                else:
                    ec += 1

    return InfluenceField(gw, gh, step_px, owner, pc, ec)

