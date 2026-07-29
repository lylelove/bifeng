"""新增指令的逻辑验证：攻击移动 / 巡逻 / 编队限速 / 画笔障碍判定（离屏，不渲染）。"""
import math
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rts.world import World
from rts.units import Faction
from rts.ai import Difficulty
from rts.terrain import TERRAIN_INFO, TILE, Terrain, GameMap

# ---- 1. 攻击移动：接敌暂存路线，灭敌后续程 ----
w = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p = w.add_unit("infantry", Faction.PLAYER, 200, 200)
e = w.add_unit("infantry", Faction.ENEMY, 200, 200)
e.x, e.y = p.x + 30, p.y                       # 拉到步兵射程(48)内
w.issue_attack_move([p], [(p.x + 600, p.y)])
assert p.order == "attack_move", f"order={p.order}"
assert p.path, "攻击移动应给出路线"
for _ in range(30):
    w.update(0.1)
assert p.target_uid == e.uid, "射程内应索敌锁定"
assert not p.path and p.travel_path, "接敌应暂存剩余路线并停下"
e.hp = 0.0                                     # 击杀敌军
for _ in range(15):
    w.update(0.1)
assert p.path and not p.travel_path, "灭敌后应恢复行军路线"
print("attack_move OK")

# ---- 2. 巡逻：在方形路线上往返，走完正序应反向 ----
w2 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p2 = w2.add_unit("infantry", Faction.PLAYER, 200, 200)
pts = [(p2.x, p2.y), (p2.x + 150, p2.y),
       (p2.x + 150, p2.y + 150), (p2.x, p2.y + 150)]
w2.issue_patrol([p2], pts)
assert p2.order == "patrol" and p2.patrol_route and p2.patrol_fwd
assert len(p2.patrol_route) >= 2, "巡逻路线应有多点"
for _ in range(6000):
    w2.update(0.05)
    if not p2.patrol_fwd:
        break
assert not p2.patrol_fwd, "走完正序应反向续程"
assert p2.path, "反向后应有续程路线"
print("patrol OK")

# ---- 3. 编队限速：快兵被压到最慢兵速度，非编队兵不限速 ----
w3 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
cav = w3.add_unit("cavalry", Faction.PLAYER, 200, 200)    # 速度 78
art = w3.add_unit("artillery", Faction.PLAYER, 220, 200)  # 速度 27
w3.issue_move([cav, art], cav.x + 500, cav.y, formation=True)
assert cav.formation_lock and art.formation_lock
assert cav.formation_id == art.formation_id != 0
w3.update(0.1)
art_speed = art.spec.speed * max(TERRAIN_INFO[w3.map.terrain_at(art.x, art.y)].speed, 0.15)
assert 0 < cav._speed_cap <= art_speed + 1e-6, \
    f"骑兵应被限速到炮兵速度 cap={cav._speed_cap} art={art_speed}"
# 不带编队的兵不限速
solo = w3.add_unit("infantry", Faction.PLAYER, 240, 200)
w3.issue_move([solo], solo.x + 500, solo.y)
w3.update(0.1)
assert solo._speed_cap == 0.0, "非编队兵不应限速"
# 停止应解除编队
w3.stop([cav, art])
assert not cav.formation_lock and not art.formation_lock
print("formation OK")

# ---- 4. 画笔障碍判定：湖泊不可通行，穿湖直线 line_clear=False ----
w4 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
gm = w4.map
lake = None
for y in range(gm.height):
    for x in range(gm.width):
        if gm.tiles[y][x] == int(Terrain.LAKE):
            lake = (x, y)
            break
    if lake:
        break
assert lake, "地图应生成湖泊"
lx, ly = lake
lpx, lpy = (lx + 0.5) * TILE, (ly + 0.5) * TILE
assert not gm.passable(lpx, lpy), "湖泊格不可通行"
# 从湖心向右跨 3 格，直线必然穿湖 → line_clear=False
assert not gm.line_clear(lpx, lpy, lpx + TILE * 3, lpy), "穿湖直线应判定为受阻"
print("obstacle detect OK, lake at", lake)

# ---- 5. 溃逃清指令：巡逻兵溃逃后不得续走巡逻路线，须向后方逃窜 ----
w5 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p5 = w5.add_unit("infantry", Faction.PLAYER, 400, 400)
e5 = w5.add_unit("infantry", Faction.ENEMY, 900, 400)
w5.issue_patrol([p5], [(p5.x, p5.y), (p5.x + 150, p5.y)], formation=True)
p5.morale = 0.0                                # 强制士气崩溃
w5.update(0.05)
assert p5.routing, "士气归零应溃逃"
assert p5.order == "move" and not p5.patrol_route and not p5.travel_path, \
    "溃逃应清空巡逻/暂存路线指令"
