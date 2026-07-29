"""地形数据：地形类型、地形效果、程序化地图生成（平原/山地/河流/湖泊）。

本模块不依赖 Qt，方便单独测试。颜色用 (r, g, b) 元组表示。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import IntEnum

TILE = 16  # 每个地形格的世界像素大小


class Terrain(IntEnum):
    PLAIN = 0
    FOREST = 1
    HILL = 2
    MOUNTAIN = 3
    RIVER = 4
    LAKE = 5


@dataclass(frozen=True)
class TerrainInfo:
    name: str
    color: tuple[int, int, int]
    speed: float      # 移动速度倍率
    defense: float    # 受到伤害的减免倍率（越大越耐打）
    attack: float     # 在此地形上的输出倍率（高地优势）
    vision: float     # 视野倍率（影响自动索敌距离）
    passable: bool
    desc: str


TERRAIN_INFO: dict[Terrain, TerrainInfo] = {
    Terrain.PLAIN: TerrainInfo(
        "平原", (126, 166, 84), 1.00, 1.00, 1.00, 1.00, True,
        "行军顺畅，无攻防加成"),
    Terrain.FOREST: TerrainInfo(
        "森林", (58, 105, 62), 0.65, 1.30, 0.95, 0.75, True,
        "行军减速，隐蔽性好，防御 +30%"),
    Terrain.HILL: TerrainInfo(
        "丘陵", (150, 137, 92), 0.80, 1.20, 1.15, 1.25, True,
        "视野与火力占优，防御 +20%"),
    Terrain.MOUNTAIN: TerrainInfo(
        "山地", (122, 112, 104), 0.45, 1.55, 1.25, 1.40, True,
        "极难通行，但攻防俱佳，易守难攻"),
    Terrain.RIVER: TerrainInfo(
        "河流", (86, 148, 190), 0.35, 0.60, 0.60, 1.00, True,
        "涉水缓慢，半渡而击时极其脆弱"),
    Terrain.LAKE: TerrainInfo(
        "湖泊", (46, 96, 152), 0.00, 1.00, 1.00, 1.00, False,
        "深水区，陆上兵团无法通行"),
}

# 编辑器手绘地形时，给该格一个代表性高程，让明暗（shade_at）与地形观感一致。
# 取值落在 GameMap 的自然分类区间内，涂出来的地块与生成地形融为一体。
NOMINAL_ELEVATION: dict[Terrain, float] = {
    Terrain.LAKE: 0.10,
    Terrain.RIVER: 0.30,
    Terrain.PLAIN: 0.45,
    Terrain.FOREST: 0.50,
    Terrain.HILL: 0.68,
    Terrain.MOUNTAIN: 0.85,
}


# ---------------------------------------------------------------- 噪声

def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def _value_noise(width: int, height: int, freq: int, rng: random.Random) -> list[list[float]]:
    """在 freq x freq 的随机格点上做双线性平滑插值，得到 width x height 的噪声。"""
    gw = gh = freq + 1
    grid = [[rng.random() for _ in range(gw)] for _ in range(gh)]
    out = [[0.0] * width for _ in range(height)]
    for y in range(height):
        fy = y / max(1, height - 1) * freq
        y0 = min(int(fy), gh - 2)
        ty = _smoothstep(fy - y0)
        row0, row1 = grid[y0], grid[y0 + 1]
        dst = out[y]
        for x in range(width):
            fx = x / max(1, width - 1) * freq
            x0 = min(int(fx), gw - 2)
            tx = _smoothstep(fx - x0)
            a = row0[x0] * (1 - tx) + row0[x0 + 1] * tx
            b = row1[x0] * (1 - tx) + row1[x0 + 1] * tx
            dst[x] = a * (1 - ty) + b * ty
    return out


def fbm(width: int, height: int, seed: int, octaves: int = 5,
        base_freq: int = 3, persistence: float = 0.5) -> list[list[float]]:
    """分形叠加噪声，返回归一化到 [0, 1] 的二维数组。"""
    rng = random.Random(seed)
    acc = [[0.0] * width for _ in range(height)]
    amp, total = 1.0, 0.0
    freq = base_freq
    for _ in range(octaves):
        layer = _value_noise(width, height, freq, rng)
        for y in range(height):
            arow, lrow = acc[y], layer[y]
            for x in range(width):
                arow[x] += lrow[x] * amp
        total += amp
        amp *= persistence
        freq *= 2
    lo, hi = 1e9, -1e9
    for row in acc:
        for v in row:
            lo = v if v < lo else lo
            hi = v if v > hi else hi
    span = (hi - lo) or 1.0
    for y in range(height):
        row = acc[y]
        for x in range(width):
            row[x] = (row[x] - lo) / span
    return acc


_NEIGHBORS8 = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]

# 地形 int → 通行代价 / 是否可走（热路径避免反复构造 Terrain 枚举）
_TILE_COST: list[float] = [0.0] * (max(int(t) for t in Terrain) + 1)
_TILE_PASS: list[bool] = [False] * len(_TILE_COST)
for _t, _info in TERRAIN_INFO.items():
    _TILE_COST[int(_t)] = math.inf if not _info.passable else 1.0 / _info.speed
    _TILE_PASS[int(_t)] = _info.passable


# ---------------------------------------------------------------- 地图

class GameMap:
    """格子地图。世界坐标以像素为单位，1 格 = TILE 像素。"""

    _uid_seq = 0                    # 全局递增：区分不同地图实例（缓存键用）

    def __init__(self, width: int = 160, height: int = 120, seed: int | None = None):
        if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
            raise ValueError("地图 width 必须是正整数")
        if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
            raise ValueError("地图 height 必须是正整数")
        self.width = width
        self.height = height
        if seed is None:
            self.seed = random.randrange(1 << 30)
        else:
            try:
                self.seed = int(seed) % (1 << 30)
            except (TypeError, ValueError):
                # 文字种子与开始界面一致：CRC32，跨进程可复现
                import zlib
                self.seed = zlib.crc32(str(seed).encode("utf-8")) % (1 << 30)
        self.terrain_version = 0    # 地形每次改动 +1，供各层缓存失效
        GameMap._uid_seq += 1
        self.map_uid = GameMap._uid_seq   # 实例唯一号（编辑器可整体换图）
        rng = random.Random(self.seed)

        self.elevation = fbm(width, height, self.seed, octaves=5, base_freq=3)
        moisture = fbm(width, height, self.seed ^ 0x5bf03635, octaves=4, base_freq=4)

        self.tiles: list[list[int]] = [[Terrain.PLAIN] * width for _ in range(height)]
        for y in range(height):
            erow, mrow, trow = self.elevation[y], moisture[y], self.tiles[y]
            for x in range(width):
                e, m = erow[x], mrow[x]
                if e < 0.20:
                    trow[x] = Terrain.LAKE
                elif e > 0.76:
                    trow[x] = Terrain.MOUNTAIN
                elif e > 0.63:
                    trow[x] = Terrain.HILL
                elif m > 0.60:
                    trow[x] = Terrain.FOREST
                else:
                    trow[x] = Terrain.PLAIN

        self._carve_rivers(rng, count=max(3, width // 45))
        self._cleanup_puddles()

    # ------------------------------------------------------------ 生成细节
    def _carve_rivers(self, rng: random.Random, count: int) -> None:
        """从高处沿最陡下降方向走到低处/湖泊/地图边缘，刻出河道。"""
        sources = [(x, y) for y in range(2, self.height - 2) for x in range(2, self.width - 2)
                   if self.elevation[y][x] > 0.70]
        if not sources:
            return
        rng.shuffle(sources)
        for sx, sy in sources[:count]:
            x, y, visited = sx, sy, set()
            for _ in range(self.width * 3):
                if not (0 < x < self.width - 1 and 0 < y < self.height - 1):
                    break
                if self.tiles[y][x] == Terrain.LAKE:
                    break
                visited.add((x, y))
                self.tiles[y][x] = Terrain.RIVER
                # 让河道略有宽度，走向更自然
                if rng.random() < 0.35:
                    nx, ny = x + rng.choice((-1, 1)), y + rng.choice((-1, 1))
                    if self._inside(nx, ny) and self.tiles[ny][nx] != Terrain.LAKE:
                        self.tiles[ny][nx] = Terrain.RIVER
                best, best_h = None, self.elevation[y][x]
                for dx, dy in _NEIGHBORS8:
                    nx, ny = x + dx, y + dy
                    if not self._inside(nx, ny) or (nx, ny) in visited:
                        continue
                    h = self.elevation[ny][nx] + rng.uniform(-0.012, 0.012)
                    if h < best_h:
                        best, best_h = (nx, ny), h
                if best is None:              # 落入洼地：积水成湖
                    self._fill_lake(x, y, radius=rng.randint(2, 4))
                    break
                x, y = best

    def _fill_lake(self, cx: int, cy: int, radius: int) -> None:
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if self._inside(x, y) and (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius:
                    self.tiles[y][x] = Terrain.LAKE

    def _cleanup_puddles(self) -> None:
        """去掉孤立的单格湖泊，避免地图上出现密密麻麻的小坑。"""
        for y in range(self.height):
            for x in range(self.width):
                if self.tiles[y][x] != Terrain.LAKE:
                    continue
                water = sum(1 for dx, dy in _NEIGHBORS8
                            if self._inside(x + dx, y + dy)
                            and self.tiles[y + dy][x + dx] in (Terrain.LAKE, Terrain.RIVER))
                if water <= 1:
                    self.tiles[y][x] = Terrain.PLAIN

    # ------------------------------------------------------------ 查询
    def _inside(self, tx: int, ty: int) -> bool:
        return 0 <= tx < self.width and 0 <= ty < self.height

    @property
    def pixel_width(self) -> int:
        return self.width * TILE

    @property
    def pixel_height(self) -> int:
        return self.height * TILE

    def tile_at(self, wx: float, wy: float) -> tuple[int, int]:
        tx = min(self.width - 1, max(0, int(wx // TILE)))
        ty = min(self.height - 1, max(0, int(wy // TILE)))
        return tx, ty

    def terrain_at(self, wx: float, wy: float) -> Terrain:
        tx, ty = self.tile_at(wx, wy)
        return Terrain(self.tiles[ty][tx])

    def info_at(self, wx: float, wy: float) -> TerrainInfo:
        return TERRAIN_INFO[self.terrain_at(wx, wy)]

    def passable(self, wx: float, wy: float) -> bool:
        if not (0 <= wx < self.pixel_width and 0 <= wy < self.pixel_height):
            return False
        tx = min(self.width - 1, max(0, int(wx // TILE)))
        ty = min(self.height - 1, max(0, int(wy // TILE)))
        return _TILE_PASS[self.tiles[ty][tx]]

    def nearest_passable(self, wx: float, wy: float, max_ring: int = 40) -> tuple[float, float]:
        """把落在不可通行区域的点吸附到最近的可通行格中心。"""
        if self.passable(wx, wy):
            return wx, wy
        tx, ty = self.tile_at(wx, wy)
        # 界外坐标会先夹到边界格；若该格可走，它就是最近落点，不能从 r=1 跳过。
        if _TILE_PASS[self.tiles[ty][tx]]:
            return (tx + 0.5) * TILE, (ty + 0.5) * TILE
        for r in range(1, max_ring + 1):
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    nx, ny = tx + dx, ty + dy
                    if self._inside(nx, ny) and _TILE_PASS[self.tiles[ny][nx]]:
                        return (nx + 0.5) * TILE, (ny + 0.5) * TILE
        return wx, wy

    def shade_at(self, tx: int, ty: int) -> float:
        """依据高程给出明暗系数，让地图有起伏感。"""
        return 0.82 + 0.36 * self.elevation[ty][tx]

    # ------------------------------------------------------------ 编辑（地图编辑器）
    def set_terrain(self, tx: int, ty: int, terrain: Terrain, update_shade: bool = True) -> bool:
        """把一格改成指定地形（供编辑器画笔调用）。返回是否真的发生了改变。

        默认顺带把该格高程设为该地形的代表值，使明暗与地形一致；越界或未变化返回 False。
        """
        if not self._inside(tx, ty):
            return False
        if self.tiles[ty][tx] == int(terrain):
            return False
        self.tiles[ty][tx] = int(terrain)
        if update_shade:
            self.elevation[ty][tx] = NOMINAL_ELEVATION[Terrain(terrain)]
        self.terrain_version += 1
        return True

    def fill(self, terrain: Terrain) -> None:
        """把整张地图铺成同一种地形（编辑器「清空」用）。"""
        e = NOMINAL_ELEVATION[Terrain(terrain)]
        t = int(terrain)
        for y in range(self.height):
            trow, erow = self.tiles[y], self.elevation[y]
            for x in range(self.width):
                trow[x] = t
                erow[x] = e
        self.terrain_version += 1

    # ------------------------------------------------------------ 寻路
    def tile_cost(self, tx: int, ty: int) -> float:
        """通行代价：越慢的地形代价越高，不可通行为 inf。"""
        return _TILE_COST[self.tiles[ty][tx]]

    def line_clear(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        """两点之间的直线是否全程可通行（按半格步进采样）。"""
        dist = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(dist / (TILE * 0.5)))
        inv = 1.0 / steps
        w, h = self.width, self.height
        tiles = self.tiles
        pw, ph = self.pixel_width, self.pixel_height
        for i in range(steps + 1):
            k = i * inv
            wx = x0 + (x1 - x0) * k
            wy = y0 + (y1 - y0) * k
            if not (0 <= wx < pw and 0 <= wy < ph):
                return False
            tx = min(w - 1, max(0, int(wx // TILE)))
            ty = min(h - 1, max(0, int(wy // TILE)))
            if not _TILE_PASS[tiles[ty][tx]]:
                return False
        return True

    def find_path(self, x0: float, y0: float, x1: float, y1: float,
                  max_expand: int = 30000) -> list[tuple[float, float]]:
        """A* 网格寻路，返回不含起点的世界坐标航点列表；失败返回空列表。

        直线可通时直接返回终点（跳过 A*）；热路径用 _TILE_COST 查表，不构造枚举。
        """
        import heapq

        sx, sy = self.tile_at(*self.nearest_passable(x0, y0))
        gx, gy = self.tile_at(*self.nearest_passable(x1, y1))
        goal_pt = ((gx + 0.5) * TILE, (gy + 0.5) * TILE)
        if (sx, sy) == (gx, gy):
            return [goal_pt]

        # 开阔地直线可达：O(采样) 即可，避免整图 A*
        if self.line_clear(x0, y0, goal_pt[0], goal_pt[1]):
            return [goal_pt]

        def h(x: int, y: int) -> float:
            dx, dy = abs(x - gx), abs(y - gy)
            return (dx + dy) + 0.41421356237 * min(dx, dy)  # ≈ √2-1 对角启发

        width = self.width
        tiles = self.tiles
        costs = _TILE_COST
        start = sy * width + sx
        goal = gy * width + gx
        g_score = {start: 0.0}
        came: dict[int, int] = {}
        heap = [(h(sx, sy), start)]
        closed: set[int] = set()
        expanded = 0
        heappop = heapq.heappop
        heappush = heapq.heappush

        while heap:
            _, cur = heappop(heap)
            if cur in closed:
                continue
            if cur == goal:
                break
            closed.add(cur)
            expanded += 1
            if expanded > max_expand:
                return []
            cy, cx = divmod(cur, width)
            base = g_score[cur]
            for dx, dy in _NEIGHBORS8:
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < width and 0 <= ny < self.height):
                    continue
                cost = costs[tiles[ny][nx]]
                if cost == math.inf:
                    continue
                if dx and dy:  # 禁止贴着障碍斜穿
                    if (costs[tiles[cy][cx + dx]] == math.inf
                            or costs[tiles[cy + dy][cx]] == math.inf):
                        continue
                    cost *= 1.41421356237
                nid = ny * width + nx
                ng = base + cost
                if ng < g_score.get(nid, math.inf):
                    g_score[nid] = ng
                    came[nid] = cur
                    heappush(heap, (ng + h(nx, ny), nid))
        else:
            return []

        # 回溯
        node, rev = goal, []
        while node != start:
            ny, nx = divmod(node, width)
            rev.append(((nx + 0.5) * TILE, (ny + 0.5) * TILE))
            node = came[node]
        rev.reverse()
        return self.smooth_path((x0, y0), rev)

    def smooth_path(self, origin: tuple[float, float],
                    pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """漏斗式简化：能直达就跳过中间点，去掉网格锯齿。"""
        if not pts:
            return []
        out: list[tuple[float, float]] = []
        cx, cy = origin
        i = 0
        while i < len(pts):
            j = len(pts) - 1
            while j > i and not self.line_clear(cx, cy, *pts[j]):
                j -= 1
            out.append(pts[j])
            cx, cy = pts[j]
            i = j + 1
        return out

