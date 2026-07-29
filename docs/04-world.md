# 04 · 世界模拟（`rts/world.py`）

## 1. 常量

| 名 | 值 | 含义 |
| --- | --- | --- |
| `INFLUENCE_INTERVAL` | `0.4` 秒 | 势力场重算间隔（叠加脏检查，没变化则跳过） |
| `MAX_ATTACK_RANGE` / `MAX_UNIT_RADIUS` / `MAX_SPOT_RANGE` | 由兵种/地形表推出 | 空间哈希粗筛的圆域半径上界 |
| `FIELD_SIG_POS=8` / `FIELD_SIG_HP=32` | — | 势力场脏检查量化粒度（8px / 1/32 血量） |

## 2. `World` 状态

| 成员 | 类型 | 说明 |
| --- | --- | --- |
| `map` | `GameMap` | 本局地图 |
| `difficulty` | `Difficulty` | 难度枚举 |
| `params` | `DiffParams` | 难度参数表项 |
| `units` | `list[Unit]` | 全部存活单位（死后移除） |
| `_next_uid` | int | 自增 ID |
| `elapsed` | float | 累计秒 |
| `events` | `list[str]` | 战报，最多保留 200 条 |
| `event_seq` | int | 累计写入的战报总数（UI 据此判断有无新战报，长度截断后不再可靠） |
| `commander` | `Commander` | 敌军指挥官 |
| `field` | `InfluenceField\|None` | 最近势力场 |
| `_field_cd` | float | 重算倒计时 |
| `_field_sig` | tuple | 上次重算时的战场签名（脏检查） |
| `_grid` | `SpatialHash` | 近邻查询网格（96px 桶） |
| `_grid_dirty` | bool | 单位增删/移动后置脏，`_ensure_grid()` 惰性重建 |
| `_spot_known` / `_spot_known_t` | dict | 玩家对敌军的最后已知位置与时刻（与 AI `_known` 独立，TTL=`SPOT_MEMORY_TTL`） |
| `stats.force_history` | list | 军力采样 `(t, 我军, 敌军)`，间隔 `FORCE_SAMPLE_INTERVAL=0.5s` |
| `stats.timeline` | list | 关键事件 `(t, kind, text)`：交火/歼灭/溃逃/重整/溃散 |

指定攻击站位常量：`ATTACK_RANGED_STANDOFF=0.88`、`ATTACK_MELEE_STANDOFF=0.55`、`ATTACK_HOLD_SLACK=0.12`；战术撤退 `RETREAT_DIST=TILE*8`。

### 空间哈希（`rts/spatial_hash.py`）

- `rebuild(units)`：O(n) 按 `(x//96, y//96)` 分桶，只装存活单位；
- `query(x, y, r)`：粗筛——返回圆域覆盖桶内全部单位（**超集**，外扩
  `QUERY_PAD=16px` 兜底同帧内碰撞分离的小幅位移），精确判定归调用方；
- `iter_pairs()`：同桶全配对 + 半邻域（右/下/右下/左下）交叉配对，
  中心距 ≤ 96px 的对必然覆盖、每对只出一次——供 `_separate` 用；
- 消费方：`_acquire_targets` / `_separate` / `_update_morale` 敌情感知 /
  死亡士气波及 / `visible_to` / `unit_at`，全部由 O(n²) 降为 O(n·k)。

## 3. 初始化与刷兵

```python
World(map_width=160, map_height=120, seed=None, difficulty=EASY)
```

1. 建 `GameMap`；
2. 取 `DIFFICULTIES[difficulty]`；
3. `Commander(Faction.ENEMY, params)`；
4. `_spawn_armies()`；
5. `refresh_field()`。

### `_spawn_armies`

- RNG：`Random(map.seed ^ 0x1f2e3d)`；
- 基础编制（双方相同）：
  ```
  步×3, 骑×2, 弓×2, 炮×1
  ```
- 敌军额外：`extra_pool[:extra_units]`（困难 +2：步、弓）；
- 敌军 `hp_scale` / `dmg_mult` 用难度倍率；
- 位置：我军 `x≈12%` 宽，敌军 `x≈88%` 宽，纵向约中线排 4 列网格 + 抖动；
- `add_unit` 内 `nearest_passable` 吸附。