assert not p5.formation_lock and p5.formation_id == 0, "溃逃应退出编队"
for _ in range(20):
    w5.update(0.05)
assert p5.routing and p5.path, "溃兵应有逃跑路线"
assert p5.path[-1][0] < 400, "我军溃兵应向西（己方后方）逃窜"
print("rout-clears-orders OK")

# ---- 6. 剧本载入校验：损坏/不支持的数据必须拒绝，合法快照可重建 ----
bad_cases = [
    {"version": 1, "width": 2, "height": 2, "tiles": [0]},        # tiles 长度错
    {"version": 99, "width": 2, "height": 2, "tiles": [0] * 4},   # 版本不支持
    {"version": 1, "width": 2, "height": 2, "tiles": [0, 1, 2]},  # 长度错（3≠4）
    {"version": 1, "width": 0, "height": 2, "tiles": []},         # 尺寸越界
    {"version": 1, "width": 2, "height": 2, "tiles": [99, 0, 0, 0]},  # 未知地形编号
]
for d in bad_cases:
    try:
        World.from_dict(d)
        assert False, f"应拒绝非法剧本: {d.get('version')} {list(d.keys())}"
    except ValueError:
        pass
good = World(seed=7, difficulty=Difficulty.EASY, populate=False)
good.add_unit("infantry", Faction.PLAYER, 200, 200)
good.add_unit("cavalry", Faction.ENEMY, 800, 200)
rebuilt = World.from_dict(good.to_dict())
assert rebuilt.units, "合法快照应重建并保留兵团"
print("scenario-validation OK")

# ---- 7. 过量伤害封顶：统计与战功不能把致死溢出算入 ----
w7 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
a7 = w7.add_unit("artillery", Faction.PLAYER, 200, 300)
b7 = w7.add_unit("infantry", Faction.ENEMY, 210, 300)
b7.hp = 1.0
w7._fire_volley(a7, b7, a7.spec.attack_interval)
assert b7.hp == 0.0, f"目标应被击杀 hp={b7.hp}"
assert a7.dmg_dealt == 1.0, f"战绩应封顶在剩余血量 1.0, got {a7.dmg_dealt}"
assert w7.stats.side[Faction.PLAYER].dmg_dealt == 1.0
print("overkill-cap OK")

# ---- 8. 托管接管：废弃玩家遗留的巡逻/攻移/编队状态；手动下令解除托管 ----
w8 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p8 = w8.add_unit("infantry", Faction.PLAYER, 200, 200)
w8.add_unit("infantry", Faction.ENEMY, 900, 200)
w8.issue_patrol([p8], [(p8.x + 100, p8.y)])
assert p8.order == "patrol" and p8.patrol_route
w8.set_auto([p8], True)
assert p8.auto and p8.order == "move"
assert not p8.patrol_route and not p8.path, "托管应清空巡逻状态"
assert not p8.formation_lock
w8.issue_move([p8], p8.x + 80, p8.y)
assert not p8.auto, "手动下令应解除托管"
print("autopilot-reset OK")

# ---- 9. 士气溃逃触发顺序：先判崩溃再回复，避免本帧被抬回而永久跳过 ----
w9 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p9 = w9.add_unit("infantry", Faction.PLAYER, 400, 400)
w9.add_unit("infantry", Faction.ENEMY, 430, 400)   # 射程内，持续敌情
p9.morale = 0.24                                # 略低于崩溃线 0.25
p9.update_morale(0.05, foe_near=True)
assert p9.routing, "低于崩溃线应先触发溃逃"
p9.morale = 1.0
p9.update_morale(0.05, foe_near=False)
assert not p9.routing, "士气回满应重整归队"
print("morale-routing-order OK")

# ---- 10. 全水地图：吸附/寻路不得抛异常或返回非法值 ----
g10 = GameMap(3, 3, 1)
g10.fill(Terrain.LAKE)
res = g10.nearest_passable(8, 8)
assert isinstance(res, tuple) and len(res) == 2, "无通行格时应安全返回坐标"
assert g10.find_path(8, 8, 24, 24) == [], "全水无路应返回空列表"
print("all-water-safe OK")

