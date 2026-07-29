# 11 · 地图编辑器（`rts/editor.py` + `ui.EditorPage`）

在自然地形上手绘地块、摆放双方兵团，随时**一键试玩**，也能把剧本**存/读档**。
逻辑收敛在无 Qt 的 `MapEditor`，界面由 `EditorPage` 承载，与战斗页并列在 `GameShell` 里。

## 1. 入口与页面路由

```
StartScreen ──editor_requested(difficulty, seed)──► GameShell._open_editor
                                                       └─ World(..., populate=False)  # 有地形、无兵
                                                          └─ EditorPage
EditorPage ──exit_to_menu──────────────► GameShell._editor_to_menu（销毁编辑器，回开始页）
EditorPage ──playtest_requested(World)──► GameShell._start_playtest
                                            └─ BattlePage(world=克隆, back_label="编辑器")
BattlePage(试玩) ──exit_to_menu──────────► GameShell._playtest_to_editor（销毁战斗，回到编辑器）
```

- 开始界面「地图编辑器」按钮复用同一套难度/种子输入；
- 试玩用的是 `world.clone()`（`World.from_dict(to_dict())` 深拷贝），战斗里的伤亡**不会**改动编辑器里的剧本；
- 试玩期间编辑器页保留在 `QStackedWidget` 中，结束/返回即原样回到编辑器继续调整。

## 2. `MapEditor`（`rts/editor.py`，无 Qt）

持有一个 `World` 与当前工具状态，把「在某世界坐标点操作」翻译成对地图/兵团的改动。

| 成员 | 含义 |
| --- | --- |
| `world` | 被编辑的世界 |
| `tool` | `"terrain"` / `"unit"` / `"erase"`（常量 `TOOL_TERRAIN/TOOL_UNIT/TOOL_ERASE`） |
| `terrain` | 当前画笔地形（`Terrain`） |
| `faction` | 摆兵阵营（`Faction`） |
| `unit_type` | 摆兵兵种（`UNIT_TYPES` 键） |
| `brush` | 笔刷半径（格），取自 `BRUSH_RADII=(0,1,2)` → 1×1 / 3×3 / 5×5 |

| 方法 | 作用 |
| --- | --- |
| `on_press(wx,wy) -> bool` | 左键按下：按工具画/放/擦；返回**是否改了地形** |
| `on_drag(wx,wy) -> bool` | 左键拖动：仅地形/擦除连续生效；摆兵不跟随拖动 |
| `on_secondary(wx,wy) -> bool` | 右键：无论何工具都擦除光标下兵团 |
| `paint(wx,wy) -> bool` | 以 `brush` 为半径批量 `GameMap.set_terrain`；有变化则刷新势力场 |
| `place(wx,wy) -> bool` | 可通行处 `world.add_unit`；命中水面则忽略 |
| `erase(wx,wy) -> bool` | `world.unit_at` 命中则 `world.remove_unit` |
| `fill(terrain)` | 整图铺同一地形（`GameMap.fill`） |
| `clear_units()` | 清空所有兵团 |
| `regenerate(seed=None) -> int` | 按新种子重建自然地形（保留兵团），返回实际种子 |
| `counts() -> (p,e)` | 我军 / 敌军团数 |
| `cursor_radius()` | 光标 footprint 半径（地形笔刷用 `brush`，其余单格） |

**返回 bool 的语义**：只有地形改变才需要重建 `MapView` 的地形 `QPixmap` 缓存，
视图据此决定 `invalidate_terrain()` 还是普通 `update()`，避免每次摆兵都重刷整图。

## 3. 视图层改动（`MapView`）

`MapView` 增加编辑态开关，不影响战斗态交互：

| 成员 / 信号 | 说明 |
| --- | --- |
| `edit_mode` | True 时鼠标走编辑分支 |
| `editor` | 注入的 `MapEditor` |
| `edited`（Signal） | 每次编辑动作后发出，供面板刷新计数 |
| `_painting` | 左键拖动画/擦状态 |
| `_edit_hover` | 光标世界坐标，用于画笔预览 |
| `_edit_pan` / `_edit_pan_moved` | 右键平移状态：拖动过则松手不擦除 |

