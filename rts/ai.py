"""敌方指挥官 AI 与难度设定。

Commander 以固定间隔统一指挥某一阵营（默认敌军）：评估双方实力与位置，
决定进攻／据守，安排远程兵种后置、受伤单位撤退、抢占防御地形、协同扑向弱侧。

受战争迷雾约束：只对己方视野内**可见**的敌军下令进攻/集火；不可见者靠
「最后已知位置」记忆维持侦察推进，避免透视全图。骑兵执行侧翼包抄，炮兵
优先打击高价值目标（高血高伤的威胁点）。难度只是给 Commander 换一组参数，
并对属性做倍率缩放。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum

from .terrain import TERRAIN_INFO, TILE, Terrain
from .units import Faction, Unit, artillery_target_value


# ------------------------------------------------------------ 侦察 / 包抄 / 记忆
SPOT_MEMORY_TTL = 8.0          # 默认最后已知位置记忆保留时长（秒）
SPOT_MEMORY_TTL_EASY = 4.0     # 简单难度：更短记忆，弱化追击
RECON_ADVANCE = TILE * 10       # 无可见敌军时，向侦察方向推进的距离基准
FLANK_OFFSET = TILE * 9          # 骑兵侧翼集结点相对敌军质心的横向偏移
FLANK_BEHIND = TILE * 3          # 侧翼切入时压向敌军纵深的距离
CAVALRY_FLANK_FAR = TILE * 4    # 骑兵距侧翼集结点超过此值时先去集结
ARTY_GUARD_RANGE = TILE * 6     # 炮兵受威胁半径：敌近战进入则召护卫
ARTY_ESCORT_SLOTS = 2           # 最多派几个近战护卫一门受威胁的炮
FEINT_FRACTION = 0.30           # 佯动分兵：近战中约三成压向侧翼次点


class Difficulty(IntEnum):
    EASY = 0
    HARD = 1


@dataclass(frozen=True)
class DiffParams:
    key: Difficulty
    name: str
    desc: str
    hp_mult: float          # 敌军血量倍率
    dmg_mult: float         # 敌军输出倍率
    reaction: float         # 指挥官决策间隔（秒），越小越灵敏
    extra_units: int        # 敌军额外兵团数
    focus_fire: bool        # 集火：优先扑向落单/受伤目标
    retreat: bool           # 受伤单位后撤重整
    hold_terrain: bool      # 抢占并据守防御地形
    coordinate: bool        # 协同推进（集结后再压上）而非各自为战


DIFFICULTIES: dict[Difficulty, DiffParams] = {
    Difficulty.EASY: DiffParams(
        Difficulty.EASY, "简单",
        "敌军略弱、各自为战、短时记忆追击、不会撤退，适合熟悉操作。",
        hp_mult=0.85, dmg_mult=0.85, reaction=1.6, extra_units=0,
        focus_fire=False, retreat=False, hold_terrain=False, coordinate=False),
    Difficulty.HARD: DiffParams(
        Difficulty.HARD, "困难",
        "敌军更强、集火弱侧、护炮、镜像玩家集火、佯动分兵、受伤撤退并抢占地形。",
        hp_mult=1.20, dmg_mult=1.15, reaction=0.55, extra_units=2,
        focus_fire=True, retreat=True, hold_terrain=True, coordinate=True),
}

# 我方托管指挥官的参数：倍率类字段不生效（只作用于敌军补兵），
# 行为上用满协同逻辑，反应速度介于两档难度之间。
AUTOPILOT_PARAMS = DiffParams(
    Difficulty.HARD, "托管", "接管选中兵团：协同推进、远程后置、残血撤退、抢占地形。",
    hp_mult=1.0, dmg_mult=1.0, reaction=0.8, extra_units=0,
    focus_fire=True, retreat=True, hold_terrain=True, coordinate=True)


def _centroid(units: list[Unit]) -> tuple[float, float]:
    n = len(units)
    return (sum(u.x for u in units) / n, sum(u.y for u in units) / n)


class Commander:
    """一个阵营的战场大脑。world 每帧调用 update()，内部按 reaction 间隔决策。

    受战争迷雾约束：进攻/集火只针对己方视野内可见的敌军；不可见者靠
    ``_known`` 记忆的「最后已知位置」维持推进，记忆超过 ``SPOT_MEMORY_TTL``
    即遗忘。这样既不会透视全图，也不会因一时丢失视野就原地发呆。
    """

    def __init__(self, faction: Faction, params: DiffParams,
                 only_auto: bool = False):
        self.faction = faction
        self.params = params
        self.only_auto = only_auto     # True=只指挥 u.auto 的兵团（我方托管）
        self.cd = 0.3
        # 战争迷雾记忆：uid -> (x, y) 最后已知位置 / uid -> 记忆时刻
        self._known: dict[int, tuple[float, float]] = {}
        self._known_t: dict[int, float] = {}
        # 己方出生侧（+1=左侧出生，敌方在右；-1=右侧出生，敌方在左），
        # 首次决策时按己方质心相对地图中线推断，供无可见敌军时定侦察方向。
        self._birth_side: float = 0.0

    # ------------------------------------------------------------ 主入口
    def update(self, world, dt: float) -> None:
        self.cd -= dt
        if self.cd > 0.0:
            return
        self.cd = self.params.reaction

        army = world.units_of(self.faction)
        mine = [u for u in army if u.auto] if self.only_auto else army
        mine = [u for u in mine if not u.routing]    # 溃兵不受指挥
        foes = world.units_of(Faction.PLAYER if self.faction is Faction.ENEMY
                              else Faction.ENEMY)
        if not mine or not foes:
            return

        # 首次决策时按己方质心相对中线推断出生侧，供无可见敌军时定侦察方向
        if self._birth_side == 0.0:
            cx_map = world.map.width * TILE * 0.5
            mx = sum(u.x for u in mine) / len(mine)
            self._birth_side = 1.0 if mx < cx_map else -1.0

        # 战争迷雾：只对可见敌军下令；同时刷新最后已知位置记忆
        vis_foes = [e for e in foes if world.visible_to(self.faction, e)]
        self._refresh_memory(world, foes, vis_foes)

        if not self.params.coordinate:
            self._simple_push(world, mine, vis_foes)
        else:
            # 局势评估看全军（含手操兵团），指挥只下达给 mine
            self._coordinated(world, mine, foes, vis_foes, army)

    # ------------------------------------------------------------ 记忆维护
    def _memory_ttl(self) -> float:
        """简单难度记忆更短，弱化丢视野后的死咬追击。"""
        if self.params.key is Difficulty.EASY and not self.only_auto:
            return SPOT_MEMORY_TTL_EASY
        return SPOT_MEMORY_TTL

    def _refresh_memory(self, world, foes: list[Unit],
                        vis_foes: list[Unit]) -> None:
        """可见敌军刷新位置与时刻；阵亡/过期者遗忘。"""
        now = world.elapsed
        ttl = self._memory_ttl()
        for e in vis_foes:
            self._known[e.uid] = (e.x, e.y)
            self._known_t[e.uid] = now
        live = {e.uid for e in foes}
        for uid in list(self._known):
            if uid not in live or now - self._known_t[uid] > ttl:
                self._known.pop(uid, None)
                self._known_t.pop(uid, None)

    def _nearest_known(self, u: Unit) -> tuple[float, float] | None:
        """离 u 最近的最后已知敌军位置；无记忆返回 None。"""
        if not self._known:
            return None
        return min(self._known.values(),
                   key=lambda p: math.hypot(p[0] - u.x, p[1] - u.y))

    def _recon_dir(self, world, my_com: tuple[float, float]
                   ) -> tuple[float, float]:
        """无可见敌军时的侦察推进点：朝推断的敌方出生侧远点（地图对侧 85%/15%
        处）持续推进。用固定远点而非「质心+偏移」，避免单位到达后质心停滞、
        目标随之停滞的死锁。途中一旦发现敌军，上层逻辑即切换为可见目标进攻。"""
        side = self._birth_side or 1.0
        cy = world.map.height * TILE * 0.5
        tx = world.map.width * TILE * (0.85 if side > 0 else 0.15)
        return (tx, cy)

    # ------------------------------------------------------------ 简单：各自扑向最近可见目标
    def _simple_push(self, world, mine: list[Unit],
                     vis_foes: list[Unit]) -> None:
        for u in mine:
            if self._engaged(world, u):
                continue
            if u.path:
                continue
            if vis_foes:
                tgt = min(vis_foes, key=u.distance_to)
                self._march(world, u, (tgt.x, tgt.y))
            else:
                # 丢失视野：短时记忆追击；记忆弱/无则侦察（简单 TTL 更短）
                pt = self._nearest_known(u)
                if pt is None:
                    pt = self._recon_dir(world, _centroid(mine))
                elif self._far(u, pt, TILE * 14):
                    # 记忆点过远：不远征死咬，改向侦察方向缓推
                    pt = self._recon_dir(world, _centroid(mine))
                if pt is not None:
                    self._march(world, u, pt)

    # ------------------------------------------------------------ 困难：协同 + 站位 + 撤退 + 据守 + 包抄
    def _coordinated(self, world, mine: list[Unit], foes: list[Unit],
                     vis_foes: list[Unit],
                     army: list[Unit] | None = None) -> None:
        army = army or mine            # army=全军（评估用），mine=受指挥的兵团
        my_str = sum(u.hp for u in army)
        foe_str = sum(u.hp for u in foes)
        my_com = _centroid(mine)
        if vis_foes:
            foe_com = _centroid(vis_foes)
        elif self._known:
            _pts = list(self._known.values())
            foe_com = (sum(p[0] for p in _pts) / len(_pts),
                       sum(p[1] for p in _pts) / len(_pts))
        else:
            foe_com = _centroid(foes)   # 退化：仅作大致方向估算

        # 主攻目标：优先镜像「玩家正在指定攻击的可见敌军」，否则弱且近
        focus = self._pick_focus(world, vis_foes, my_com)
        # 集结点：己方重心与主攻目标之间，略偏己方，保证成群压上
        anchor_pt = (focus.x, focus.y) if focus else foe_com
        rally = (my_com[0] * 0.45 + anchor_pt[0] * 0.55,
                 my_com[1] * 0.45 + anchor_pt[1] * 0.55)

        attacking = my_str >= foe_str * 0.85     # 不占优时转入据守，拖入僵持
        melee = [u for u in mine if u.role == "melee"]
        van = _centroid(melee) if melee else my_com     # 前锋均值，供远程兵种躲在其后
        cav = [u for u in mine if u.type_key == "cavalry"]
        cav_com = _centroid(cav) if cav else my_com
        # 骑兵侧翼集结点（选离己方骑兵更近的一侧，少绕路）；无可见敌军不做包抄
        flank = (self._flank_rally(foe_com, my_com, cav_com)
                 if attacking and vis_foes else None)
        # 佯动次点：主攻方向另一侧，分出一部分近战牵制
        feint_pt = None
        if attacking and vis_foes and focus is not None and flank is not None:
            feint_pt = flank
        elif attacking and vis_foes and focus is not None:
            feint_pt = self._flank_rally(foe_com, my_com, my_com)

        # 护炮：受威胁的炮 → 最近近战护卫名单
        escorts = self._artillery_escorts(mine, foes, vis_foes) if attacking else {}

        # 佯动分兵：近战（非骑兵、非护卫）按 uid 取约 FEINT_FRACTION
        feint_ids: set[int] = set()
        if feint_pt is not None and attacking:
            pool = [u for u in melee
                    if u.type_key != "cavalry" and u.uid not in escorts]
            pool.sort(key=lambda u: u.uid)
            n_feint = max(1, int(round(len(pool) * FEINT_FRACTION))) if pool else 0
            feint_ids = {u.uid for u in pool[:n_feint]}

        for u in mine:
            # 1) 受伤撤退：可见敌军或记忆中的近敌都算威胁（避免迷雾外被磨死不撤）
            threat_pts = [(e.x, e.y) for e in vis_foes]
            threat_pts.extend(self._known.values())
            if (self.params.retreat and u.hp_ratio < 0.30
                    and self._threatened_at(u, threat_pts, foes)):
                bx, by = self._behind(my_com, anchor_pt, TILE * 6)
                self._march(world, u, (bx, by), force=True)
                continue

            if self._engaged(world, u):
                continue      # 已在交火，交给战斗逻辑，别乱动

            # 1b) 护卫受威胁炮兵
            if u.uid in escorts:
                ax, ay = escorts[u.uid]
                if self._far(u, (ax, ay), TILE * 2):
                    self._march(world, u, (ax, ay))
                continue

            # 2) 骑兵侧翼包抄：先去侧翼集结点，到位后横切入敌阵（触发侧击/背击）
            if u.type_key == "cavalry" and attacking and flank is not None:
                if self._far(u, flank, CAVALRY_FLANK_FAR):
                    self._march(world, u, flank)
                else:
                    self._march(world, u, foe_com)   # 从侧翼横切入
                continue

            # 3) 远程兵种
            if u.role == "ranged":
                # 炮兵受近威胁时略后撤
                if (u.type_key == "artillery"
                        and self._arty_threatened(u, foes, vis_foes)):
                    bx, by = self._behind((u.x, u.y), foe_com, TILE * 4)
                    if self._far(u, (bx, by), TILE * 1.5):
                        self._march(world, u, (bx, by))
                    continue
                # 炮兵瞄准可见的最高价值目标；弓兵/弩兵跟随主攻弱侧
                if u.type_key == "artillery" and vis_foes:
                    aim = max(vis_foes, key=artillery_target_value)
                else:
                    aim = focus if (focus and attacking) else None
                if aim is None:
                    if vis_foes:
                        # 据守中（不占优）：远程不前压，就近抢防御地形站稳
                        spot = (self._defensive_spot(world, u)
                                if self.params.hold_terrain else None)
                        if spot is not None and self._far(u, spot, TILE * 1.5):
                            self._march(world, u, spot)
                        continue
                    # 无可见敌军：随全军侦察推进，避免远程拖死整体推进
                    pt = self._nearest_known(u) or self._recon_dir(world, my_com)
                    if pt is not None and (not u.path or self._far(u, pt, TILE * 3)):
                        self._march(world, u, pt)
                    continue
                anchor = van
                tx = anchor[0] * 0.7 + aim.x * 0.3
                ty = anchor[1] * 0.7 + aim.y * 0.3
                if not u.path or self._far(u, (tx, ty), TILE * 2.5):
                    self._march(world, u, (tx, ty))
                continue

            # 4) 近战：进攻则压向主攻 / 佯动侧翼，据守则抢占附近防御地形
            if attacking:
                if u.uid in feint_ids and feint_pt is not None:
                    if not u.path or self._far(u, feint_pt, TILE * 3):
                        self._march(world, u, feint_pt)
                elif focus is not None:
                    if not u.path or self._far(u, (focus.x, focus.y), TILE * 3):
                        self._march(world, u, (rally if self._far(u, rally, TILE * 8)
                                               else (focus.x, focus.y)))
                else:
                    # 无可见敌军：向最近记忆/侦察方向推进，保持接触
                    pt = self._nearest_known(u) or self._recon_dir(world, my_com)
                    if pt is not None and (not u.path or self._far(u, pt, TILE * 3)):
                        self._march(world, u, pt)
            else:
                spot = self._defensive_spot(world, u) if self.params.hold_terrain else None
                if spot is not None and self._far(u, spot, TILE * 1.5):
                    self._march(world, u, spot)

    def _pick_focus(self, world, vis_foes: list[Unit],
                    my_com: tuple[float, float]) -> Unit | None:
        """困难主攻：优先镜像玩家指定攻击的可见目标，否则弱且近。"""
        if not vis_foes:
            return None
        # 玩家正在锁定的敌军 uid
        player_locks = {
            u.target_uid for u in world.units
            if (u.alive and u.faction is Faction.PLAYER
                and u.order == "attack" and u.target_uid is not None)}
        mirrored = [e for e in vis_foes if e.uid in player_locks]
        if mirrored:
            return min(mirrored, key=lambda e: e.hp + 0.15 * math.hypot(
                e.x - my_com[0], e.y - my_com[1]))
        return min(vis_foes, key=lambda e: e.hp + 0.15 * math.hypot(
            e.x - my_com[0], e.y - my_com[1]))

    def _arty_threatened(self, arty: Unit, foes: list[Unit],
                         vis_foes: list[Unit]) -> bool:
        """炮兵是否被敌近战贴脸威胁。"""
        pool = vis_foes or foes
        return any(e.role == "melee" and arty.distance_to(e) <= ARTY_GUARD_RANGE
                   for e in pool if e.alive)

    def _artillery_escorts(self, mine: list[Unit], foes: list[Unit],
                           vis_foes: list[Unit]) -> dict[int, tuple[float, float]]:
        """受威胁炮兵 → 护卫近战 uid 映射到护卫落点（炮附近）。"""
        arties = [u for u in mine if u.type_key == "artillery"
                  and self._arty_threatened(u, foes, vis_foes)]
        if not arties:
            return {}
        melee = [u for u in mine if u.role == "melee" and u.type_key != "cavalry"]
        assigned: dict[int, tuple[float, float]] = {}
        used: set[int] = set()
        for arty in arties:
            # 落点：炮与己方出生侧之间略偏后，护卫站在炮前侧
            candidates = sorted(
                (u for u in melee if u.uid not in used),
                key=lambda u: u.distance_to(arty))
            for guard in candidates[:ARTY_ESCORT_SLOTS]:
                gx = arty.x * 0.65 + guard.x * 0.35
                gy = arty.y * 0.65 + guard.y * 0.35
                assigned[guard.uid] = (gx, gy)
                used.add(guard.uid)
        return assigned

    # ------------------------------------------------------------ 包抄几何
    @staticmethod
    def _flank_rally(foe_com: tuple[float, float],
                     my_com: tuple[float, float],
                     cav_com: tuple[float, float]) -> tuple[float, float]:
        """敌军侧翼集结点：取主攻方向的法向量，选离己方骑兵更近的一侧，
        纵向略后退到敌阵侧后方，便于横切时打出侧击/背击。"""
        dx, dy = foe_com[0] - my_com[0], foe_com[1] - my_com[1]
        n = math.hypot(dx, dy) or 1.0
        ux, uy = dx / n, dy / n                 # 主攻方向单位向量
        # 两侧法向量
        p1 = (foe_com[0] + (-uy) * FLANK_OFFSET - ux * FLANK_BEHIND,
              foe_com[1] + (ux) * FLANK_OFFSET - uy * FLANK_BEHIND)
        p2 = (foe_com[0] + (uy) * FLANK_OFFSET - ux * FLANK_BEHIND,
              foe_com[1] + (-ux) * FLANK_OFFSET - uy * FLANK_BEHIND)
        d1 = math.hypot(p1[0] - cav_com[0], p1[1] - cav_com[1])
        d2 = math.hypot(p2[0] - cav_com[0], p2[1] - cav_com[1])
        return p1 if d1 <= d2 else p2

    # ------------------------------------------------------------ 小工具
    @staticmethod
    def _engaged(world, u: Unit) -> bool:
        t = world.unit_by_uid(u.target_uid)
        return t is not None and u.distance_to(t) <= u.spec.attack_range * 1.05

    @staticmethod
    def _threatened(u: Unit, foes: list[Unit]) -> bool:
        return any(u.distance_to(e) <= e.spec.attack_range * 1.3 for e in foes)

    @staticmethod
    def _threatened_at(u: Unit, pts: list[tuple[float, float]],
                       foes: list[Unit]) -> bool:
        """可见敌军按真实射程；记忆点用通用威胁半径（迷雾外仍可能被磨）。"""
        if any(u.distance_to(e) <= e.spec.attack_range * 1.3 for e in foes):
            return True
        avg_r = TILE * 5
        return any(math.hypot(u.x - px, u.y - py) <= avg_r * 1.3
                   for px, py in pts)

    @staticmethod
    def _far(u: Unit, pt: tuple[float, float], tol: float) -> bool:
        return math.hypot(u.x - pt[0], u.y - pt[1]) > tol

    @staticmethod
    def _behind(com: tuple[float, float],
                target_pt: tuple[float, float],
                dist: float) -> tuple[float, float]:
        """从 target_pt 指向 com 再延伸 dist：撤退/绕后的落脚点。"""
        dx, dy = com[0] - target_pt[0], com[1] - target_pt[1]
        n = math.hypot(dx, dy) or 1.0
        return com[0] + dx / n * dist, com[1] + dy / n * dist

    def _march(self, world, u: Unit, pt: tuple[float, float], force: bool = False) -> None:
        if not force and u.ai_cd > 0.0:
            return
        # 已有相近终点的路线：只刷新冷却，不重跑 A*（大批 AI 同帧决策时的主热点）
        if not force and u.path and u._path_goal is not None:
            gx, gy = u._path_goal
            if math.hypot(pt[0] - gx, pt[1] - gy) < TILE * 4:
                u.ai_cd = self.params.reaction * 0.8
                return
        # 帧预算：本帧 A* 次数用尽则缩短冷却，下帧再试
        if force:
            if getattr(world, "_path_budget", 1) <= 0 and u.path:
                u.ai_cd = 0.15
                return
            u.set_path([pt], world.map)
            if hasattr(world, "_path_budget"):
                world._path_budget = max(0, world._path_budget - 1)
        else:
            ok = world.try_repath(
                u, pt,
                min_interval=max(0.4, self.params.reaction * 0.9),
                goal_slack=TILE * 4)
            if not ok and not u.path:
                u.ai_cd = 0.12             # 预算不足且无路：尽快再试
                return
        u.retreating = force and bool(u.path)
        if force:
            u.target_uid = None
        u.ai_cd = self.params.reaction * (0.8 if u.path else 0.4)

    def _defensive_spot(self, world, u: Unit):
        """在附近找一处防御更好、可通行的格子据守；找不到则原地不动。"""
        gm = world.map
        cur = TERRAIN_INFO[gm.terrain_at(u.x, u.y)].defense
        best, best_score = None, cur + 0.05     # 需明显更好才动
        tx, ty = gm.tile_at(u.x, u.y)
        span = 5
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                nx, ny = tx + dx, ty + dy
                if not gm._inside(nx, ny):
                    continue
                info = TERRAIN_INFO[Terrain(gm.tiles[ny][nx])]
                if not info.passable:
                    continue
                # 偏好高防御、离得近
                score = info.defense - 0.03 * (abs(dx) + abs(dy))
                if score > best_score:
                    best, best_score = ((nx + 0.5) * TILE, (ny + 0.5) * TILE), score
        return best