# ---- 11. 战争迷雾：视野外敌军不可见，不会被标记/暴露 ----
w11 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
pl11 = w11.add_unit("infantry", Faction.PLAYER, 200, 200)
en11 = w11.add_unit("infantry", Faction.ENEMY, 1500, 200)   # 远超出视野
w11.refresh_field()
assert not w11.visible_to(Faction.PLAYER, en11), "远处敌军应不可见"
assert w11.visible_to(Faction.PLAYER, pl11), "我军自身应可见"
print("fog-visibility OK")

# ---- 12. 进入射程即停步：远程不再边走边打走进近战圈 ----
w12 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
a12 = w12.add_unit("archer", Faction.PLAYER, 200, 400)
e12 = w12.add_unit("infantry", Faction.ENEMY, 200 + 110, 400)  # 射程 122 内、0.7R 外
w12.issue_move([a12], a12.x + 400, a12.y)
x0 = a12.x
for _ in range(15):
    w12.update(0.1)
assert a12.target_uid == e12.uid, "应锁定射程内敌军"
assert not a12.path, "进入射程应立刻停步行军"
assert a12.x < e12.x - 20, "弓兵不应继续逼近敌阵"
assert a12.x - x0 < 40, "停步后位移应很小"
print("stop-in-range OK")

# ---- 13. 攻移续程从当前位置重接，不橡皮筋回旧航点 ----
w13 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p13 = w13.add_unit("infantry", Faction.PLAYER, 200, 200)
e13 = w13.add_unit("infantry", Faction.ENEMY, 230, 200)
w13.issue_attack_move([p13], [(p13.x + 300, p13.y), (p13.x + 300, p13.y + 200)])
for _ in range(25):
    w13.update(0.1)
assert p13.travel_path, "接敌应暂存剩余路线"
# 模拟推挤：单位被撞到远处
p13.x, p13.y = 100.0, 100.0
old_first = p13.travel_path[0]
e13.hp = 0.0
for _ in range(5):
    w13.update(0.1)
assert p13.path, "灭敌后应续程"
# 续程路线应靠近当前位置，而非硬拉回旧 travel 首点
d_new = math.hypot(p13.path[0][0] - p13.x, p13.path[0][1] - p13.y)
d_old = math.hypot(old_first[0] - p13.x, old_first[1] - p13.y)
assert d_new <= d_old + 1.0, f"应从当前位置重接 d_new={d_new} d_old={d_old}"
print("resume-from-here OK")

# ---- 14. issue_path 单点退化为 issue_move，给出有效路线 ----
w14 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p14 = w14.add_unit("infantry", Faction.PLAYER, 200, 200)
w14.issue_path([p14], [(500.0, 200.0)])
assert p14.path, "单点 issue_path 应给出行军路线"
assert p14.path[-1][0] > 400, "应朝目标点推进"
print("issue-path-single OK")

# ---- 15. 编队在攻移接敌期间保持锁定 ----
w15 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
c15 = w15.add_unit("cavalry", Faction.PLAYER, 200, 200)
i15 = w15.add_unit("infantry", Faction.PLAYER, 220, 200)
e15 = w15.add_unit("infantry", Faction.ENEMY, 250, 200)
w15.issue_attack_move([c15, i15], [(c15.x + 40, c15.y)], formation=True)
for _ in range(20):
    w15.update(0.1)
assert c15.formation_lock and i15.formation_lock, "接敌交战中编队不应拆开"
assert c15.formation_id == i15.formation_id != 0
print("formation-in-combat OK")

# ---- 16. 溃兵影响力折三成；文字种子可解析 ----
from rts.influence import _presence
from rts.terrain import GameMap
w16 = World(seed=1, populate=False)
u16 = w16.add_unit("infantry", Faction.PLAYER, 200, 200)
full = _presence(u16)
u16.routing = True
assert abs(_presence(u16) - full * 0.3) < 1e-9, "溃兵影响力应折三成"
g16 = GameMap(8, 8, seed="battlefield")
assert isinstance(g16.seed, int) and 0 <= g16.seed < (1 << 30)
print("presence-seed OK")

# ---- 17. build_route 单段不可达不丢弃后续笔迹 ----
from rts.units import build_route
from rts.terrain import Terrain as T
g17 = GameMap(20, 20, 1)
g17.fill(T.PLAIN)
# 在中间挖一条不可逾越的湖带（留上下通道），使水平直穿失败但后续点可绕
for y in range(6, 14):
    for x in range(8, 12):
        g17.set_terrain(x, y, T.LAKE)
start = (3.5 * TILE, 10.5 * TILE)
# 第一目标在湖心（不可达吸附后仍可能难直达），第二目标在湖对岸可通行
pts = [(10.5 * TILE, 10.5 * TILE), (16.5 * TILE, 10.5 * TILE)]
route = build_route(start, pts, g17)
assert route, "后续可走笔迹应保留"
assert route[-1][0] > 14 * TILE, "应最终抵达对岸附近"
print("build-route-skip-gap OK")

