# 10 · 数值常量与公式汇总

便于调参时一处查全。

## 1. 地图与时间

| 常量 | 位置 | 值 |
| --- | --- | --- |
| TILE | terrain | 16 px/格 |
| 默认地图 | World / GameMap | 160×120 格 |
| TICK_MS | ui | 33 ms |
| 模拟 dt | BattlePage | 0.033 s × speed |
| INFLUENCE_INTERVAL | world | 0.4 s |
| INF_STEP | influence | 3 地形格 |
| INF_RADIUS | influence | 150 px |
| MIN/MAX_ZOOM | mapview | 0.35 / 3.0 |

## 2. 据守

| 常量 | 值 |
| --- | --- |
| ENTRENCH_TIME | 4.0 s |
| ENTRENCH_MAX | 0.35（+35% 防） |
| 位移清零阈值 | 1.4 px/帧累计判定 |
| 光环显示阈值 | still > 0.4 × ENTRENCH_TIME |

```
entrench = 1 + ENTRENCH_MAX * clamp(still_time / ENTRENCH_TIME, 0, 1)
```

## 3. 地形倍率

见 [02-terrain](02-terrain.md) 表。高程/湿度阈值：

| 阈值 | 用途 |
| --- | --- |
| e < 0.20 | 湖 |
| e > 0.76 | 山 |
| e > 0.63 | 丘 |
| m > 0.60 | 林 |
| 河源 e > 0.70 | 河流起点 |

## 4. 兵种

见 [03-units](03-units.md) `UNIT_TYPES`。  
`role`：`attack_range >= 90` → ranged。

## 5. 难度

见 [05-ai](05-ai.md)。刷兵基础编制：

```
infantry×3, cavalry×2, archer×2, artillery×1
+ enemy extra_pool[:extra_units]
extra_pool = infantry, archer, cavalry, artillery
```

## 6. 战斗公式（齐射制）

```
reach = max(attack_range, vision * terrain.vision * 0.5)
# 保持目标: dist <= reach * 1.15
# 集火 score: dist + (e.hp * 0.20 if focus else 0)；溃兵 score -70
# 炮兵 score: dist*0.4 - artillery_target_value*0.5（价值主导、距离为次）
# 溃兵不索敌、不开火

defense = def_terrain.defense * target.entrench
# 冷却归零打一轮：伤害 = DPS × attack_interval × 修正
dmg = attack * dmg_mult * atk_terrain.attack / defense * interval
dmg *= 1 + VET_DMG * vet_level
dmg *= COUNTER_MULT.get((atk, def), 1.0)
dmg *= max(0.55, 1 + 0.6 * (atk_elev - def_elev))
# 背击 ×1.30 / 侧击 ×1.12；骑兵冲势 ×1.30；齐射 ±10%

# 近战停步: dist < attack_range * 0.7 → clear path
# 受击闪烁: flash = 0.22 s
# 胜负: winner() / is_draw()（双方皆灭或皆溃 → 平局）
```

## 7. 移动

```
speed = unit.speed * max(terrain.speed, 0.15)
resample spacing (画笔): TILE * 0.9
resample 默认工具: 12 px
丢弃起笔近点: TILE * 0.6
画笔采样最小间隔(屏幕): 4 / zoom 世界单位
```

## 8. AI 阈值（困难）

| 项 | 值 |
| --- | --- |
| 进攻判定 | my_hp_sum >= foe_hp_sum × 0.85 |
| 撤退血量 | hp_ratio < 0.30 |
| 威胁距离 | 敌 range × 1.3 |
| 交火判定 | 目标 dist ≤ own range × 1.05 |
| 远程跟进容差 | TILE × 2.5 |
| 近战追 focus | TILE × 3 |
| 先集结 rally | 距 rally > TILE × 8 |
| 后撤距离 | TILE × 6 |
| 防点搜索半径 | 5 格 |
| 防点提升门槛 | 当前 defense + 0.05 |
| focus 选敌 | min(hp + 0.15 × dist_to_com) |

## 8b. 侦察 / 包抄 / 记忆（战争迷雾约束）

| 常量 | 值 | 含义 |
| --- | --- | --- |
| SPOT_MEMORY_TTL | 8.0 s | 困难/托管记忆时长 |
| SPOT_MEMORY_TTL_EASY | 4.0 s | 简单记忆时长（弱化追击） |
| FLANK_OFFSET | TILE×9 | 骑兵侧翼集结点相对敌军质心的横向偏移 |
| FLANK_BEHIND | TILE×3 | 侧翼切入时压向敌军纵深的距离 |
| CAVALRY_FLANK_FAR | TILE×4 | 骑兵距集结点超过此值先去集结 |
| ARTY_GUARD_RANGE | TILE×6 | 炮兵护卫触发半径 |
| FEINT_FRACTION | 0.30 | 佯动分兵比例 |
| 侦察远点 | 地图对侧 85%/15% 宽处 | 无可见敌军时的推进目标（固定远点，防死锁） |
| 炮兵索敌 | score = dist×0.4 − value×0.5 | 价值主导、距离为次（见 03-units） |
| ATTACK_RANGED_STANDOFF | 0.88×射程 | 指定攻击远程站位环 |
| ATTACK_MELEE_STANDOFF | max(半径和, 0.55×射程) | 指定攻击近战贴脸 |
| RETREAT_DIST | TILE×8 | 玩家战术撤退距离 |

Commander 只对 `world.visible_to` 可见的敌军下令进攻 / 集火 / 包抄；不可见
者靠 `_known` 记忆的最近点维持推进，无记忆则朝推断的敌方出生侧远点侦察。

## 9. 势力

```
presence = 0.5 + hp_ratio
contrib = presence * (1 - dist² / R²)   # R=INF_RADIUS 处硬截断，超出无贡献
owner = sign(pv - ev)  # 平局归我; 双零格由 BFS 泛洪染色; 双方全无用 default
面积: 仅 passable 格
```

## 10. 碰撞分离

```
min_d = r_a + r_b
push = (min_d - d) * 0.5  # 各半
新位置须 passable
```

## 11. 种子

| 用途 | 派生 |
| --- | --- |
| 地图 | 用户 seed 或随机 |
| 湿度噪声 | seed ^ 0x5bf03635 |
| 刷兵抖动 | seed ^ 0x1f2e3d |
| 文本种子 | zlib.crc32(text) % (1<<30)（跨进程可复现） |

## 12. UI 尺寸

| 项 | 值 |
| --- | --- |
| 窗口默认 | 1380×880 |
| 侧栏宽 | 304 |
| 小地图目标边 | 172 |
| MapView 最小 | 640×480 |
| 编辑器面板宽 | 304（滚动区 322） |

## 13. 编辑器

| 常量 / 项 | 位置 | 值 |
| --- | --- | --- |
| BRUSH_RADII | editor | (0, 1, 2) → 1×1 / 3×3 / 5×5 |
| SCENARIO_VERSION | world | 1（剧本存档格式版本） |
| NOMINAL_ELEVATION | terrain | 湖.10 河.30 平.45 林.50 丘.68 山.85 |
| 擦除命中半径 | editor | `unit_at(slack=8)` |
| 剧本格式 | world | JSON：seed/difficulty/尺寸/tiles/elevation/units |
