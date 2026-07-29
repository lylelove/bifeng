# 03 · 兵团与路线（`rts/units.py`）

## 1. 阵营

### `Faction(IntEnum)`

| 值 | 含义 |
| --- | --- |
| `PLAYER = 0` | 我军 |
| `ENEMY = 1` | 敌军 |

| 常量 | 内容 |
| --- | --- |
| `FACTION_COLOR` | PLAYER `(72,148,232)` · ENEMY `(214,76,68)` |
| `FACTION_NAME` | `"我军"` / `"敌军"` |

## 2. 据守常量

| 名 | 值 | 含义 |
| --- | --- | --- |
| `ENTRENCH_TIME` | `4.0` 秒 | 静止累计达此为满级 |
| `ENTRENCH_MAX` | `0.35` | 满级额外防御 **+35%** |

公式：

```
entrench = 1.0 + ENTRENCH_MAX * min(1, still_time / ENTRENCH_TIME)
```

战斗中最终防御：`def_info.defense * target.entrench`。

## 2b. 士气 / 克制 / 老练度常量

| 名 | 值 | 含义 |
| --- | --- | --- |
| `MORALE_ROUT` | 0.25 | 士气跌破此线 → 溃逃 |
| `MORALE_RALLY` | 0.55 | 溃逃中回升到此线 → 重整 |
| `MORALE_REGEN` | 0.05/s | 无敌情时回复 |
| `MORALE_REGEN_NEAR` | 0.012/s | 敌人在附近时的缓慢回复 |
| `ROUT_SPEED_MULT` | 1.18 | 溃兵移动加速 |
| `COUNTER_MULT` | 表 | (攻方,守方)→伤害倍率：骑克弓1.35/炮1.45，步克骑1.25，弓克步1.20，炮轰步1.15 |
| `VET_THRESHOLDS` | 0/260/700/1500 | 战功门槛（新兵/老练/精锐/禁卫） |
| `VET_DMG` | 0.08 | 每级输出 +8% |
| `VET_STEADY` | 0.14 | 每级士气损失 -14% |
| `VET_KILL_XP` | 150 | 击破一团折算战功 |

战功 `merit = dmg_dealt + VET_KILL_XP * kills`；`vet_level` 取最高达标档。

## 2c. 炮兵目标价值常量

| 名 | 值 | 含义 |
| --- | --- | --- |
| `ARTILLERY_V_HP` | 1.0 | 血量权重：越耐打越值得集火 |
| `ARTILLERY_V_ATK` | 8.0 | 攻击权重：把 DPS 折算成"威胁度" |
| `ARTILLERY_V_MULT` | 表 | 兵种偏好：`infantry` 1.4 / `artillery` 1.3 |

```
artillery_target_value(target) = (max_hp * ARTILLERY_V_HP
                                 + attack * ARTILLERY_V_ATK)
                                * ARTILLERY_V_MULT[type_key]
```

供 `World._acquire_targets`（炮兵索敌：价值主导、距离为次）与 `Commander`
（炮兵站位瞄准最高价值可见目标）共用。步兵加权最高——炮兵克步兵（+15%）且
步兵常密集结阵，重火力收益最大；反炮次之（互相威胁，先解决对方远程重火力）。

## 3. `UnitType`

| 字段 | 含义 |
| --- | --- |
| `name` | 中文名 |
| `max_hp` | 基础最大生命 |
| `speed` | 平原像素/秒 |
| `attack` | 伤害/秒（DPS） |
| `attack_range` | 射程（像素） |
| `radius` | 碰撞/选中半径 |
| `vision` | 基础视野 |
| `shape` | `circle` / `triangle` / `diamond` / `hexagon` |
| `letter` | 角标字：步/骑/弓/炮 |
| `attack_interval` | 齐射间隔（秒）；单轮伤害 = attack × interval |

### `UNIT_TYPES` 数值

| key | 名 | HP | 速 | 攻 | 射程 | 半径 | 视野 | 外形 | 齐射间隔 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `infantry` | 步兵团 | 520 | 42 | 12 | 48 | 13 | 210 | circle ● | 1.0s |
| `cavalry` | 骑兵团 | 400 | 78 | 15 | 34 | 12 | 250 | triangle ▲ | 0.9s |
| `archer` | 弓兵团 | 320 | 40 | 13 | 122 | 11 | 300 | diamond ◆ | 1.6s |
| `artillery` | 炮兵团 | 300 | 27 | 26 | 178 | 15 | 240 | hexagon ⬢ | 2.5s |

设计：血厚伤低 → 僵持；外形区分兵种，颜色区分敌我。