### `add_unit(type_key, faction, x, y, hp_scale=1, dmg_mult=1)`

创建 `Unit`，`hp = max_hp`，加入列表。

## 4. 查询

| 方法 | 说明 |
| --- | --- |
| `units_of(faction)` | 存活同阵营 |
| `unit_by_uid(uid)` | 存活单位或 None |
| `unit_at(wx, wy, slack=6)` | 点击半径内最近单位（走空间哈希） |
| `clear_units()` | 清空全部兵团并置脏网格（编辑器「清兵」） |
| `units_in_rect(x0,y0,x1,y1, faction=None)` | 矩形内（框选） |
| `selected` | 属性：存活且 selected |
| `winner()` | 一方全灭，或残部全部溃逃而对方仍有战力 → 对方 Faction；否则 None |
| `is_draw()` | 双方皆灭，或双方都只剩溃兵 → True（平局，避免战局挂死） |
| `snap_units_passable()` | 把站在不可通行处的兵团吸附到最近可通行点；返回移动数量 |

## 5. 指令

### `clear_selection()`

全员 `selected = False`。

### `_reset_order(u)` / `_apply_formation(units, formation)`

- `_reset_order`：清空一个兵的指令状态（order→`move`，`travel_path`/`patrol_route`/编队均清）。
- `_apply_formation`：`formation=True` 时给这批兵分配同一个 `formation_id`，供 update 同步限速。

### `issue_path(units, points, formation=False)`

画笔路线：笔触作「行进形状」平移到各兵脚下 → 多选保队形、无赶往起笔点。
`formation=True` 锁定编队同步限速。

```
hx,hy = points[0]
对每个存活 u:
  _reset_order(u)
  dx,dy = u.x-hx, u.y-hy
  u.path = build_route((u.x,u.y), [(x+dx,y+dy) for ...], map)
  u.target_uid = None; u.auto = False
_apply_formation(units, formation)
```

### `issue_move(units, wx, wy, formation=False)`

右键单击：以队中心为参考，各兵目标 = 点击点 + 相对中心偏移，再 `set_path`。

### `issue_attack(units, target, formation=False, queue=False)`

锁定 `order=attack` + `target_uid`。站位由 `_path_attackers_to_holds`：
远程落在 `attack_range×0.88` 环上按 uid 分槽，近战贴脸环分槽；已在环带内则清 path。
`_chase_attack_orders` 追向同一公式的 hold 点（cohort 动态重算 slot），不再全挤目标坐标。
`queue=True`（Shift）时追加到 `Unit.order_queue`，当前指令完成后再执行。

### 指令队列 `order_queue` / `_advance_order_queues`

- `Unit.order_queue`：`{kind, ...}` 列表（move/path/attack/attack_move/patrol）
- 忙判定 `_is_order_busy`：有 path/travel、撤退中、指定攻击目标仍在、或巡逻中
- 每帧 `_resume_orders` 后 `_advance_order_queues`：空闲则 pop 并 `_apply_order_entry`
- 非 queue 的 `issue_*` 清空队列；stop/溃逃/托管开启也清
- 巡逻持续忙碌，其后队列需 stop 后才会执行

### `issue_retreat(units)`

向出生侧（我军西 / 敌军东）`RETREAT_DIST` 短撤，`retreating=True`，途中 `_acquire_targets` 不接敌。

### `estimate_volley_damage` / `combat_preview`

稳态期望齐射（可选含侧击/冲锋）；悬停预览返回克制文案、下一轮 ±jitter 区间、合计 TTK。

### `issue_attack_move(units, points, formation=False)`

攻击移动：设 `order="attack_move"`。单击（单点）退化为队中心偏移目标点；多点走笔触平移。
接敌时 `_resolve_combat` 把剩余 `path` 暂存进 `travel_path` 并清空 `path`，灭敌后由
`_resume_orders` 把 `travel_path` 还回 `path` 续程——区别于普通移动的「接敌即永久停下」。

