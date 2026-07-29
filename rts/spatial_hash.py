"""空间哈希网格：把兵团按坐标装进固定边长的方格桶。

「找附近的兵团」由全体扫描 O(n) 降为只翻圆域覆盖到的几个桶 O(k)；
索敌、碰撞分离、士气感知、战争迷雾等热循环由此从 O(n²) 降为 O(n·k)。

本模块不依赖 Qt 与其它游戏模块，可单独测试。约定：
- 桶内只存重建时刻存活的单位；查询方仍需自行复查 alive / 阵营 / 精确距离。
- query() 只做「粗筛」：返回的集合覆盖圆域，但含少量圈外单位。
- 同一帧内重建后单位若又被小幅推挤（碰撞分离约几个像素），桶归属可能
  过期；query 用 pad 外扩一圈兜底，精确距离判定始终用单位当前坐标。
"""
from __future__ import annotations

from typing import Iterable, Iterator

CELL = 96.0     # 桶边长（像素）。需 ≥ 碰撞候选对的最大中心距（2×最大半径=30）
QUERY_PAD = 16.0  # 粗筛外扩，兜底重建后同帧内的小幅位移


class SpatialHash:
    """按 (x//cell, y//cell) 分桶的均匀网格。每帧 rebuild 一次，O(n)。"""

    def __init__(self, cell: float = CELL):
        self.cell = cell
        self._buckets: dict[tuple[int, int], list] = {}

    # ------------------------------------------------------------ 构建
    def rebuild(self, units: Iterable) -> None:
        """用当前存活单位重建全部桶。"""
        self._buckets.clear()
        c = self.cell
        buckets = self._buckets
        for u in units:
            if not u.alive:
                continue
            key = (int(u.x // c), int(u.y // c))
            b = buckets.get(key)
            if b is None:
                buckets[key] = [u]
            else:
                b.append(u)

    # ------------------------------------------------------------ 查询
    def query(self, x: float, y: float, r: float) -> list:
        """粗筛：返回圆 (x, y, r) 附近桶里的所有单位（可能含圈外少量单位，
        调用方自行做精确距离/阵营/存活判定）。"""
        c = self.cell
        rr = r + QUERY_PAD
        gx0 = int((x - rr) // c)
        gx1 = int((x + rr) // c)
        gy0 = int((y - rr) // c)
        gy1 = int((y + rr) // c)
        buckets = self._buckets
        if gx0 == gx1 and gy0 == gy1:       # 常见：小半径只命中一桶
            b = buckets.get((gx0, gy0))
            return list(b) if b else []
        out: list = []
        get = buckets.get
        for gy in range(gy0, gy1 + 1):
            for gx in range(gx0, gx1 + 1):
                b = get((gx, gy))
                if b:
                    out.extend(b)
        return out

    def iter_pairs(self) -> Iterator[tuple]:
        """遍历中心距可能 ≤ cell 的候选单位对，每对恰好出现一次。

        覆盖方式：桶内全配对 + 与半邻域（右、下、右下、左下）四桶交叉配对。
        任何中心距 ≤ cell 的两单位必然落在同桶或相邻桶，故不漏对。
        供碰撞分离使用（要求碰撞距离 ≤ cell，当前 2×最大半径=30 ≪ 96）。
        """
        buckets = self._buckets
        get = buckets.get
        half = ((1, 0), (0, 1), (1, 1), (-1, 1))   # 半邻域，保证每对只出一次
        for (gx, gy), cell_units in buckets.items():
            n = len(cell_units)
            for i in range(n):
                a = cell_units[i]
                for j in range(i + 1, n):
                    yield a, cell_units[j]
            for dx, dy in half:
                other = get((gx + dx, gy + dy))
                if not other:
                    continue
                for a in cell_units:
                    for b in other:
                        yield a, b