## 4. `Unit` 字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `uid` | — | 唯一 ID |
| `type_key` | — | `UNIT_TYPES` 键 |
| `faction` | — | 阵营 |
| `x, y` | — | 世界坐标 |
| `hp` | 0 → post_init 填满 | 当前生命 |
| `path` | `[]` | 航点队列（世界坐标） |
| `selected` | False | 是否选中（仅 UI/指令） |
| `attack_cd` | 0 | 齐射冷却：归零时打出一轮（DPS×interval） |
| `target_uid` | None | 当前目标 |
| `facing` | 0 | 朝向角（弧度） |
| `flash` | 0 | 受击闪烁剩余秒 |
| `ai_cd` | 0 | AI 再寻路冷却 |
| `dmg_mult` | 1.0 | 难度输出系数 |
| `hp_scale` | 1.0 | 难度血量系数 |
| `still_time` | 0 | 静止累计（据守） |
| `auto` | False | 托管：由我方 autopilot 指挥；手动下令（issue_*/stop）即解除 |
| `morale` | 1.0 | 士气 0..1；跌破 `MORALE_ROUT` 溃逃 |
| `routing` | False | 溃逃中：不受指挥、不索敌、向后方逃窜 |
| `kills` | 0 | 战绩：击破敌团数 |
| `dmg_dealt` | 0 | 战绩：累计输出 |
| `dmg_taken` | 0 | 战绩：累计承伤 |
| `_lx, _ly` | 位置 | 上一帧位置，用于位移检测 |

#### 行军指令状态

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `order` | `"move"` | 指令类型：`move`(接敌即停) / `attack_move`(接敌暂存路线、灭敌续程) / `patrol`(路线往返) |
| `travel_path` | `[]` | 攻击移动/巡逻接敌时暂存的剩余路线，灭敌后还回 `path` |
| `patrol_route` | `[]` | 巡逻基准路线；走完后反向续程，正序↔逆序往返 |
| `patrol_fwd` | `True` | 巡逻当前行进方向 |
| `order_queue` | `[]` | Shift 排队的后续指令（`{kind,...}`）；当前忙完后由 World 弹出 |
| `formation_lock` | `False` | 编队锁定：与同编队兵同步限速、保持队形 |
| `formation_id` | `0` | 所属编队号（0=无编队） |
| `_speed_cap` | `0.0` | 本帧编队限速（0=不限），由 `World._apply_formation_speeds` 每帧重算 |

### 属性

| 属性 | 逻辑 |
| --- | --- |
| `spec` | `UNIT_TYPES[type_key]` |
| `max_hp` | `spec.max_hp * hp_scale` |
| `alive` | `hp > 0` |
| `hp_ratio` | 夹到 [0,1] |
| `role` | `attack_range >= 90` → `"ranged"` 否则 `"melee"` |
| `entrench` | 见上式，静止 0 时为 1.0 |
| `moving` | `bool(path)` |
| `merit` / `vet_level` / `vet_name` | 战功与老练度（0..3：新兵/老练/精锐/禁卫） |

### 士气方法

| 方法 | 逻辑 |
| --- | --- |
| `shake_morale(amount)` | 掉士气；老练每级少掉 `VET_STEADY` |
| `update_morale(dt, foe_near)` | 回复（有敌情用慢速档）；跌破 ROUT → `routing=True`，清指令与选中（含 `travel_path`/`patrol_route`/order/编队，防止溃逃中续走巡逻或攻移路线）；回到 RALLY → 重整停下 |

溃逃中 `advance` 移速 ×`ROUT_SPEED_MULT`。

### 方法

#### `track_stillness(dt)`

- 位移 `> 1.4` 或 `path` 非空 → `still_time = 0`；
- 否则 `still_time += dt`。

#### `distance_to(other)`

欧氏距离。

#### `set_path(points, game_map)`

`path = build_route((x,y), points, game_map)`。

#### `advance(dt, game_map)`

沿 `path` 前进；巡逻兵团走完一段后自动反向续程，编队锁定时受 `_speed_cap` 限速：

```
# 巡逻续程：path 空、且未在接敌暂停（travel_path 空）时，反向续走 patrol_route
if not path and order=="patrol" and patrol_route and not travel_path:
    patrol_fwd = not patrol_fwd
    path = patrol_route if patrol_fwd else reversed(patrol_route)
speed = spec.speed * max(terrain.speed, 0.15)   # 兜底防卡死
if routing: speed *= ROUT_SPEED_MULT
if _speed_cap > 0: speed = min(speed, _speed_cap)   # 编队：以最慢兵为节拍同步
budget = speed * dt
while budget and path:
  朝下一航点移动；到达则 pop；否则按比例走完 budget
  更新 facing
```

## 5. 路径工具

### `resample_path(points, spacing=12.0)`

1. 去重近点（距离 < 1）；
2. 按 `spacing` 弧长重采样；
3. 中间点三点平滑：`(p_prev + 2*p + p_next) / 4`。

### `build_route(start, points, game_map)` — 画笔核心

```
sampled = resample_path(points, spacing=TILE*0.9)
丢弃开头距 start < TILE*0.6 的点（防原地打转）
cx,cy = start
for 每个采样点 (wx,wy):
  不可通 → nearest_passable
  若 line_clear(cx,cy,wx,wy): 原样追加
  否则: detour = find_path(...); 无路径则 break
  更新 cx,cy
return route
```

**语义：**

- 尊重玩家笔触（可直达段不改）；
- 水面等障碍用 A* 接回；
- 与 `World.issue_path` 配合：笔触先平移到各兵脚下，无「先跑到起笔点」。

## 6. role 分界

`attack_range >= 90` → 远程：

- 弓 122、炮 178 → ranged；
- 步 48、骑 34 → melee。

供 `Commander` 站位（远程躲前锋后）。