### `issue_patrol(units, points, formation=False)`

巡逻：设 `order="patrol"`，`patrol_route`=路线，`path`=副本。走完后 `advance` 自动反向续程，
在 `patrol_route` 上正序↔逆序往返。单击时 `patrol_route=[起点,终点]` 保证可往返；
多点在笔触两端往返。巡逻自带攻击移动语义（接敌暂存、灭敌续程）。

### `stop(units)`

`_reset_order` + 清空 `path`（停止也解除编队/巡逻/攻击移动状态）。

## 6. `update(dt)` 详解

见 [01-architecture](01-architecture.md) 管线。细节：

### 编队限速 `_apply_formation_speeds`（update 开头、advance 之前）

按 `formation_id` 分组，组内取各兵当前地形速度（`spec.speed * terrain.speed`，兜底 0.15）
的最小值，写入各兵 `_speed_cap`；非编队兵 `_speed_cap=0`（不限）。快兵被压到最慢者节拍，
保持队形不脱节。路线全部走完（含 `travel_path`）且非巡逻的兵退出编队。

### 灭敌续程 `_resume_orders`（索敌之后、战斗之前）

对 `order in (attack_move, patrol)` 的兵：若 `path` 空、`travel_path` 非空、且当前
`target_uid` 已无有效目标（死亡/超出射程被索敌清掉），则把 `travel_path` 还回 `path`
继续行军（用交换避免别名清空）。

### 索敌 `_acquire_targets`

```
reach = max(attack_range, vision * terrain.vision * 0.5)
若当前目标存活且距离 ≤ reach*1.15 → 保持
否则在 reach 内选 score 最小（候选集 = grid.query(u, reach)，非全体扫描）：
  炮兵：  score = distance*0.4 - artillery_target_value(e)*0.5   # 价值主导
  其他：  score = distance + (e.hp * 0.20 if use_focus else 0)
  溃兵：  score -= 70（背对且不还手，优先收割）
use_focus = params.focus_fire and 本单位是敌军
```

困难敌军集火残血（hp 进 score）；炮兵改用价值评分，让重火力落在血厚 / 威胁
大的高价值目标上，而非最近目标（见 [03-units](03-units.md) `artillery_target_value`）。

### 战斗 `_resolve_combat` + `_fire_volley`（齐射制）

对每个有目标且在 `attack_range` 内的单位：朝向目标、近身停止行军（按 `order` 分支：
`move` 直接清空 `path`；`attack_move`/`patrol` 先把剩余 `path` 暂存进 `travel_path` 再清空，
留待灭敌后由 `_resume_orders` 续程）；
`attack_cd` 归零时打出一轮**齐射**并重置为 `spec.attack_interval`
（步 1.0s / 骑 0.9s / 弓 1.6s / 炮 2.5s——DPS 不变，节奏变离散）。

单轮伤害（`_fire_volley`）：

```
dmg = attack * dmg_mult * atk_terrain.attack
      / (def_terrain.defense * target.entrench) * attack_interval
dmg *= 1 + VET_DMG * 攻方老练度                                    # 老练 +8%/级
dmg *= COUNTER_MULT.get((攻方兵种, 守方兵种), 1.0)                 # 兵种克制
dmg *= max(0.55, 1 + HIGH_GROUND_SCALE * (攻方海拔 - 守方海拔))   # 高差 ±0.6
rel = |攻方方位 - 守方朝向|:  >120° → ×FLANK_REAR_MULT(1.30)      # 背击
                              >60°  → ×FLANK_SIDE_MULT(1.12)      # 侧击
骑兵近战且 still_time<1.5 → ×CHARGE_MULT(1.30)                    # 冲势
dmg *= 1 ± VOLLEY_JITTER(0.10)      # 浮动，RNG = Random(seed^0x77aa11)
```

同时冲击守方士气：`shake = dmg/max_hp * MORALE_DMG_SCALE(1.15)`，
被侧击/背击 ×`MORALE_FLANK_BONUS(1.6)`，被骑兵冲锋命中额外 +`MORALE_CHARGE_SHAKE(0.06)`。