# ---- 18. 指定攻击：锁定目标、追击、灭敌后解除 ----
w18 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p18 = w18.add_unit("infantry", Faction.PLAYER, 200, 300)
e18 = w18.add_unit("infantry", Faction.ENEMY, 500, 300)
# 干扰目标：若未锁定会优先打更近的
decoy = w18.add_unit("infantry", Faction.ENEMY, 250, 300)
w18.issue_attack([p18], e18)
assert p18.order == "attack" and p18.target_uid == e18.uid
assert p18.path, "目标在射程外应给出追击路线"
for _ in range(40):
    w18.update(0.1)
    if p18.target_uid != e18.uid and e18.alive:
        assert False, f"指定攻击期间不得改锁到诱饵 decoy={decoy.uid} got={p18.target_uid}"
# 目标仍存活时应保持锁定
if e18.alive:
    assert p18.order == "attack" and p18.target_uid == e18.uid
e18.hp = 0.0
for _ in range(10):
    w18.update(0.1)
assert p18.order == "move" or p18.target_uid != e18.uid, "目标阵亡后应解除指定攻击"
# 禁止点名友军
ally = w18.add_unit("infantry", Faction.PLAYER, 280, 300)
w18.issue_attack([p18], ally)
assert p18.order != "attack" or p18.target_uid != ally.uid, "不得指定攻击友军"
print("issue-attack OK")

# ---- 19. 玩家敌情记忆：可见刷新、迷雾外残留、过期遗忘 ----
from rts.ai import SPOT_MEMORY_TTL
w19 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p19 = w19.add_unit("infantry", Faction.PLAYER, 200, 300)
e19 = w19.add_unit("infantry", Faction.ENEMY, 220, 300)   # 贴身 → 可见
w19.update(0.05)
assert e19.uid in w19._spot_known, "可见敌军应写入玩家记忆"
# 把敌军挪到视野外（玩家步兵 vision≈210）
e19.x, e19.y = 2000, 300
w19._grid_dirty = True
w19.update(0.05)
ghosts = w19.player_memory_ghosts()
assert any(g[3] == e19.uid for g in ghosts), "不可见后应出现记忆残影"
assert e19.uid in w19._spot_known
# 位置应仍是最后可见点附近，而非 2000
gx, gy, _age, _ = next(g for g in ghosts if g[3] == e19.uid)
assert abs(gx - 220) < 5 and abs(gy - 300) < 5, "记忆点应为最后可见位置"
# 过期
w19.elapsed += SPOT_MEMORY_TTL + 0.5
w19._refresh_player_spot_memory()
assert e19.uid not in w19._spot_known, "过期应遗忘"
assert not any(g[3] == e19.uid for g in w19.player_memory_ghosts())
# 阵亡遗忘
e19b = w19.add_unit("infantry", Faction.ENEMY, 210, 300)
w19.update(0.05)
assert e19b.uid in w19._spot_known
e19b.hp = 0.0
w19.update(0.05)
assert e19b.uid not in w19._spot_known, "阵亡应遗忘"
print("player-spot-memory OK")

# ---- 20. 指定攻击站位：远程分槽不挤同一点 ----
w20 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
a1 = w20.add_unit("archer", Faction.PLAYER, 200, 280)
a2 = w20.add_unit("archer", Faction.PLAYER, 200, 320)
e20 = w20.add_unit("infantry", Faction.ENEMY, 500, 300)
w20.issue_attack([a1, a2], e20)
assert a1.order == "attack" and a2.order == "attack"
# 终点应靠近各自射程环，且两兵目标点明显分离
g1 = a1._path_goal or (a1.path[-1] if a1.path else (a1.x, a1.y))
g2 = a2._path_goal or (a2.path[-1] if a2.path else (a2.x, a2.y))
d1 = math.hypot(g1[0] - e20.x, g1[1] - e20.y)
d2 = math.hypot(g2[0] - e20.x, g2[1] - e20.y)
sep = math.hypot(g1[0] - g2[0], g1[1] - g2[1])
ideal = a1.spec.attack_range * 0.88
assert abs(d1 - ideal) < TILE * 3, f"弓1 hold 距应近射程环 got {d1}"
assert abs(d2 - ideal) < TILE * 3, f"弓2 hold 距应近射程环 got {d2}"
assert sep > TILE * 2, f"两弓 hold 应分离 got sep={sep}"
print("attack-standoff OK")

