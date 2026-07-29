"""游戏世界：持有地图与所有兵团，负责推进模拟（移动、索敌、战斗、AI、势力场）。"""
from __future__ import annotations

import json
import math
import random

from dataclasses import dataclass, field

from .ai import (AUTOPILOT_PARAMS, DIFFICULTIES, SPOT_MEMORY_TTL, Commander,
                 Difficulty)
from .influence import compute_field
from .spatial_hash import SpatialHash
from .terrain import TERRAIN_INFO, TILE, GameMap, Terrain
from .units import (COUNTER_MULT, FACTION_NAME, UNIT_TYPES, VET_DMG,
                    Faction, Unit, artillery_target_value, build_route)

INFLUENCE_INTERVAL = 0.4     # 势力场重算间隔（秒）
SCENARIO_VERSION = 1         # 剧本存档格式版本
PATHS_PER_FRAME = 12         # 每帧最多完整 A*/build_route 次数（AI 追击分摊尖峰）

# ---------------------------------------------------------------- 战斗修正
HIGH_GROUND_SCALE = 0.6      # 高差伤害系数：dmg *= 1 + 0.6*(攻方海拔-守方海拔)
FLANK_REAR_MULT = 1.30       # 背击（攻方位于守方朝向后方 >120°）
FLANK_SIDE_MULT = 1.12       # 侧击（>60°）
CHARGE_MULT = 1.30           # 骑兵冲势：带着冲劲接敌（刚停下不久）的齐射加成
VOLLEY_JITTER = 0.10         # 齐射伤害 ±10% 浮动

# ---------------------------------------------------------------- 士气冲击
MORALE_DMG_SCALE = 1.15      # 掉血占比 → 士气损失的换算系数
MORALE_FLANK_BONUS = 1.6     # 被侧击/背击时士气损失放大
MORALE_CHARGE_SHAKE = 0.06   # 被骑兵冲锋命中额外动摇
MORALE_ALLY_FALL = 0.16      # 目睹友军覆灭的基准动摇（随距离衰减）
MORALE_ALLY_FALL_R = 220.0   # 「目睹」半径（像素）
ROUT_LEG = TILE * 7          # 溃兵每段逃跑路径的长度

# ---------------------------------------------------------------- 侦察
FOREST_CONCEAL = 0.60        # 目标藏身森林时，发现距离打的折扣
SPOT_GRACE = 1.1             # 发现判定的宽限系数（略大于视野，避免边缘闪烁）

# ---------------------------------------------------------------- 指定攻击站位 / 战术撤退
ATTACK_RANGED_STANDOFF = 0.88   # 远程理想距离 = 射程 × 此值（最大射程环内侧）
ATTACK_MELEE_STANDOFF = 0.55    # 近战理想距离 = max(两半径和, 射程 × 此值)
ATTACK_HOLD_SLACK = 0.12        # 已在理想距离 ±此比例·射程 内则站住
RETREAT_DIST = TILE * 8         # 玩家战术撤退向出生侧的距离

# ---------------------------------------------------------------- 空间哈希粗筛上界
# 按兵种表 / 地形表推出的全局最大值，启动时算一次，供近邻查询定圆域半径。
MAX_ATTACK_RANGE = max(t.attack_range for t in UNIT_TYPES.values())
MAX_UNIT_RADIUS = max(t.radius for t in UNIT_TYPES.values())
MAX_SPOT_RANGE = (max(t.vision for t in UNIT_TYPES.values())
                  * max(i.vision for i in TERRAIN_INFO.values()) * SPOT_GRACE)

# 势力场脏检查的量化粒度：位置 8px、血量 1/32——低于该幅度的变化不触发重算
FIELD_SIG_POS = 8.0
FIELD_SIG_HP = 32.0

# 军力时间线采样
FORCE_SAMPLE_INTERVAL = 0.5      # 每隔多少模拟秒记一笔军力
FORCE_HISTORY_MAX = 2400         # 约 20 分钟 @0.5s；超出丢最旧
TIMELINE_MAX = 200               # 关键事件刻度条数上限


def army_strength(units) -> float:
    """一方有效战力：Σ 当前HP × (1+老练输出加成) × 难度输出系数；溃兵折三成。"""
    s = 0.0
    for u in units:
        w = u.hp * (1.0 + 0.08 * u.vet_level) * u.dmg_mult
        s += w * 0.3 if u.routing else w
    return s


@dataclass
class SideStats:
    """一方的累计战报数字。"""
    kills: int = 0                 # 击破敌团数
    losses: int = 0                # 损失己团数
    dmg_dealt: float = 0.0         # 造成伤害
    dmg_taken: float = 0.0         # 承受伤害
    losses_by_type: dict[str, int] = field(default_factory=dict)


class BattleStats:
    """整场战斗的统计：双方汇总 + 阵亡名录 + 军力时间线（供折线/结算）。"""

    def __init__(self) -> None:
        self.side = {Faction.PLAYER: SideStats(), Faction.ENEMY: SideStats()}
        self.first_blood: float | None = None    # 首次伤害发生时刻（秒）
        self.volleys = 0                         # 齐射总次数
        # 阵亡单位的最终战绩：(faction, "步兵团#3", kills, dmg_dealt)
        self.fallen: list[tuple[Faction, str, int, float]] = []
        # 军力采样：(elapsed, 我军战力, 敌军战力)
        self.force_history: list[tuple[float, float, float]] = []
        # 时间线事件：(elapsed, kind, text)  kind=first_blood/kill/rout/rally/escape
        self.timeline: list[tuple[float, str, str]] = []