- **左键** 按 `editor.tool` 画地形 / 放兵 / 擦除，可拖动连续作用（地形、擦除）；
- **右键轻点** 擦除光标下兵团；**右键拖拽** 平移视角（在 `mouseRelease` 里按是否拖动过区分：
  未拖动=擦除，拖动过=只平移）；**中键 / 方向键 / 滚轮** 平移缩放照旧；
- `_draw_edit_cursor` 在光标处画预览：地形/擦除画笔刷 footprint 方块，摆兵画半透明幽灵兵团；
- `_edit_hover_text` 状态栏显示当前工具 + 该格地形与速/防倍率；
- 小地图点击仍可快速跳转视角。

## 4. `EditorPage`（`rts/ui.py`）

左侧 `MapView`（编辑态），右侧固定宽度控制面板（套 `QScrollArea` 防截断），底部状态栏。

| 面板分区 | 控件 |
| --- | --- |
| 信息 | 尺寸、双方团数、种子、难度 |
| 工具 | 地形 / 兵团 / 擦除（互斥） |
| 地形 | 六种地形按钮，带颜色色块图标（选地形即切到地形笔刷） |
| 笔刷大小 | 1×1 / 3×3 / 5×5（互斥） |
| 兵团 | 阵营 我军/敌军（选中态蓝/红）+ 四兵种（选兵种即切到摆放） |
| 地图 / 剧本 | 清空为平原、随机地形、清空兵团、势力层开关、保存剧本、载入剧本 |
| 底部 | **▶ 试玩这张地图**、返回主菜单 |

- **保存 / 载入**：`QFileDialog` 选路径，落到无 Qt 的 `world.save_world` / `world.load_world`（JSON）；
- **载入**用 `_swap_world` 把新世界替换进 `MapEditor` / `MapView` 并重置相机与地形缓存；
- **试玩**先校验双方各≥1 团（否则 `winner()` 会瞬间判负/胜），再 `emit(world.clone())`。

## 5. 逻辑层新增（`terrain` / `world`）

| 位置 | 新增 | 作用 |
| --- | --- | --- |
| `terrain.NOMINAL_ELEVATION` | 每地形一个代表高程 | 手绘后 `shade_at` 明暗与地形一致 |
| `GameMap.set_terrain(tx,ty,t)` | 改一格并同步高程 | 越界/未变返回 False |
| `GameMap.fill(t)` | 整图铺地形 | 「清空」用 |
| `World(..., populate=False)` | 建空世界（有地形无兵） | 编辑器起点 |
| `World.remove_unit(u)` | 移除兵团 | 擦除用 |
| `World.to_dict()/from_dict()` | 剧本快照 ↔ 世界 | 存读档、克隆 |
| `World.clone()` | 深拷贝初始状态世界 | 试玩隔离 |
| `world.save_world/load_world(path)` | JSON 存读（无 Qt） | UI 经 `QFileDialog` 调用 |

## 6. 剧本文件格式（JSON，`SCENARIO_VERSION=1`）

```json
{
  "version": 1, "seed": 7, "difficulty": 0,
  "width": 160, "height": 120,
  "tiles": [/* width*height 个地形枚举 int，行优先 */],
  "elevation": [/* width*height 个高程 float，保留 3 位 */],
  "units": [{"type":"infantry","faction":0,"x":..,"y":..,"hp_scale":1.0,"dmg_mult":1.0}, ...]
}
```

`from_dict` 用 `populate=False` 建世界后覆写 `tiles`/`elevation` 并逐个 `add_unit`（满血、会吸附到可通行格）。

## 7. 边界与约定

- 摆兵命中水面被忽略（画笔可把地形改成陆地后再放）；
- 试玩克隆时 `add_unit` 会把落在水面的兵团吸附到最近可通行格；
- 势力层在编辑器默认关闭（先看地形与兵摆位），可用面板按钮或 **T** 打开；
- 逻辑层（`editor` / `world` / `terrain`）不 import Qt，`MapEditor` 与序列化均可单独测。