# ---- 21. 战术撤退：向出生侧、retreating ----
w21 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p21 = w21.add_unit("infantry", Faction.PLAYER, 800, 400)
x0 = p21.x
w21.issue_retreat([p21])
assert p21.retreating and p21.path, "撤退应上路"
goal = p21._path_goal or p21.path[-1]
assert goal[0] < x0, "我军撤退应向西（出生侧）"
print("issue-retreat OK")

# ---- 22. 战斗预览：克制 / TTK / 齐射区间 ----
w22 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
arch = w22.add_unit("archer", Faction.PLAYER, 200, 300)
inf = w22.add_unit("infantry", Faction.ENEMY, 250, 300)
prev = w22.combat_preview([arch], inf)
assert "克制" in prev["counter"], prev
assert prev["volley_lo"] < prev["volley_hi"]
assert 0 < prev["ttk"] < 500
# 期望齐射与 estimate 一致（±jitter 包住）
est = w22.estimate_volley_damage(arch, inf)
assert prev["volley_lo"] <= est <= prev["volley_hi"]
print("combat-preview OK")

# ---- 23. 简单难度记忆 TTL 更短 ----
from rts.ai import (DIFFICULTIES, SPOT_MEMORY_TTL, SPOT_MEMORY_TTL_EASY,
                    Commander)
assert SPOT_MEMORY_TTL_EASY < SPOT_MEMORY_TTL
ce = Commander(Faction.ENEMY, DIFFICULTIES[Difficulty.EASY])
ch = Commander(Faction.ENEMY, DIFFICULTIES[Difficulty.HARD])
assert ce._memory_ttl() == SPOT_MEMORY_TTL_EASY
assert ch._memory_ttl() == SPOT_MEMORY_TTL
print("ai-memory-ttl OK")

# ---- 24. Shift 指令队列：移动完成后执行下一项 ----
w24 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
p24 = w24.add_unit("infantry", Faction.PLAYER, 200, 300)
w24.issue_move([p24], 280, 300)
assert p24.path and not p24.order_queue
w24.issue_move([p24], 400, 300, queue=True)
assert len(p24.order_queue) == 1 and p24.order_queue[0]["kind"] == "move"
# 走到第一目标附近
for _ in range(80):
    w24.update(0.1)
    if not p24.path and not p24.order_queue:
        break
    if not p24.path and p24.order_queue:
        w24.update(0.1)   # 再推进一帧弹出队列
        break
# 队列应已弹出并开始第二段
assert not p24.order_queue or p24.path, "队列应被消费或正在执行"
# 再走完
for _ in range(120):
    w24.update(0.1)
assert not p24.order_queue, "两段移动后队列应空"
assert abs(p24.x - 400) < 40, f"应接近第二目标 x={p24.x}"
# 非 queue 应清空队列
w24.issue_move([p24], 450, 300, queue=True)
w24.issue_move([p24], 500, 300)          # 覆盖
assert not p24.order_queue, "非 Shift 下令应清空队列"
# 排队攻击
e24 = w24.add_unit("infantry", Faction.ENEMY, 700, 300)
w24.issue_move([p24], 520, 300)
w24.issue_attack([p24], e24, queue=True)
assert p24.order_queue and p24.order_queue[0]["kind"] == "attack"
print("order-queue OK")

# ---- 25. 军力时间线采样 + 关键事件 ----
from rts.world import army_strength as _as
w25 = World(seed=1, difficulty=Difficulty.EASY, populate=False)
assert w25.stats.force_history, "开局应有 t=0 采样"
p25 = w25.add_unit("infantry", Faction.PLAYER, 200, 300)
e25 = w25.add_unit("infantry", Faction.ENEMY, 250, 300)
w25.record_force_sample(force=True)
assert len(w25.stats.force_history) >= 2
# 推进超过采样间隔应自动追加
n0 = len(w25.stats.force_history)
for _ in range(40):
    w25.update(0.1)
assert len(w25.stats.force_history) > n0, "update 应追加军力采样"
# 交火应写入时间线
p25.x, p25.y = e25.x - 20, e25.y
p25.target_uid = e25.uid
p25.attack_cd = 0.0
w25._resolve_combat(0.05)
assert any(k == "first_blood" for _, k, _ in w25.stats.timeline) or w25.stats.first_blood is not None
# army_strength 与采样一致量级
ps = _as(w25.units_of(Faction.PLAYER))
assert ps > 0
print("force-history OK")

print("ALL REGRESSION TESTS PASSED")