### 士气推进 `_update_morale`（每帧，在索敌之前）

- 「附近有无敌人」用**反向标记**：每个兵团按自己的 `attack_range*1.6`
  向周边桶散布威胁（`grid.query`），被覆盖者进 `threatened` 集合——
  与逐一比对语义一致，但查询圆按兵种缩小（步骑仅 ~77px）；
- 每个存活单位按敌情选择回复速率，调 `u.update_morale`；
- 溃逃触发/重整写入战报；
- 溃兵没有 path 时补一段逃跑路线 `_flee_point`（背向最近敌人 60% + 己方出生侧 40%，
  每段 `ROUT_LEG = TILE*7`）；
- 溃兵逃入地图边缘 2 格内 → 「溃散出战场」，按阵亡记账并移除；
- 友军覆灭时 `MORALE_ALLY_FALL(0.16)` 在 `MORALE_ALLY_FALL_R(220px)` 内随距离衰减冲击周边同阵营士气（死亡清理处，只查阵亡点周边桶）。

### 侦察 `spot_range` / `visible_to`

`spot_range(viewer, target) = viewer.vision * 所在地形.vision * SPOT_GRACE(1.1)`，
目标在森林 ×`FOREST_CONCEAL(0.60)`。`visible_to(faction, target)`：任一己方兵团
够得着即可见——战争迷雾渲染与悬停都走这里；只查 target 周围
`MAX_SPOT_RANGE`（≈462px）内的桶，能看见它的兵团必在此圆内。索敌 `_acquire_targets` 对藏身森林的
目标同样按此折扣（但不低于武器射程）。

### 玩家敌情记忆 `_refresh_player_spot_memory` / `player_memory_ghosts`

每帧 update 末：对 **PLAYER 可见** 的敌军刷新 `_spot_known` 位置与时刻；阵亡或超过
`SPOT_MEMORY_TTL`(8s，与 AI 共用常量) 遗忘。`player_memory_ghosts()` 返回
`(x, y, age_ratio, uid)`——当前不可见的记忆点，供小地图淡红点与主地图残影绘制。
与 `Commander._known` 独立（那边是 AI 对玩家的记忆）。

结算：`target.flash=0.22`；伤害与击破计入 `u.kills/dmg_dealt`、
`target.dmg_taken` 及 `stats`（首次伤害记 `first_blood` 并写战报）。

### 统计 `BattleStats` / `SideStats`

`world.stats`：双方 `kills / losses / dmg_dealt / dmg_taken / losses_by_type`、
`volleys` 总轮次、`first_blood` 时刻、`fallen` 阵亡名录（faction、名#uid、kills、输出）。
死亡清理时记账并在战报附「曾击破 N 团」。

### 结算 `battle_summary(winner) -> str`

HTML 文本：时长、齐射轮数、首次交火、双方存活/损失（按兵种细分）/击破/输出/承伤、
全场最佳（`score = dmg_dealt + 150*kills`，含阵亡单位）。供结束对话框。

### 分离 `_separate`

圆碰撞分离：候选对来自 `grid.iter_pairs()`（同桶/邻桶配对，O(n·k)），
重叠则各推一半；新位置须 `passable` 才写入（防推进水）。结束后置脏网格。

### 势力 `refresh_field(force=False)`

先算战场签名 `_field_signature()`（map_uid + terrain_version + 各兵团量化
位置/血量）：与上次相同则只重置 CD **不重算不 bump version**（渲染层缓存
也因此不失效）；有变化才 `compute_field` 并 `field_version += 1`。

### 战报 `log(text)`

`[MM:SS] text`，`events` 截断保留末 200。

## 7. 胜负

`winner()`：一方全灭 → 对方胜；一方只剩溃兵而对方仍有战斗力 → 对方胜
（溃兵终将逃散，胜局已定）；其余 None。

## 8. 与 UI 的边界

- UI / MapView **只**通过上述指令与查询操作世界；
- 不直接改 `hp` / `path`（AI 例外：Commander 调 `Unit.set_path`）。