class World:
    def __init__(self, map_width: int = 160, map_height: int = 120,
                 seed: int | None = None, difficulty: Difficulty = Difficulty.EASY,
                 populate: bool = True):
        self.map = GameMap(map_width, map_height, seed)
        self.difficulty = difficulty
        self.params = DIFFICULTIES[difficulty]
        self.units: list[Unit] = []
        self._next_uid = 1
        self._next_fid = 1            # 编队号自增（formation_id）
        self.elapsed = 0.0
        self.events: list[str] = []
        self.event_seq = 0            # 累计写入的战报总数（events 只保留末 200 条）
        self.commander = Commander(Faction.ENEMY, self.params)
        # 我方托管指挥官：只接管 u.auto 的兵团
        self.autopilot = Commander(Faction.PLAYER, AUTOPILOT_PARAMS, only_auto=True)
        self.stats = BattleStats()
        self._rng = random.Random(self.map.seed ^ 0x77aa11)  # 齐射浮动等战斗随机
        self.field = None                # 最近一次势力场结果
        self.field_version = 0           # 每次重算 +1，供渲染层缓存失效
        self._field_cd = 0.0
        self._field_sig = None           # 上次重算时的战场签名（脏检查用）
        self._grid = SpatialHash()       # 空间哈希：近邻查询 O(k)，代替全体扫描
        self._grid_dirty = True          # 单位增删/移动后置脏，查询时惰性重建
        self._path_budget = 0            # 本帧剩余 A* 额度，削尖峰
        # 玩家视野记忆：曾见敌军的最后位置（小地图/主图淡影），与 AI._known 独立
        self._spot_known: dict[int, tuple[float, float]] = {}
        self._spot_known_t: dict[int, float] = {}
        if populate:                     # 编辑器建的是空世界，自行摆兵
            self._spawn_armies()
        self.refresh_field()
        self.record_force_sample(force=True)   # t=0 起点

    # ------------------------------------------------------------ 建立初始部队
    def _new_uid(self) -> int:
        uid = self._next_uid
        self._next_uid += 1
        return uid

    def add_unit(self, type_key: str, faction: Faction, x: float, y: float,
                 hp_scale: float = 1.0, dmg_mult: float = 1.0) -> Unit:
        if type_key not in UNIT_TYPES:
            raise ValueError(f"未知兵种: {type_key}")
        faction = Faction(faction)
        if not all(math.isfinite(v) for v in (x, y, hp_scale, dmg_mult)):
            raise ValueError("兵团坐标与倍率必须是有限数值")
        if hp_scale <= 0.0 or dmg_mult < 0.0:
            raise ValueError("hp_scale 必须大于 0，dmg_mult 不能为负数")
        x, y = self.map.nearest_passable(x, y)
        if not self.map.passable(x, y):
            raise ValueError("地图上没有可供兵团站立的通行地形")
        unit = Unit(self._new_uid(), type_key, faction, x, y,
                    hp_scale=hp_scale, dmg_mult=dmg_mult)
        unit.hp = unit.max_hp
        self.units.append(unit)
        self._grid_dirty = True
        return unit

    def remove_unit(self, unit: Unit) -> bool:
        """从世界移除一个兵团（供地图编辑器擦除）。返回是否移除成功。"""
        try:
            self.units.remove(unit)
            self._grid_dirty = True
            return True
        except ValueError:
            return False

    def clear_units(self) -> None:
        """清空全部兵团（供编辑器「清兵」）。"""
        self.units.clear()
        self._grid_dirty = True

    # ------------------------------------------------------------ 空间哈希
    def _ensure_grid(self) -> SpatialHash:
        """返回最新的空间哈希；脏了才重建（O(n)），一帧至多重建两三次。"""
        if self._grid_dirty:
            self._grid.rebuild(self.units)
            self._grid_dirty = False
        return self._grid

    def _spawn_armies(self) -> None:
        rng = random.Random(self.map.seed ^ 0x1f2e3d)
        base = ["infantry", "infantry", "infantry", "cavalry", "cavalry",
                "archer", "archer", "artillery"]
        extra_pool = ["infantry", "archer", "cavalry", "artillery"]
        w, h = self.map.pixel_width, self.map.pixel_height
        for faction, base_x in ((Faction.PLAYER, w * 0.12), (Faction.ENEMY, w * 0.88)):
            comp = list(base)
            hp_s, dmg_s = 1.0, 1.0
            if faction is Faction.ENEMY:
                comp += extra_pool[:self.params.extra_units]
                hp_s, dmg_s = self.params.hp_mult, self.params.dmg_mult
            for i, key in enumerate(comp):
                col, row = divmod(i, 4)
                sign = 1 if faction is Faction.PLAYER else -1
                x = base_x + (col - 0.5) * TILE * 3 * sign
                y = h * 0.5 + (row - 1.5) * TILE * 4
                x += rng.uniform(-TILE, TILE)
                y += rng.uniform(-TILE, TILE)
                self.add_unit(key, faction, x, y, hp_scale=hp_s, dmg_mult=dmg_s)


    # ------------------------------------------------------------ 查询
    def units_of(self, faction: Faction) -> list[Unit]:
        return [u for u in self.units if u.faction == faction and u.alive]

    def unit_by_uid(self, uid: int | None) -> Unit | None:
        if uid is None:
            return None
        for u in self.units:
            if u.uid == uid and u.alive:
                return u
        return None

    def unit_at(self, wx: float, wy: float, slack: float = 6.0) -> Unit | None:
        """返回点击位置下的兵团，取最近的一个（只翻附近几个空间哈希桶）。"""
        best, best_d = None, 1e18
        for u in self._ensure_grid().query(wx, wy, MAX_UNIT_RADIUS + slack):
            if not u.alive:
                continue
            d = math.hypot(u.x - wx, u.y - wy)
            if d <= u.spec.radius + slack and d < best_d:
                best, best_d = u, d
        return best

    def units_in_rect(self, x0: float, y0: float, x1: float, y1: float,
                      faction: Faction | None = None) -> list[Unit]:
        lo_x, hi_x = min(x0, x1), max(x0, x1)
        lo_y, hi_y = min(y0, y1), max(y0, y1)
        return [u for u in self.units
                if u.alive and (faction is None or u.faction is faction)
                and lo_x <= u.x <= hi_x and lo_y <= u.y <= hi_y]

    @property
    def selected(self) -> list[Unit]:
        return [u for u in self.units if u.alive and u.selected]

    # ------------------------------------------------------------ 侦察 / 视野
    def spot_range(self, viewer: Unit, target: Unit) -> float:
        """viewer 能发现 target 的最远距离：己方视野受所在地形加成，
        目标藏身森林则大打折扣（近了才能看见）。"""
        info = TERRAIN_INFO[self.map.terrain_at(viewer.x, viewer.y)]
        r = viewer.spec.vision * info.vision * SPOT_GRACE
        if self.map.terrain_at(target.x, target.y) is Terrain.FOREST:
            r *= FOREST_CONCEAL
        return r

    def visible_to(self, faction: Faction, target: Unit) -> bool:
        """target 是否被 faction 的任一兵团发现（战争迷雾判定）。

        只查 target 周围最大侦察半径内的桶：任何能看见它的兵团必在此圆内。
        """
        if target.faction is faction:
            return True
        return any(u.alive and u.faction is faction
                   and u.distance_to(target) <= self.spot_range(u, target)
                   for u in self._ensure_grid().query(target.x, target.y,
                                                      MAX_SPOT_RANGE))

    # ------------------------------------------------------------ 指令
    def clear_selection(self) -> None:
        for u in self.units:
            u.selected = False

    def _reset_order(self, u: Unit) -> None:
        """清空一个兵团的行军指令状态（order / 暂存路线 / 巡逻 / 队列 / 编队）。"""
        u.order = "move"
        u.travel_path.clear()
        u.patrol_route.clear()
        u.order_queue.clear()
        u.formation_lock = False
        u.formation_id = 0
        u._speed_cap = 0.0
        u._path_goal = None

    def _apply_formation(self, units: list[Unit], formation: bool) -> None:
        """编队锁定：给这批兵分配同一个编队号，update 时据此同步限速。"""
        if not formation:
            return
        fid = self._next_fid
        self._next_fid += 1
        for u in units:
            u.formation_lock = True
            u.formation_id = fid

    def _is_order_busy(self, u: Unit) -> bool:
        """当前指令是否尚未完成（队列需等待）。巡逻视为持续忙碌。"""
        if u.path or u.travel_path:
            return True
        if u.retreating:
            return True
        if u.order == "attack" and self.unit_by_uid(u.target_uid) is not None:
            return True
        if u.order == "patrol" and u.patrol_route:
            return True
        return False

    def _enqueue_or_start(self, u: Unit, entry: dict, queue: bool) -> None:
        """queue 且正忙 → 追加；否则立即执行（并清空旧队列）。"""
        u.auto = False
        if queue and self._is_order_busy(u):
            u.order_queue.append(entry)
            return
        if not queue:
            u.order_queue.clear()
        self._apply_order_entry(u, entry)

    def _apply_order_entry(self, u: Unit, entry: dict) -> None:
        """执行单条指令条目（不清空 order_queue 剩余项）。"""
        kind = entry.get("kind", "move")
        u.travel_path.clear()
        u.patrol_route.clear()
        u.retreating = False
        u.auto = False
        u.formation_lock = False
        u.formation_id = 0
        u._speed_cap = 0.0
        if kind == "move":
            u.order = "move"
            u.target_uid = None
            u.set_path([(entry["x"], entry["y"])], self.map)
        elif kind == "path":
            u.order = "move"
            u.target_uid = None
            pts = list(entry.get("points") or [])
            if len(pts) < 2:
                if pts:
                    u.set_path([pts[0]], self.map)
                else:
                    u.path.clear()
                    u._path_goal = None
            else:
                u.path = build_route((u.x, u.y), pts, self.map)
                u._path_goal = pts[-1]
        elif kind == "attack":
            t = self.unit_by_uid(entry.get("target_uid"))
            if t is None or not t.alive:
                u.order = "move"
                u.target_uid = None
                u.path.clear()
                u._path_goal = None
                return
            u.order = "attack"
            u.target_uid = t.uid
            self._path_attackers_to_holds([u], t)
        elif kind == "attack_move":
            u.order = "attack_move"
            u.target_uid = None
            pts = list(entry.get("points") or [])
            if not pts:
                u.order = "move"
                u.path.clear()
                return
            if len(pts) < 2:
                u.set_path([pts[0]], self.map)
            else:
                u.path = build_route((u.x, u.y), pts, self.map)
                u._path_goal = pts[-1]
        elif kind == "patrol":
            u.order = "patrol"
            u.target_uid = None
            u.patrol_fwd = True
            pts = list(entry.get("points") or [])
            if not pts:
                u.order = "move"
                u.path.clear()
                return
            if len(pts) < 2:
                route = build_route((u.x, u.y), pts, self.map) or list(pts)
                u.patrol_route = [(u.x, u.y)] + route
            else:
                u.patrol_route = list(build_route((u.x, u.y), pts, self.map) or pts)
            u.path = list(u.patrol_route)
            u._path_goal = u.patrol_route[-1] if u.patrol_route else None
        else:
            u.order = "move"
            u.target_uid = None
            u.path.clear()

    def _advance_order_queues(self) -> None:
        """当前指令完成后弹出队列下一项。"""
        for u in self.units:
            if not u.alive or u.routing or not u.order_queue:
                continue
            if self._is_order_busy(u):
                continue
            entry = u.order_queue.pop(0)
            self._apply_order_entry(u, entry)

    def issue_path(self, units: list[Unit], points: list[tuple[float, float]],
                   formation: bool = False, queue: bool = False) -> None:
        """下达一条手绘行军路线。

        笔触被视为「行进形状」：把它平移到每个兵团脚下，各自从当前位置起步
        照着笔迹走。因此没有任何「先赶到起笔点」的绕行，队形也天然保持。
        formation=True 时锁定编队：编队内多兵以最慢者当前地形速度同步推进，保持间距不脱节。
        queue=True（Shift）时追加到指令队列，不打断当前行动。
        溃逃中的兵团听不进指挥，自动跳过。
        """
        units = [u for u in units if u.alive and not u.routing]
        if not units or not points:
            return
        # 单点笔迹平移后会落在脚下 → 退化为向该点收拢（与 issue_move 一致）
        if len(points) < 2:
            self.issue_move(units, points[0][0], points[0][1],
                            formation=formation, queue=queue)
            return
        hx, hy = points[0]
        immediate: list[Unit] = []
        for u in units:
            dx, dy = u.x - hx, u.y - hy
            shifted = [(x + dx, y + dy) for x, y in points]
            entry = {"kind": "path", "points": shifted}
            if queue and self._is_order_busy(u):
                u.order_queue.append(entry)
                u.auto = False
            else:
                if not queue:
                    u.order_queue.clear()
                u.retreating = False
                u.auto = False
                u.order = "move"
                u.travel_path.clear()
                u.patrol_route.clear()
                u.target_uid = None
                u.path = build_route((u.x, u.y), shifted, self.map)
                u._path_goal = shifted[-1]
                immediate.append(u)
        if immediate and formation and not queue:
            self._apply_formation(immediate, True)

    def issue_move(self, units: list[Unit], wx: float, wy: float,
                   formation: bool = False, queue: bool = False) -> None:
        """下达一个目标点（右键单击）：整队向该点收拢，并保持相对队形。

        formation=True 时锁定编队同步限速。queue=True 时追加到队列。
        """
        units = [u for u in units if u.alive and not u.routing]
        if not units:
            return
        cx = sum(u.x for u in units) / len(units)
        cy = sum(u.y for u in units) / len(units)
        immediate: list[Unit] = []
        for u in units:
            entry = {"kind": "move",
                     "x": wx + (u.x - cx), "y": wy + (u.y - cy)}
            if queue and self._is_order_busy(u):
                u.order_queue.append(entry)
                u.auto = False
            else:
                if not queue:
                    u.order_queue.clear()
                self._apply_order_entry(u, entry)
                immediate.append(u)
        if immediate and formation and not queue:
            self._apply_formation(immediate, True)

    def issue_attack(self, units: list[Unit], target: Unit,
                     formation: bool = False, queue: bool = False) -> None:
        """指定攻击：锁定目标追击，直至歼灭或另行下令。

        右键点在可见敌军上时下达。与攻击移动不同——不沿路线扫荡，只咬住这一团；
        目标移出射程会持续追击，期间不改索其它目标。
        站位：远程落在最大射程环内侧分槽，近战贴脸分槽，避免多团挤同一点。
        queue=True 时追加到队列（当前任务完成后执行）。
        """
        units = [u for u in units if u.alive and not u.routing]
        if not units or target is None or not target.alive:
            return
        if any(u.faction is target.faction for u in units):
            return                      # 禁止点名友军
        immediate: list[Unit] = []
        for u in units:
            entry = {"kind": "attack", "target_uid": target.uid}
            if queue and self._is_order_busy(u):
                u.order_queue.append(entry)
                u.auto = False
            else:
                if not queue:
                    u.order_queue.clear()
                u.retreating = False
                u.auto = False
                u.travel_path.clear()
                u.patrol_route.clear()
                u.order = "attack"
                u.target_uid = target.uid
                immediate.append(u)
        if immediate:
            self._path_attackers_to_holds(immediate, target)
            if formation and not queue:
                self._apply_formation(immediate, True)

    def issue_retreat(self, units: list[Unit]) -> None:
        """战术撤退：向己方出生侧短撤一段，期间不重新接敌（retreating）。

        我军向西、敌军向东（与刷兵侧一致）。解除托管与指定攻击等行军指令。
        """
        units = [u for u in units if u.alive and not u.routing]
        if not units:
            return
        for u in units:
            self._reset_order(u)
            u.target_uid = None
            u.auto = False
            u.retreating = True
            home = -1.0 if u.faction is Faction.PLAYER else 1.0
            fx = u.x + home * RETREAT_DIST
            fy = u.y
            fx = max(TILE, min(self.map.pixel_width - TILE, fx))
            fy = max(TILE, min(self.map.pixel_height - TILE, fy))
            u.set_path([(fx, fy)], self.map)

    # ------------------------------------------------------------ 指定攻击站位
    def _attack_cohort(self, target: Unit, role: str) -> list[Unit]:
        """锁定同一目标、同角色、仍在执行指定攻击的存活单位（按 uid 稳定排序）。"""
        out = [u for u in self.units
               if (u.alive and not u.routing and u.order == "attack"
                   and u.target_uid == target.uid and u.role == role)]
        out.sort(key=lambda u: u.uid)
        return out

    def _attack_hold_point(self, u: Unit, target: Unit,
                           slot: int, n: int) -> tuple[float, float]:
        """远程最大射程环 / 近战贴脸环上的分槽落点。"""
        if u.role == "ranged":
            ideal = u.spec.attack_range * ATTACK_RANGED_STANDOFF
        else:
            ideal = max(u.spec.radius + target.spec.radius,
                        u.spec.attack_range * ATTACK_MELEE_STANDOFF)
        # 基准角：从目标看向攻击方当前方位（单兵即「自己→目标」反方向）
        base = math.atan2(u.y - target.y, u.x - target.x)
        if n <= 1:
            ang = base
        else:
            # 以 base 为扇区中心均分，避免全挤在同一射线
            span = min(math.pi * 1.4, math.pi * 0.35 * n)
            ang = base - span * 0.5 + span * (slot + 0.5) / n
        hx = target.x + math.cos(ang) * ideal
        hy = target.y + math.sin(ang) * ideal
        hx = max(TILE, min(self.map.pixel_width - TILE, hx))
        hy = max(TILE, min(self.map.pixel_height - TILE, hy))
        return self.map.nearest_passable(hx, hy)

    def _path_attackers_to_holds(self, units: list[Unit], target: Unit) -> None:
        """按角色分槽，给每个攻击者一条通向 hold 点的路（已在环上则清 path）。"""
        by_role: dict[str, list[Unit]] = {"ranged": [], "melee": []}
        for u in units:
            by_role.setdefault(u.role, []).append(u)
        for role, group in by_role.items():
            group.sort(key=lambda u: u.uid)
            n = len(group)
            for i, u in enumerate(group):
                hold = self._attack_hold_point(u, target, i, n)
                dist = u.distance_to(target)
                slack = u.spec.attack_range * ATTACK_HOLD_SLACK
                if u.role == "ranged":
                    ideal = u.spec.attack_range * ATTACK_RANGED_STANDOFF
                    in_band = abs(dist - ideal) <= max(slack, TILE)
                    can_fire = dist <= u.spec.attack_range * 0.98
                    if can_fire and in_band:
                        u.path.clear()
                        continue
                else:
                    if dist <= u.spec.attack_range * 0.95:
                        u.path.clear()
                        continue
                u.set_path([hold], self.map)

    def issue_attack_move(self, units: list[Unit],
                          points: list[tuple[float, float]],
                          formation: bool = False, queue: bool = False) -> None:
        """攻击移动：沿路线行进，遇敌交战，消灭后恢复原路线继续前进。

        与普通移动的区别在接敌行为——普通移动一旦交火即清空路线停下；
        攻击移动只是把剩余路线暂存到 travel_path，待当前目标消失后自动续程。
        笔触同样平移到各兵脚下（多选保队形）；单击（单点）时退化为向该点攻击移动。
        queue=True 时追加到指令队列。
        """
        units = [u for u in units if u.alive and not u.routing]
        if not units or not points:
            return
        hx, hy = points[0]
        single = len(points) < 2
        if single:
            cx = sum(u.x for u in units) / len(units)
            cy = sum(u.y for u in units) / len(units)
        immediate: list[Unit] = []
        for u in units:
            if single:
                pts = [(hx + (u.x - cx), hy + (u.y - cy))]
            else:
                dx, dy = u.x - hx, u.y - hy
                pts = [(x + dx, y + dy) for x, y in points]
            entry = {"kind": "attack_move", "points": pts}
            if queue and self._is_order_busy(u):
                u.order_queue.append(entry)
                u.auto = False
            else:
                if not queue:
                    u.order_queue.clear()
                self._apply_order_entry(u, entry)
                immediate.append(u)
        if immediate and formation and not queue:
            self._apply_formation(immediate, True)

    def issue_patrol(self, units: list[Unit],
                     points: list[tuple[float, float]],
                     formation: bool = False, queue: bool = False) -> None:
        """巡逻：在当前位置与目标路线之间往返循环，遇敌交战、消灭后继续巡逻。

        多点笔触：在笔触两端往返；单击（单点）：在「下达位置 ↔ 目标点」之间往返。
        巡逻自带攻击移动语义：接敌暂存路线、灭敌续程。
        queue=True 时追加到指令队列（注意：巡逻本身持续忙碌，其后队列要等 stop 才可能执行）。
        """
        units = [u for u in units if u.alive and not u.routing]
        if not units or not points:
            return
        hx, hy = points[0]
        single = len(points) < 2
        if single:
            cx = sum(u.x for u in units) / len(units)
            cy = sum(u.y for u in units) / len(units)
        immediate: list[Unit] = []
        for u in units:
            if single:
                tgt = (hx + (u.x - cx), hy + (u.y - cy))
                pts = [tgt]
            else:
                dx, dy = u.x - hx, u.y - hy
                pts = [(x + dx, y + dy) for x, y in points]
            entry = {"kind": "patrol", "points": pts}
            if queue and self._is_order_busy(u):
                u.order_queue.append(entry)
                u.auto = False
            else:
                if not queue:
                    u.order_queue.clear()
                self._apply_order_entry(u, entry)
                immediate.append(u)
        if immediate and formation and not queue:
            self._apply_formation(immediate, True)

    def stop(self, units: list[Unit]) -> None:
        for u in units:
            self._reset_order(u)
            u.path.clear()
            u.target_uid = None
            u.retreating = False
            u.auto = False        # 停止也是手动指令，一并解除托管

    def set_auto(self, units: list[Unit], enabled: bool) -> None:
        """开/关托管。开启后由我方 autopilot 指挥，直到玩家再次手动下令。"""
        changed = [u for u in units if u.alive and u.faction is Faction.PLAYER
                   and u.auto != enabled]
        for u in changed:
            u.auto = enabled
            if enabled:
                # 托管是控制权切换：废弃玩家留下的巡逻/攻移/编队状态，
                # 否则 AI 的普通 set_path 会被旧 order 重新解释成巡逻续程。
                self._reset_order(u)
                u.path.clear()
                u.target_uid = None
                u.retreating = False
                u.ai_cd = 0.0     # 立即允许 autopilot 下令，无需等冷却
            else:
                u.retreating = False
        if changed:
            self.log(f"{len(changed)} 个兵团{'进入托管' if enabled else '解除托管'}")

    # ------------------------------------------------------------ 模拟推进
    def update(self, dt: float) -> None:
        """推进模拟。允许较大 dt（UI 倍速一次传入 speed×帧时基），
        避免 ×4 时完整跑 4 遍 update；寻路热点由 set_path_throttled 另行节流。"""
        if dt <= 0.0:
            return
        # 极端大步（如脚本快进）拆成 ≤2 帧，防止一次穿过整段路径/齐射窗口
        if dt > 0.25:
            step = 1.0 / 30.0
            while dt > 1e-9:
                sub = min(step, dt)
                self._update_motion(sub)
                dt -= sub
            return
        self._update_motion(dt)

    def _update_motion(self, dt: float) -> None:
        self.elapsed += dt
        self._path_budget = PATHS_PER_FRAME  # 每帧重置寻路额度
        self._apply_formation_speeds()      # 编队锁定：按最慢者限速，保持队形同步
        for u in self.units:
            if not u.alive:
                continue
            u.flash = max(0.0, u.flash - dt)
            u.ai_cd = max(0.0, u.ai_cd - dt)
            u.advance(dt, self.map)
        for u in self.units:
            u.track_stillness(dt)
        self._grid_dirty = True          # 行军后位置已变，近邻查询前重建
        self._update_morale(dt)
        self._acquire_targets()
        self._chase_attack_orders()        # 指定攻击：目标跑出射程则追击
        self._resume_orders()              # 攻击移动/巡逻：灭敌后恢复暂存路线
        self._advance_order_queues()       # Shift 排队：当前指令完成后弹出下一项
        self._resolve_combat(dt)
        self._separate()
        self.commander.update(self, dt)
        self.autopilot.update(self, dt)
        self._field_cd -= dt
        if self._field_cd <= 0.0:
            self.refresh_field()
        dead = [u for u in self.units if not u.alive]
        for u in dead:
            side = self.stats.side[u.faction]
            side.losses += 1
            side.losses_by_type[u.type_key] = side.losses_by_type.get(u.type_key, 0) + 1
            self.stats.fallen.append((u.faction, f"{u.spec.name}#{u.uid}",
                                      u.kills, u.dmg_dealt))
            tag = f"（曾击破 {u.kills} 团）" if u.kills else ""
            msg = f"{FACTION_NAME[u.faction]}{u.spec.name} 被歼灭{tag}"
            self.log(msg)
            self.mark_timeline("kill", msg)
            # 目睹友军覆灭：附近同阵营部队军心动摇（越近越重）
            for a in self._ensure_grid().query(u.x, u.y, MORALE_ALLY_FALL_R):
                if not a.alive or a.faction is not u.faction or a is u:
                    continue
                d = a.distance_to(u)
                if d < MORALE_ALLY_FALL_R:
                    a.shake_morale(MORALE_ALLY_FALL * (1.0 - d / MORALE_ALLY_FALL_R))
        if dead:
            self.units = [u for u in self.units if u.alive]
            self._grid_dirty = True
        self._refresh_player_spot_memory()
        self.record_force_sample()

    def record_force_sample(self, force: bool = False) -> None:
        """按间隔把双方有效战力写入 force_history（供折线图）。"""
        st = self.stats
        last_t = st.force_history[-1][0] if st.force_history else -1e9
        if not force and self.elapsed - last_t < FORCE_SAMPLE_INTERVAL:
            return
        ps = army_strength(self.units_of(Faction.PLAYER))
        es = army_strength(self.units_of(Faction.ENEMY))
        st.force_history.append((self.elapsed, ps, es))
        del st.force_history[:-FORCE_HISTORY_MAX]

    def mark_timeline(self, kind: str, text: str) -> None:
        """记录一条时间线事件（与战报 log 配合，供图表刻度）。"""
        self.stats.timeline.append((self.elapsed, kind, text))
        del self.stats.timeline[:-TIMELINE_MAX]

    def _refresh_player_spot_memory(self) -> None:
        """刷新玩家对敌军的最后已知位置（可见则更新；阵亡/过期遗忘）。"""
        now = self.elapsed
        for e in self.units_of(Faction.ENEMY):
            if self.visible_to(Faction.PLAYER, e):
                self._spot_known[e.uid] = (e.x, e.y)
                self._spot_known_t[e.uid] = now
        live = {e.uid for e in self.units_of(Faction.ENEMY)}
        for uid in list(self._spot_known):
            if uid not in live or now - self._spot_known_t[uid] > SPOT_MEMORY_TTL:
                self._spot_known.pop(uid, None)
                self._spot_known_t.pop(uid, None)

    def player_memory_ghosts(self) -> list[tuple[float, float, float, int]]:
        """曾见但当前不可见的敌军记忆点。

        返回 ``(x, y, age_ratio, uid)``：``age_ratio`` 为 0..1（0=刚见，1=即将遗忘），
        供小地图淡红点与主地图残影按年龄淡出。
        """
        now = self.elapsed
        out: list[tuple[float, float, float, int]] = []
        for uid, pos in self._spot_known.items():
            u = self.unit_by_uid(uid)
            if u is not None and self.visible_to(Faction.PLAYER, u):
                continue
            age = now - self._spot_known_t.get(uid, now)
            if age < 0.0 or age > SPOT_MEMORY_TTL:
                continue
            out.append((pos[0], pos[1], age / SPOT_MEMORY_TTL, uid))
        return out

    def _terrain_speed(self, u: Unit) -> float:
        """兵团在当前地形下的行军速度（像素/秒），供编队限速取最小值。"""
        info = TERRAIN_INFO[self.map.terrain_at(u.x, u.y)]
        return u.spec.speed * max(info.speed, 0.15)

    def _apply_formation_speeds(self) -> None:
        """编队锁定：同一编队内取各兵当前地形速度的最小值，作为本帧限速写入各兵。

        快兵被压到最慢者的节拍，不会超前脱节；路线全部走完（含暂存）的兵退出编队。
        """
        groups: dict[int, list[Unit]] = {}
        for u in self.units:
            if not u.alive or not u.formation_lock or u.formation_id == 0:
                u._speed_cap = 0.0
                continue
            # 仍有行军/暂存/巡逻/锁定攻击目标时保持编队，避免接敌时拆队
            busy = (bool(u.path) or bool(u.travel_path) or u.order == "patrol"
                    or (u.order in ("attack", "attack_move")
                        and self.unit_by_uid(u.target_uid) is not None))
            if not busy:
                u.formation_lock = False
                u.formation_id = 0
                u._speed_cap = 0.0
                continue
            groups.setdefault(u.formation_id, []).append(u)
        for members in groups.values():
            cap = min(self._terrain_speed(m) for m in members)
            for m in members:
                m._speed_cap = cap

    def _resume_orders(self) -> None:
        """攻击移动 / 巡逻的兵团在当前目标消失后，恢复此前暂存的行军路线。

        接敌时 _resolve_combat 把剩余 path 暂存进 travel_path 并清空 path；
        这里在索敌之后检测：若无有效目标且仍有暂存路线，则把 travel_path 还回 path 续程。
        """
        for u in self.units:
            if not u.alive or u.routing:
                continue
            if u.order not in ("attack_move", "patrol"):
                continue
            if u.path or not u.travel_path:
                continue                   # 仍在行军，或没有暂存路线可恢复
            if self.unit_by_uid(u.target_uid) is not None:
                continue                   # 仍有目标在射程，继续交战
            # 从当前位置重接暂存路线，避免接敌推挤后橡皮筋拉回旧航点
            rest = u.travel_path
            u.travel_path = []
            u.path = build_route((u.x, u.y), rest, self.map)
            u._path_goal = rest[-1] if rest else None

    def _update_morale(self, dt: float) -> None:
        """士气回复、溃逃触发/重整、溃兵逃跑路线与出图溃散。"""
        escaped: list[Unit] = []
        grid = self._ensure_grid()
        # 敌情感知（反向标记）：每个兵团按「自己的」威胁半径向周边散布威胁，
        # 而非每个单位按全局最大射程收集——查询圆按兵种缩小（步骑仅 ~77px），
        # 语义与逐一比对 d < e.attack_range*1.6 完全一致。
        threatened: set[int] = set()
        for e in self.units:
            if not e.alive:
                continue
            r = e.spec.attack_range * 1.6
            for u in grid.query(e.x, e.y, r):
                if u.faction is not e.faction and u.alive and u.distance_to(e) < r:
                    threatened.add(u.uid)
        for u in self.units:
            if not u.alive:
                continue
            foe_near = u.uid in threatened
            was_routing = u.routing
            u.update_morale(dt, foe_near)
            if u.routing and not was_routing:
                msg = f"{FACTION_NAME[u.faction]}{u.spec.name}#{u.uid} 军心崩溃，溃逃！"
                self.log(msg)
                self.mark_timeline("rout", msg)
            elif was_routing and not u.routing:
                msg = f"{FACTION_NAME[u.faction]}{u.spec.name}#{u.uid} 重整旗鼓，归队"
                self.log(msg)
                self.mark_timeline("rally", msg)
            if u.routing:
                # 逃到地图边缘 → 彻底溃散离场，计入损失
                if (u.x < TILE * 2 or u.x > self.map.pixel_width - TILE * 2
                        or u.y < TILE * 2 or u.y > self.map.pixel_height - TILE * 2):
                    escaped.append(u)
                    continue
                if not u.path:
                    u.set_path([self._flee_point(u)], self.map)
        for u in escaped:
            side = self.stats.side[u.faction]
            side.losses += 1
            side.losses_by_type[u.type_key] = side.losses_by_type.get(u.type_key, 0) + 1
            self.stats.fallen.append((u.faction, f"{u.spec.name}#{u.uid}",
                                      u.kills, u.dmg_dealt))
            msg = f"{FACTION_NAME[u.faction]}{u.spec.name}#{u.uid} 溃散出战场"
            self.log(msg)
            self.mark_timeline("escape", msg)
            self.units.remove(u)
            self._grid_dirty = True

    def _flee_point(self, u: Unit) -> tuple[float, float]:
        """溃兵的下一段逃跑落点：背向最近敌人、朝己方出生侧逃。"""
        foes = [e for e in self.units if e.alive and e.faction is not u.faction]
        if foes:
            near = min(foes, key=u.distance_to)
            dx, dy = u.x - near.x, u.y - near.y
            n = math.hypot(dx, dy) or 1.0
            dx, dy = dx / n, dy / n
        else:
            dx, dy = 0.0, 0.0
        # 叠加「回家」方向：我军向西、敌军向东（与出生位置一致）
        home = -1.0 if u.faction is Faction.PLAYER else 1.0
        dx = dx * 0.6 + home * 0.4
        dy = dy * 0.6
        n = math.hypot(dx, dy) or 1.0
        fx = u.x + dx / n * ROUT_LEG
        fy = u.y + dy / n * ROUT_LEG
        fx = max(TILE, min(self.map.pixel_width - TILE, fx))
        fy = max(TILE, min(self.map.pixel_height - TILE, fy))
        return self.map.nearest_passable(fx, fy)

    def _field_signature(self) -> tuple:
        """战场签名（脏检查）：地图版本 + 各兵团量化后的位置与血量。

        任何影响势力场结果的因素（地形改动、单位增删、移动超过 8px、
        血量变化超过 1/32）都会改变签名；签名不变则跳过重算。
        """
        gm = self.map
        return (gm.map_uid, gm.terrain_version,
                tuple((u.uid, int(u.faction),
                       int(u.x // FIELD_SIG_POS), int(u.y // FIELD_SIG_POS),
                       int(u.hp_ratio * FIELD_SIG_HP))
                      for u in self.units if u.alive))

    def refresh_field(self, force: bool = False) -> None:
        """重算势力场；战场没有实质变化时跳过（脏检查），只重置冷却。"""
        sig = self._field_signature()
        if not force and self.field is not None and sig == self._field_sig:
            self._field_cd = INFLUENCE_INTERVAL
            return
        self.field = compute_field(self)
        self._field_sig = sig
        self.field_version += 1
        self._field_cd = INFLUENCE_INTERVAL

    def try_repath(self, u: Unit, point: tuple[float, float],
                   min_interval: float = 0.45,
                   goal_slack: float = TILE * 3.0,
                   force: bool = False) -> bool:
        """带全局帧预算的重寻：预算耗尽则推迟到后续帧。"""
        if not force and self._path_budget <= 0:
            return False
        if force:
            u.set_path([point], self.map)
            self._path_budget = max(0, self._path_budget - 1)
            return True
        ok = u.set_path_throttled(point, self.map, min_interval, goal_slack)
        if ok:
            self._path_budget -= 1
        return ok

    def _chase_attack_orders(self) -> None:
        """指定攻击：锁定目标仍在则追向分槽站位点；目标已死则解除指令。"""
        for u in self.units:
            if not u.alive or u.routing or u.order != "attack":
                continue
            t = self.unit_by_uid(u.target_uid)
            if t is None:
                u.order = "move"
                u.target_uid = None
                u.path.clear()
                continue
            dist = u.distance_to(t)
            if u.role == "ranged":
                ideal = u.spec.attack_range * ATTACK_RANGED_STANDOFF
                slack = max(u.spec.attack_range * ATTACK_HOLD_SLACK, TILE)
                if dist <= u.spec.attack_range and abs(dist - ideal) <= slack:
                    if u.path:
                        u.path.clear()
                    continue
            elif dist <= u.spec.attack_range:
                continue                  # 近战射程内由战斗逻辑停步开火
            cohort = self._attack_cohort(t, u.role)
            try:
                slot = next(i for i, c in enumerate(cohort) if c is u)
            except StopIteration:
                slot, n = 0, 1
            else:
                n = len(cohort)
            hold = self._attack_hold_point(u, t, slot, n)
            self.try_repath(
                u, hold,
                min_interval=0.5,
                goal_slack=max(TILE * 2.5, u.spec.attack_range * 0.25))

    def _acquire_targets(self) -> None:
        """索敌带集火倾向：射程内优先打更近且更残的目标。

        候选集来自空间哈希：只看 reach 圆覆盖的桶，不再全体扫描。
        指定攻击（order=attack）期间不改锁定目标，即使其在射程外。
        """
        focus = self.params.focus_fire
        grid = self._ensure_grid()
        for u in self.units:
            if not u.alive:
                continue
            if u.routing:
                u.target_uid = None     # 溃兵只顾逃命，不索敌
                continue
            if u.retreating:
                if u.path:
                    u.target_uid = None  # 主动撤退期间不重新接敌，否则撤退命令立即失效
                    continue
                u.retreating = False     # 已到撤退点，恢复正常索敌
            # 玩家/指令锁定的指定攻击：目标仍存活则绝不改口
            if u.order == "attack":
                if self.unit_by_uid(u.target_uid) is not None:
                    continue
                u.order = "move"         # 目标已死，恢复普通索敌
            info = TERRAIN_INFO[self.map.terrain_at(u.x, u.y)]
            reach = max(u.spec.attack_range, u.spec.vision * info.vision * 0.5)
            cur = self.unit_by_uid(u.target_uid)
            if cur is not None and u.distance_to(cur) <= reach * 1.15:
                continue
            # 集火：参数开启时优先残血（敌军困难 / 我方托管）
            use_focus = focus
            best, best_score = None, math.inf
            for e in grid.query(u.x, u.y, reach):
                if not e.alive or e.faction is u.faction:
                    continue
                d = u.distance_to(e)
                # 藏身森林的目标：超出射程的部分要靠"发现"，距离打折
                r = reach
                if (self.map.terrain_at(e.x, e.y) is Terrain.FOREST
                        and r > u.spec.attack_range):
                    r = max(u.spec.attack_range, r * FOREST_CONCEAL)
                if d > r:
                    continue
                if u.type_key == "artillery":
                    # 炮兵优先高价值目标：价值主导（负分越小越优先）、距离为次，
                    # 让重火力落在血厚/威胁大的点上，而非最近目标。
                    score = d * 0.4 - artillery_target_value(e) * 0.5
                else:
                    score = d + (e.hp * 0.20 if use_focus else 0.0)
                if e.routing:
                    score -= 70.0       # 追杀溃兵：背对且不还手，优先收割
                if score < best_score:
                    best, best_score = e, score
            u.target_uid = best.uid if best else None

    def _resolve_combat(self, dt: float) -> None:
        """齐射制战斗：冷却归零打出一轮，伤害受地形、高差、方位、冲势修正。"""
        for u in self.units:
            if not u.alive:
                continue
            u.attack_cd = max(0.0, u.attack_cd - dt)
            if u.routing:
                continue                    # 溃兵只顾逃命，不还手
            target = self.unit_by_uid(u.target_uid)
            if target is None:
                continue
            dist = u.distance_to(target)
            if dist > u.spec.attack_range:
                continue
            u.facing = math.atan2(target.y - u.y, target.x - u.x)
            # 进入射程即停步交战（远程不再边走边打走进近战圈）
            if u.path:
                if u.order in ("attack_move", "patrol"):
                    # 攻击移动/巡逻：暂存剩余路线，灭敌后由 _resume_orders 恢复续程
                    u.travel_path = list(u.path)
                u.path.clear()
            if u.attack_cd > 0.0:
                continue
            interval = u.spec.attack_interval
            u.attack_cd = interval
            self._fire_volley(u, target, interval)

    def estimate_volley_damage(self, attacker: Unit, target: Unit,
                               *, include_flank: bool = False,
                               include_charge: bool = False) -> float:
        """预估一次齐射伤害（期望值，不含 jitter）。

        默认不含侧击/冲锋（悬停提示用稳态期望）；实战 ``_fire_volley`` 会再乘随机与方位。
        """
        interval = attacker.spec.attack_interval
        atk_info = TERRAIN_INFO[self.map.terrain_at(attacker.x, attacker.y)]
        def_info = TERRAIN_INFO[self.map.terrain_at(target.x, target.y)]
        defense = max(0.05, def_info.defense * target.entrench)
        dmg = (attacker.spec.attack * attacker.dmg_mult * atk_info.attack
               / defense * interval)
        dmg *= 1.0 + VET_DMG * attacker.vet_level
        dmg *= COUNTER_MULT.get((attacker.type_key, target.type_key), 1.0)
        ax, ay = self.map.tile_at(attacker.x, attacker.y)
        tx, ty = self.map.tile_at(target.x, target.y)
        d_elev = self.map.elevation[ay][ax] - self.map.elevation[ty][tx]
        dmg *= max(0.55, 1.0 + HIGH_GROUND_SCALE * d_elev)
        if include_flank:
            rel = math.atan2(attacker.y - target.y,
                             attacker.x - target.x) - target.facing
            rel = abs((rel + math.pi) % (2 * math.pi) - math.pi)
            if rel > math.radians(120):
                dmg *= FLANK_REAR_MULT
            elif rel > math.radians(60):
                dmg *= FLANK_SIDE_MULT
        if (include_charge and attacker.type_key == "cavalry"
                and attacker.role == "melee" and attacker.still_time < 1.5):
            dmg *= CHARGE_MULT
        return dmg

    def combat_preview(self, attackers: list[Unit], target: Unit
                       ) -> dict[str, float | str]:
        """悬停用战斗预览：克制文案、下一轮伤害区间、合计 TTK。"""
        attackers = [u for u in attackers if u.alive and not u.routing]
        if not attackers or target is None or not target.alive:
            return {}
        mults = [COUNTER_MULT.get((u.type_key, target.type_key), 1.0)
                 for u in attackers]
        revs = [COUNTER_MULT.get((target.type_key, u.type_key), 1.0)
                for u in attackers]
        best_m, best_r = max(mults), max(revs)
        if best_m > 1.01 and best_m >= best_r:
            counter = f"克制 ↑{int(round((best_m - 1) * 100))}%"
        elif best_r > 1.01:
            counter = f"被克 ↓{int(round((best_r - 1) * 100))}%"
        else:
            counter = "均势"

        volleys = [self.estimate_volley_damage(u, target) for u in attackers]
        # 展示「主选/最近一台」的下一轮区间 + 全选合计 TTK
        primary = max(range(len(attackers)),
                      key=lambda i: volleys[i] / max(0.1, attackers[i].spec.attack_interval))
        v0 = volleys[primary]
        lo, hi = v0 * (1.0 - VOLLEY_JITTER), v0 * (1.0 + VOLLEY_JITTER)
        total_dps = sum(v / max(0.05, u.spec.attack_interval)
                        for u, v in zip(attackers, volleys))
        ttk = target.hp / total_dps if total_dps > 1e-6 else 999.0
        return {
            "counter": counter,
            "volley_lo": lo,
            "volley_hi": hi,
            "ttk": ttk,
            "n": float(len(attackers)),
        }

    def _fire_volley(self, u: Unit, target: Unit, interval: float) -> None:
        """一次齐射：基础 DPS×间隔，乘上所有战场修正后一次性结算，并冲击守方士气。"""
        dmg = self.estimate_volley_damage(
            u, target, include_flank=True, include_charge=True)
        dmg *= 1.0 + self._rng.uniform(-VOLLEY_JITTER, VOLLEY_JITTER)

        # 统计与战功只记录实际造成的伤害，不能把致死溢出伤害算入老练度。
        actual_dmg = min(dmg, max(0.0, target.hp))
        target.hp = max(0.0, target.hp - dmg)
        target.flash = 0.22

        # 士气冲击：按实际掉血占比换算，被包抄/被冲锋更容易动摇
        shake = actual_dmg / target.max_hp * MORALE_DMG_SCALE
        rel = math.atan2(u.y - target.y, u.x - target.x) - target.facing
        rel = abs((rel + math.pi) % (2 * math.pi) - math.pi)
        if rel > math.radians(60):
            shake *= MORALE_FLANK_BONUS
        if (u.type_key == "cavalry" and u.role == "melee"
                and u.still_time < 1.5):
            shake += MORALE_CHARGE_SHAKE
        target.shake_morale(shake)

        u.dmg_dealt += actual_dmg
        target.dmg_taken += actual_dmg
        st = self.stats
        st.volleys += 1
        st.side[u.faction].dmg_dealt += actual_dmg
        st.side[target.faction].dmg_taken += actual_dmg
        if st.first_blood is None:
            st.first_blood = self.elapsed
            msg = f"{FACTION_NAME[u.faction]}{u.spec.name} 打响第一枪"
            self.log(msg)
            self.mark_timeline("first_blood", msg)
        if target.hp <= 0:
            u.kills += 1
            st.side[u.faction].kills += 1


    def _separate(self) -> None:
        """简单的圆形碰撞分离，防止兵团完全重叠（不会把人推进水里）。

        候选对来自空间哈希的同桶/邻桶配对（O(n·k)），
        不再做全体两两比较（O(n²)）。
        """
        for a, b in self._ensure_grid().iter_pairs():
            if not a.alive or not b.alive:
                continue
            dx, dy = b.x - a.x, b.y - a.y
            d = math.hypot(dx, dy)
            min_d = a.spec.radius + b.spec.radius
            if d >= min_d:
                continue
            if d < 1e-6:
                dx, dy, d = 0.7, 0.7, 1.0
            push = (min_d - d) * 0.5
            ux, uy = dx / d, dy / d
            ax, ay = a.x - ux * push, a.y - uy * push
            bx, by = b.x + ux * push, b.y + uy * push
            if self.map.passable(ax, ay):
                a.x, a.y = ax, ay
            if self.map.passable(bx, by):
                b.x, b.y = bx, by
        self._grid_dirty = True          # 分离推挤改动了位置，桶归属过期

    # ------------------------------------------------------------ 日志
    def log(self, text: str) -> None:
        self.events.append(f"[{int(self.elapsed // 60):02d}:{int(self.elapsed % 60):02d}] {text}")
        self.event_seq += 1
        del self.events[:-200]

    def battle_summary(self, winner: Faction) -> str:
        """战后结算文本（HTML），供结束对话框展示。"""
        st = self.stats
        mm, ss = int(self.elapsed) // 60, int(self.elapsed) % 60
        lines = [f"<b>战斗时长</b> {mm:02d}:{ss:02d}　<b>齐射</b> {st.volleys} 轮"]
        if st.first_blood is not None:
            fb = int(st.first_blood)
            lines.append(f"<b>首次交火</b> {fb // 60:02d}:{fb % 60:02d}")

        type_name = {k: v.name for k, v in UNIT_TYPES.items()}
        for fac, color in ((Faction.PLAYER, "#48a0f0"), (Faction.ENEMY, "#e05548")):
            s = st.side[fac]
            alive = len(self.units_of(fac))
            loss_txt = "、".join(f"{type_name[k]}×{n}"
                                 for k, n in sorted(s.losses_by_type.items())) or "无"
            lines.append(
                f"<span style='color:{color}'><b>{FACTION_NAME[fac]}</b>　"
                f"存活 {alive} 团 / 损失 {s.losses} 团（{loss_txt}）<br>"
                f"　击破 {s.kills} 团　输出 {s.dmg_dealt:.0f}　承伤 {s.dmg_taken:.0f}</span>")

        # MVP：全场（含阵亡）战功最高——击破一团抵 150 输出
        def score(kills: int, dealt: float) -> float:
            return dealt + 150.0 * kills

        best_score, best_txt = -1.0, ""
        for u in self.units:
            if score(u.kills, u.dmg_dealt) > best_score:
                best_score = score(u.kills, u.dmg_dealt)
                best_txt = (f"{FACTION_NAME[u.faction]}{u.spec.name}#{u.uid}　"
                            f"击破 {u.kills} 团、输出 {u.dmg_dealt:.0f}")
        for fac, name, kills, dealt in st.fallen:
            if score(kills, dealt) > best_score:
                best_score = score(kills, dealt)
                best_txt = (f"{FACTION_NAME[fac]}{name}（阵亡）　"
                            f"击破 {kills} 团、输出 {dealt:.0f}")
        if best_txt:
            lines.append(f"<b>全场最佳</b>　{best_txt}")
        return "<br>".join(lines)

    def winner(self) -> Faction | None:
        """一方全灭、或残部全部溃逃而对方仍有战力时，另一方获胜。

        双方皆空、或双方都只剩溃兵时返回 None；此时请用 `is_draw()` 判定平局，
        避免战局无限拖下去。
        """
        p = self.units_of(Faction.PLAYER)
        e = self.units_of(Faction.ENEMY)
        if p and not e:
            return Faction.PLAYER
        if e and not p:
            return Faction.ENEMY
        if not p and not e:
            return None
        p_fight = any(not u.routing for u in p)
        e_fight = any(not u.routing for u in e)
        if p_fight and e and not e_fight:
            return Faction.PLAYER      # 敌军只剩溃兵，胜局已定
        if e_fight and p and not p_fight:
            return Faction.ENEMY
        return None

    def is_draw(self) -> bool:
        """双方皆灭，或双方都只剩溃兵（无人还能作战）→ 平局。"""
        if self.winner() is not None:
            return False
        p = self.units_of(Faction.PLAYER)
        e = self.units_of(Faction.ENEMY)
        if not p and not e:
            return True
        if p and e and all(u.routing for u in p) and all(u.routing for u in e):
            return True
        return False

    def snap_units_passable(self) -> int:
        """把落在不可通行处的兵团吸附到最近可通行点（编辑器改地形后调用）。"""
        n = 0
        for u in self.units:
            if not self.map.passable(u.x, u.y):
                u.x, u.y = self.map.nearest_passable(u.x, u.y)
                u.path.clear()
                n += 1
        if n:
            self._grid_dirty = True
        return n

    # ------------------------------------------------------------ 剧本存档（编辑器）
    def to_dict(self) -> dict:
        """把当前地图与兵团摆位导出为可 JSON 序列化的字典（剧本快照）。"""
        gm = self.map
        return {
            "version": SCENARIO_VERSION,
            "seed": gm.seed,
            "difficulty": int(self.difficulty),
            "width": gm.width,
            "height": gm.height,
            "tiles": [t for row in gm.tiles for t in row],
            "elevation": [round(e, 3) for row in gm.elevation for e in row],
            "units": [
                {"type": u.type_key, "faction": int(u.faction),
                 "x": round(u.x, 2), "y": round(u.y, 2),
                 "hp_scale": u.hp_scale, "dmg_mult": u.dmg_mult}
                for u in self.units
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "World":
        """从剧本快照重建世界；拒绝损坏或不受支持的数据。"""
        if not isinstance(data, dict):
            raise ValueError("剧本根节点必须是对象")
        version = data.get("version", SCENARIO_VERSION)
        if version != SCENARIO_VERSION:
            raise ValueError(f"不支持的剧本版本: {version}")
        try:
            w = int(data["width"])
            h = int(data["height"])
        except (KeyError, TypeError, ValueError) as ex:
            raise ValueError("剧本缺少有效的 width/height") from ex
        if not (1 <= w <= 512 and 1 <= h <= 512):
            raise ValueError("地图尺寸必须在 1..512 格之间")

        tiles = data.get("tiles")
        if not isinstance(tiles, list) or len(tiles) != w * h:
            raise ValueError(f"tiles 长度必须等于 width*height（{w * h}）")
        try:
            tile_values = [int(v) for v in tiles]
            valid_terrain = {int(t) for t in Terrain}
            if any(v not in valid_terrain for v in tile_values):
                raise ValueError("tiles 含未知地形编号")
            difficulty = Difficulty(int(data.get("difficulty", int(Difficulty.EASY))))
        except (TypeError, ValueError) as ex:
            raise ValueError(f"剧本地形或难度无效: {ex}") from ex

        elev_values: list[float] | None = None
        elev = data.get("elevation")
        if elev is not None:
            if not isinstance(elev, list) or len(elev) != w * h:
                raise ValueError(f"elevation 长度必须等于 width*height（{w * h}）")
            try:
                elev_values = [float(v) for v in elev]
            except (TypeError, ValueError) as ex:
                raise ValueError("elevation 必须全部为数值") from ex
            if any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in elev_values):
                raise ValueError("elevation 必须是 0..1 的有限数值")

        units = data.get("units", [])
        if not isinstance(units, list):
            raise ValueError("units 必须是数组")

        world = cls(map_width=w, map_height=h, seed=data.get("seed"),
                    difficulty=difficulty, populate=False)
        world.map.tiles = [tile_values[y * w:(y + 1) * w] for y in range(h)]
        world.map.terrain_version += 1     # 整批替换 tiles，各层缓存需失效
        if elev_values is not None:
            world.map.elevation = [elev_values[y * w:(y + 1) * w] for y in range(h)]
        for i, ud in enumerate(units):
            if not isinstance(ud, dict):
                raise ValueError(f"units[{i}] 必须是对象")
            try:
                world.add_unit(ud["type"], Faction(int(ud["faction"])),
                               float(ud["x"]), float(ud["y"]),
                               hp_scale=float(ud.get("hp_scale", 1.0)),
                               dmg_mult=float(ud.get("dmg_mult", 1.0)))
            except (KeyError, TypeError, ValueError) as ex:
                raise ValueError(f"units[{i}] 无效: {ex}") from ex
        world.refresh_field(force=True)
        return world

    def clone(self) -> "World":
        """深拷贝一份初始状态世界（试玩时用，避免战斗改动编辑器里的剧本）。"""
        return World.from_dict(self.to_dict())


def save_world(world: World, path: str) -> None:
    """把世界剧本写入 JSON 文件（不含 Qt，逻辑层可直接调用）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(world.to_dict(), f, ensure_ascii=False)


def load_world(path: str) -> World:
    """从 JSON 文件读回一个世界剧本。"""
    with open(path, "r", encoding="utf-8") as f:
        return World.from_dict(json.load(f))
