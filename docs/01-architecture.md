# 01 · 架构与数据流

## 1. 分层

```
┌─────────────────────────────────────────────┐
│  UI 层 (ui.py)                              │
│  GameShell → StartScreen / BattlePage       │
│  SidePanel · StrengthBar · 快捷键 · 定时器 │
└──────────────────┬──────────────────────────┘
                   │ 持有 / 驱动
┌──────────────────▼──────────────────────────┐
│  视图层 (mapview.py)                        │
│  MapView：渲染 + 输入 → 调用 World 指令     │
└──────────────────┬──────────────────────────┘
                   │ 读写
┌──────────────────▼──────────────────────────┐
│  模拟层 (world.py)                          │
│  World：update 管线、指令、查询、胜负       │
└───┬─────────┬──────────┬──────────┬─────────┘
    │         │          │          │
    ▼         ▼          ▼          ▼
 terrain   units       ai      influence
  地图      兵团     Commander   势力场
```

**原则：**

- 逻辑层不 import Qt；
- 视图只读 `World` 状态并调用指令方法，不直接改战斗公式；
- `Commander` 通过 `Unit.set_path` 下指令，与玩家路径共用同一套路线构建。

## 2. 启动与页面切换

```
main.py
  └─ ui.run()
       ├─ QApplication + DARK_QSS
       └─ GameShell
            ├─ StartScreen  ──start_requested───►  _start_game   （新建 BattlePage）
            │                ──editor_requested──►  _open_editor  （新建 EditorPage）
            ├─ BattlePage   ──exit_to_menu──►  _to_menu / _playtest_to_editor
            └─ EditorPage   ──exit_to_menu──►  _editor_to_menu（回 StartScreen）
                            ──playtest_requested(World)──►  _start_playtest（克隆世界开战）
```

`GameShell` 用 `QStackedWidget` 在开始页、战斗页与编辑器页之间切换。编辑器「试玩」用
`world.clone()` 深拷贝开战，战斗伤亡不影响编辑器剧本；试玩结束回到仍在栈中的编辑器。

## 3. 战斗主循环

`BattlePage` 内 `QTimer(TICK_MS=33)` → `_tick()`：

```
_tick():
  if not paused and not ended:
    for _ in range(speed):          # 1 / 2 / 4 倍速
      world.update(dt=0.033)
    if world.winner():
      结束对话框
  panel.refresh()
  view.update()                     # 触发 paintEvent
```

## 4. World.update 管线（每帧）

顺序固定，见 `world.py`：

```
1. elapsed += dt
2. 各存活单位：flash/ai_cd 衰减 → Unit.advance（沿路径移动）
3. 各单位 track_stillness（据守计时）；空间哈希置脏
4. _update_morale（士气/溃逃；敌情感知走空间哈希）
5. _acquire_targets（索敌；候选集走空间哈希）
6. _resolve_combat（伤害）
7. _separate（圆形分离；候选对走空间哈希邻桶配对）
8. commander.update（敌军 AI，按 reaction 节流）
9. 势力场 CD 到期则 refresh_field（带脏检查，战场没变直接跳过）
10. 移除死亡单位并写战报（士气波及只查阵亡点周边桶）
```

**近邻查询（`spatial_hash.py`）**：`World` 持有一张 96px 方格的 `SpatialHash`，
单位增删/移动后置脏、查询时惰性重建（O(n)）。索敌、碰撞分离、敌情感知、
战争迷雾 `visible_to`、点选 `unit_at` 都只翻查询圆覆盖的几个桶（O(k)），
不再全体扫描——原先这些环节都是 O(n²)。网格只做**粗筛**（返回超集），
精确的距离/阵营/存活判定仍由调用方完成，因此战斗语义与旧实现完全一致。

## 5. 玩家指令路径

```
MapView 鼠标
  左键点兵 / 框选  →  Unit.selected
  右键单击         →  World.issue_move(sel, wx, wy)
  右键拖拽松手     →  World.issue_path(sel, brush_points)
       │
       ├─ issue_move: 以队中心为参考，保持相对队形收拢到目标
       └─ issue_path: 笔触平移到各兵脚下，再 build_route
            └─ resample_path → line_clear / find_path(A*)
```

## 6. 模块依赖图（import）

```
ui ──► mapview, world, ai, terrain, units, editor
mapview ──► world, terrain, units
world ──► ai, influence, spatial_hash, terrain, units
editor ──► world, terrain, units
ai ──► terrain, units
influence ──► terrain, units
units ──► terrain
spatial_hash ──► (stdlib only)
terrain ──► (stdlib only)
```

无环依赖。`mapview` 在编辑态持有一个 `editor.MapEditor`（鸭子类型，不 import editor）。

## 7. 关键数据对象

| 对象 | 生命周期 | 说明 |
| --- | --- | --- |
| `GameMap` | 一局一场 | 种子决定地形；像素尺寸 = 格数 × 16 |
| `Unit` | 一局内动态 | 死亡后从 `world.units` 移除 |
| `Commander` | 随 World | 仅指挥敌军 |
| `InfluenceField` | 每 0.4s 重建 | `world.field` 缓存最近结果 |
| `MapView` 相机 | 会话 | `cam_x/y` + `zoom` |
| 地形 `QPixmap` 缓存 | 换图时 invalidate | 新战场时重建 |

## 8. 扩展建议（查阅用）

| 想改… | 优先看 |
| --- | --- |
| 兵种数值 / 外形 | `units.UNIT_TYPES`、`MapView._shape_polygon` |
| 地形效果 / 生成 | `terrain.TERRAIN_INFO`、`GameMap.__init__` |
| 战斗公式 | `World._resolve_combat` |
| AI 行为 | `ai.Commander`、`DiffParams` |
| 势力显示 | `influence` + `MapView._draw_territory` |
| 操作手感 | `MapView` 鼠标事件 + `World.issue_*` |
| UI 布局 / 主题 | `ui.py` 各 Widget + `DARK_QSS` |
| 地图编辑器 | `editor.MapEditor` + `ui.EditorPage`（见 [11-editor](11-editor.md)） |
