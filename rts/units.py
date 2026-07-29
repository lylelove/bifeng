"""兵团（Unit）与阵营定义，以及沿手绘路径行军的逻辑。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum

from .terrain import TERRAIN_INFO, TILE, GameMap, Terrain


class Faction(IntEnum):
    PLAYER = 0
    ENEMY = 1


FACTION_COLOR = {
    Faction.PLAYER: (72, 148, 232),
    Faction.ENEMY: (214, 76, 68),
}
FACTION_NAME = {Faction.PLAYER: "我军", Faction.ENEMY: "敌军"}

# 据守：静止一段时间后逐步获得防御加成，鼓励结阵坚守、拉出僵持感
ENTRENCH_TIME = 4.0       # 秒，达到此时长即满级据守
ENTRENCH_MAX = 0.35       # 满级据守额外防御 +35%

# 士气：受创与目睹友军覆灭会动摇军心；跌破崩溃线即溃逃，脱战后可重整
MORALE_ROUT = 0.25        # 士气跌破此线 → 溃逃
MORALE_RALLY = 0.55       # 溃逃中回升到此线 → 重整归队
MORALE_REGEN = 0.05       # 无敌情时每秒回复
MORALE_REGEN_NEAR = 0.012 # 敌人仍在附近时的缓慢回复
ROUT_SPEED_MULT = 1.18    # 溃兵跑得比列队行军快（丢盔弃甲）

# 兵种克制：攻方兵种 → 守方兵种 的伤害倍率（未列出的组合为 1.0）
# 骑兵冲垮远程、步兵结阵拒马、弓兵放风筝步兵、炮兵轰击密集步阵。
COUNTER_MULT: dict[tuple[str, str], float] = {
    ("cavalry", "archer"): 1.35,
    ("cavalry", "artillery"): 1.45,
    ("infantry", "cavalry"): 1.25,
    ("archer", "infantry"): 1.20,
    ("artillery", "infantry"): 1.15,
}

# 老练度：按战功（输出 + 击破）晋升，输出更高、军心更稳
VET_THRESHOLDS = (0.0, 260.0, 700.0, 1500.0)     # 新兵/老练/精锐/禁卫 的战功门槛
VET_NAMES = ("新兵", "老练", "精锐", "禁卫")
VET_DMG = 0.08            # 每级输出 +8%
VET_STEADY = 0.14         # 每级士气损失 -14%
VET_KILL_XP = 150.0       # 击破一团折算的战功

# 炮兵目标价值：远程重火力优先打击高价值目标（血厚、威胁大的点）。
# 克制收益（炮兵克步兵 +15%、步兵常密集结阵）与反炮（互相威胁）进一步加权。
ARTILLERY_V_HP = 1.0            # 血量权重：越耐打越值得集火
ARTILLERY_V_ATK = 8.0           # 攻击权重：把 DPS 折算成"威胁度"
ARTILLERY_V_MULT: dict[str, float] = {
    "infantry": 1.4,            # 炮兵克步兵，且步兵扎堆最吃重火力
    "artillery": 1.3,           # 反炮：先解决对方远程重火力
}


def artillery_target_value(target: "Unit") -> float:
    """炮兵眼中的目标价值：高血高伤优先，步兵/炮兵再乘偏好系数。"""
    v = (target.max_hp * ARTILLERY_V_HP
         + target.spec.attack * ARTILLERY_V_ATK)
    v *= ARTILLERY_V_MULT.get(target.type_key, 1.0)
    return v


@dataclass
class UnitType:
    name: str
    max_hp: float
    speed: float          # 像素 / 秒（平原）
    attack: float         # 伤害 / 秒（DPS，齐射伤害 = attack * attack_interval）
    attack_range: float   # 像素
    radius: float         # 圆圈半径（像素）
    vision: float         # 像素
    shape: str            # 兵种外形：circle / triangle / diamond / hexagon
    letter: str           # 角标字母
    attack_interval: float = 1.0   # 齐射间隔（秒）：一次打出 interval 秒的伤害


# 数值偏「僵持」：血厚、单位时间伤害低，交火要打很久，据守方尤其难啃。
# 兵种以外形区分：步兵=圆、骑兵=三角、弓兵=菱形、炮兵=六边形。
# 齐射间隔：步骑短促、弓一轮箭雨、炮一发重弹——DPS 不变，打击变「一下一下」。
UNIT_TYPES: dict[str, UnitType] = {
    "infantry":  UnitType("步兵团", 520, 42, 12, 48, 13, 210, "circle", "步", 1.0),
    "cavalry":   UnitType("骑兵团", 400, 78, 15, 34, 12, 250, "triangle", "骑", 0.9),
    "archer":    UnitType("弓兵团", 320, 40, 13, 122, 11, 300, "diamond", "弓", 1.6),
    "artillery": UnitType("炮兵团", 300, 27, 26, 178, 15, 240, "hexagon", "炮", 2.5),
}


@dataclass
class Unit:
    uid: int
    type_key: str
    faction: Faction
    x: float
    y: float
    hp: float = 0.0
    path: list[tuple[float, float]] = field(default_factory=list)
    selected: bool = False
    attack_cd: float = 0.0
    target_uid: int | None = None
    facing: float = 0.0
    flash: float = 0.0        # 受击闪烁计时
    ai_cd: float = 0.0        # AI 重新寻路的冷却
    dmg_mult: float = 1.0     # 难度带来的输出系数
    hp_scale: float = 1.0     # 难度带来的血量系数
    still_time: float = 0.0   # 静止累计时长（据守）
    auto: bool = False        # 托管：交给己方 AI 指挥（玩家手动下令即解除）
    morale: float = 1.0       # 士气 0..1，跌破 MORALE_ROUT 即溃逃
    routing: bool = False     # 溃逃中：不受指挥、不开火，向后方逃窜
    retreating: bool = False  # AI 主动撤退中：行军期间不重新索敌，避免刚撤就被战斗逻辑钉住
    kills: int = 0            # 战绩：击破的敌方兵团数
    dmg_dealt: float = 0.0    # 战绩：累计造成伤害
    dmg_taken: float = 0.0    # 战绩：累计承受伤害
    # ---- 行军指令状态 ----
    # order：move=普通行军(接敌即停) / attack=指定攻击(锁定目标追击)
    #        / attack_move=攻击移动(接敌暂存路线,灭敌后续程)
    #        / patrol=巡逻(在 patrol_route 上往返,遇敌交战后继续)
    order: str = "move"
    travel_path: list[tuple[float, float]] = field(default_factory=list)  # 攻击移动/巡逻接敌时暂存的剩余路线
    patrol_route: list[tuple[float, float]] = field(default_factory=list)  # 巡逻基准路线(往返用)
    patrol_fwd: bool = True       # 巡逻当前行进方向(True=正序)
    # Shift 排队的后续指令：dict 列表，kind=move/path/attack/attack_move/patrol
    order_queue: list = field(default_factory=list)
    formation_lock: bool = False  # 编队锁定：与同编队兵同步限速、保持队形
    formation_id: int = 0         # 所属编队号(0=无编队)
    _speed_cap: float = 0.0       # 本帧编队限速(0=不限)，由 World.update 每帧重算
    _repath_cd: float = 0.0       # 重寻路冷却：追击/撞障/AI 共用，避免每帧 A*
    _path_goal: tuple[float, float] | None = None  # 当前 path 的逻辑终点（节流用）
    _lx: float = 0.0
    _ly: float = 0.0

    def __post_init__(self) -> None:
        if self.hp <= 0:
            self.hp = self.max_hp
        self._lx, self._ly = self.x, self.y

    @property
    def spec(self) -> UnitType:
        return UNIT_TYPES[self.type_key]

    @property
    def max_hp(self) -> float:
        return self.spec.max_hp * self.hp_scale

    @property
    def alive(self) -> bool:
        return self.hp > 0

    @property
    def hp_ratio(self) -> float:
        return max(0.0, min(1.0, self.hp / self.max_hp))


    @property
    def role(self) -> str:
        """melee / ranged —— 供 AI 站位判断。"""
        return "ranged" if self.spec.attack_range >= 90 else "melee"

    # ------------------------------------------------------------ 老练度
    @property
    def merit(self) -> float:
        """战功：输出 + 击破折算，决定老练度。"""
        return self.dmg_dealt + VET_KILL_XP * self.kills

    @property
    def vet_level(self) -> int:
        """老练度等级 0..3（新兵/老练/精锐/禁卫）。"""
        m = self.merit
        lvl = 0
        for i, th in enumerate(VET_THRESHOLDS):
            if m >= th:
                lvl = i
        return lvl

    @property
    def vet_name(self) -> str:
        return VET_NAMES[self.vet_level]

    # ------------------------------------------------------------ 士气
    def shake_morale(self, amount: float) -> None:
        """军心动摇：老练部队更沉着（每级少掉 VET_STEADY）。"""
        self.morale = max(0.0, self.morale
                          - amount * (1.0 - VET_STEADY * self.vet_level))

    def update_morale(self, dt: float, foe_near: bool) -> None:
        """每帧回复士气；溃逃中回过 MORALE_RALLY 即重整，跌破 MORALE_ROUT 即崩溃。"""
        # 先判崩溃、再回复；否则刚跌破阈值的单位可能被本帧回复抬回阈值上方，
        # 从而永久跳过一次本应触发的溃逃。
        if not self.routing and self.morale < MORALE_ROUT:
            self.routing = True
            self.retreating = False
            self.target_uid = None
            self.selected = False
            self.path.clear()
            # 溃兵不受指挥：清掉全部行军指令状态，避免 advance 里巡逻续程 /
            # 恢复暂存路线把溃兵又送回前线；编队锁也一并解除，不拖累同队。
            self.travel_path.clear()
            self.patrol_route.clear()
            self.order_queue.clear()
            self.order = "move"
            self.formation_lock = False
            self.formation_id = 0
            self._speed_cap = 0.0

        regen = MORALE_REGEN_NEAR if foe_near else MORALE_REGEN
        self.morale = min(1.0, self.morale + regen * dt)
        if self.routing and self.morale >= MORALE_RALLY:
            self.routing = False
            self.path.clear()      # 停下来重整，等待新指令

    @property
    def entrench(self) -> float:
        """据守防御系数（1.0 起，静止越久越高，封顶 1+ENTRENCH_MAX）。"""
        if self.still_time <= 0.0:
            return 1.0
        return 1.0 + ENTRENCH_MAX * min(1.0, self.still_time / ENTRENCH_TIME)

    @property
    def moving(self) -> bool:
        return bool(self.path)

    def track_stillness(self, dt: float) -> None:
        """由 world 每帧调用：位移很小则累积据守时间，移动则清零。"""
        moved = math.hypot(self.x - self._lx, self.y - self._ly)
        self._lx, self._ly = self.x, self.y
        if moved > 1.4 or self.path:
            self.still_time = 0.0
        else:
            self.still_time += dt

    def distance_to(self, other: "Unit") -> float:
        return math.hypot(other.x - self.x, other.y - self.y)


    # ------------------------------------------------------------ 行军
    def set_path(self, points: list[tuple[float, float]], game_map: GameMap) -> None:
        """接收一条手绘折线作为行军路线，做重采样并绕开不可通行地形。"""
        self.path = build_route((self.x, self.y), points, game_map)
        self._path_goal = points[-1] if points else None
        self._repath_cd = max(self._repath_cd, 0.35)

    def set_path_throttled(self, point: tuple[float, float], game_map: GameMap,
                           min_interval: float = 0.45,
                           goal_slack: float = TILE * 3.0) -> bool:
        """带节流的单点寻路：目标未明显移动且冷却未到则跳过，返回是否真正重寻。"""
        if self._repath_cd > 0.0 and self.path and self._path_goal is not None:
            gx, gy = self._path_goal
            if math.hypot(point[0] - gx, point[1] - gy) < goal_slack:
                return False
        if self._repath_cd > 0.0 and self.path:
            # 冷却中但目标已漂走：仍等冷却结束，避免尖峰
            end = self.path[-1]
            if math.hypot(point[0] - end[0], point[1] - end[1]) < goal_slack:
                return False
            if self._repath_cd > min_interval * 0.25:
                return False
        self.set_path([point], game_map)
        self._repath_cd = min_interval
        return True

    def advance(self, dt: float, game_map: GameMap) -> None:
        """沿路径前进；速度受当前地形影响。

        巡逻兵团走完一段后会自动反向续程（在 patrol_route 上往返）；
        编队锁定时受 `_speed_cap` 限速，与同编队兵以最慢者为节拍同步，保持队形。
        """
        if self._repath_cd > 0.0:
            self._repath_cd = max(0.0, self._repath_cd - dt)
        # 若因编辑器改地形等原因站在不可通行处，先吸附到岸上再走
        if not game_map.passable(self.x, self.y):
            self.x, self.y = game_map.nearest_passable(self.x, self.y)
        # 巡逻续程：当前 path 走完、且未在接敌暂停（travel_path 空）时，反向续走 patrol_route
        if (not self.path and self.order == "patrol"
                and self.patrol_route and not self.travel_path):
            self.patrol_fwd = not self.patrol_fwd
            self.path = (list(self.patrol_route) if self.patrol_fwd
                         else [p for p in reversed(self.patrol_route)])
        if not self.path:
            return
        info = TERRAIN_INFO[game_map.terrain_at(self.x, self.y)]
        speed = self.spec.speed * max(info.speed, 0.15)   # 兜底，避免完全卡死
        if self.routing:
            speed *= ROUT_SPEED_MULT                      # 溃兵丢盔弃甲，跑得更快
        if self._speed_cap > 0.0:                         # 编队锁定：以最慢兵为节拍同步
            speed = min(speed, self._speed_cap)
        budget = speed * dt
        while budget > 0.0 and self.path:
            tx, ty = self.path[0]
            dx, dy = tx - self.x, ty - self.y
            dist = math.hypot(dx, dy)
            if dist <= 1e-6:
                self.path.pop(0)
                continue
            self.facing = math.atan2(dy, dx)
            if dist <= budget:
                # 航点偶发落水时吸附，避免卡进湖心
                if game_map.passable(tx, ty):
                    self.x, self.y = tx, ty
                else:
                    self.x, self.y = game_map.nearest_passable(tx, ty)
                self.path.pop(0)
                budget -= dist
            else:
                k = budget / dist
                nx, ny = self.x + dx * k, self.y + dy * k
                if game_map.passable(nx, ny):
                    self.x, self.y = nx, ny
                else:
                    # 撞障：冷却内只跳过本航点；冷却外 A* 绕行（避免每帧重寻尖峰）
                    self.path.pop(0)
                    if self._repath_cd <= 0.0:
                        detour = game_map.find_path(self.x, self.y, tx, ty)
                        if detour:
                            self.path = detour + self.path
                        self._repath_cd = 0.4
                budget = 0.0


# ---------------------------------------------------------------- 路径工具

def resample_path(points: list[tuple[float, float]],
                  spacing: float = 12.0) -> list[tuple[float, float]]:
    """把手绘轨迹按固定间距重采样并轻度平滑（不做地形处理）。"""
    if not points:
        return []
    pts = [points[0]]
    for p in points[1:]:
        if math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) >= 1.0:
            pts.append(p)
    if len(pts) == 1:
        return [pts[0]]

    out: list[tuple[float, float]] = []
    carry = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        t = carry
        while t < seg:
            k = t / seg
            out.append((x0 + (x1 - x0) * k, y0 + (y1 - y0) * k))
            t += spacing
        carry = t - seg
    out.append(pts[-1])

    if len(out) >= 3:                       # 去掉手抖锯齿
        smoothed = [out[0]]
        for i in range(1, len(out) - 1):
            px, py = out[i - 1]
            cx, cy = out[i]
            nx, ny = out[i + 1]
            smoothed.append(((px + 2 * cx + nx) / 4.0, (py + 2 * cy + ny) / 4.0))
        smoothed.append(out[-1])
        out = smoothed
    return out


def build_route(start: tuple[float, float], points: list[tuple[float, float]],
                game_map: GameMap) -> list[tuple[float, float]]:
    """把一条可能穿越水面的手绘轨迹，变成一条真正可走的行军路线。

    做法：沿手绘线依次前进，能直达的段落原样保留（尊重玩家的笔触）；
    一旦下一段被湖泊之类的障碍挡住，就用 A* 绕过去再接回手绘线。
    """
    sampled = resample_path(points, spacing=TILE * 0.9)
    if not sampled:
        return []

    # 丢弃开头贴着起点的采样点，避免刚下令就原地打转
    while len(sampled) > 1 and math.hypot(sampled[0][0] - start[0],
                                          sampled[0][1] - start[1]) < TILE * 0.6:
        sampled.pop(0)

    route: list[tuple[float, float]] = []
    cx, cy = start
    for wx, wy in sampled:
        if not game_map.passable(wx, wy):
            wx, wy = game_map.nearest_passable(wx, wy)
        if math.hypot(wx - cx, wy - cy) < 1e-6:
            continue
        if game_map.line_clear(cx, cy, wx, wy):
            route.append((wx, wy))
        else:
            detour = game_map.find_path(cx, cy, wx, wy)
            if not detour:
                continue            # 本段不可达：跳过，继续尝试后续笔迹
            route.extend(detour)
        cx, cy = route[-1] if route else (cx, cy)
    return route


