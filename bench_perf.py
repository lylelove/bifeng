"""无头性能基准：批量摆兵后连续推进模拟，测 World.update 与 compute_field 耗时。

用法：python bench_perf.py [每方兵团数=80] [模拟秒数=20]
不依赖 Qt，可在任何环境运行。
"""
from __future__ import annotations

import random
import sys
import time

from rts.ai import Difficulty
from rts.influence import compute_field
from rts.units import Faction
from rts.world import World


def build_world(per_side: int, seed: int = 42) -> World:
    w = World(map_width=160, map_height=120, seed=seed,
              difficulty=Difficulty.HARD, populate=False)
    rng = random.Random(seed)
    pool = ["infantry", "infantry", "cavalry", "archer", "artillery"]
    pw, ph = w.map.pixel_width, w.map.pixel_height
    for faction, cx in ((Faction.PLAYER, pw * 0.25), (Faction.ENEMY, pw * 0.75)):
        for i in range(per_side):
            x = cx + rng.uniform(-pw * 0.18, pw * 0.18)
            y = ph * 0.5 + rng.uniform(-ph * 0.42, ph * 0.42)
            w.add_unit(pool[i % len(pool)], faction, x, y)
    return w


def main() -> None:
    per_side = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    dt = 1.0 / 30.0
    world = build_world(per_side)
    n0 = len(world.units)

    # 双方朝中间推进，尽快进入交战（负载最高的阶段）
    mid_x = world.map.pixel_width * 0.5
    for u in world.units:
        u.set_path([(mid_x, u.y)], world.map)

    steps = int(seconds / dt)
    t_update = 0.0
    worst = 0.0
    t0 = time.perf_counter()
    for _ in range(steps):
        s = time.perf_counter()
        world.update(dt)
        e = time.perf_counter() - s
        t_update += e
        worst = max(worst, e)
    wall = time.perf_counter() - t0

    # 单独测一次全量势力场重算
    s = time.perf_counter()
    for _ in range(20):
        compute_field(world)
    t_field = (time.perf_counter() - s) / 20

    print(f"units      : {n0} -> {len(world.units)} (alive at end)")
    print(f"sim        : {seconds:.0f}s game time, {steps} steps, wall {wall:.2f}s")
    print(f"update avg : {t_update / steps * 1000:.3f} ms/step")
    print(f"update max : {worst * 1000:.3f} ms")
    print(f"field once : {t_field * 1000:.3f} ms")
    print(f"realtime x : {seconds / wall:.1f}")


if __name__ == "__main__":
    main()
