# 07 · 地图视图（`rts/mapview.py`）

Qt 控件：渲染 + 输入。逻辑指令转发到 `World`。

## 1. 常量

| 名 | 值 |
| --- | --- |
| `MIN_ZOOM` | 0.35 |
| `MAX_ZOOM` | 3.0 |

## 2. `MapView` 状态

| 成员 | 说明 |
| --- | --- |
| `world` | 当前 World（换战场时替换） |
| `zoom` | 缩放 |
| `cam_x, cam_y` | 视口左上角世界坐标 |
| `_terrain_cache` | 地形 QPixmap |
| `_dragging_cam` | 中键平移中 |
| `_select_origin/current` | 框选世界坐标 |
| `_brush` / `_drawing` | 右键画笔点列 |
| `order_mode` | 右键下达的指令类型：`move`/`attack_move`/`patrol`（工具栏按钮或 M/A/P 切换） |
| `formation_lock` | 编队锁定开关：下令时是否同步限速保队形（Q 切换） |
| `_show_grid` / `_show_territory` / `_show_fog` | 图层开关（势力、战雾默认开） |
| `hovered` | 悬停单位 |
| `_attack_pings` | 指定攻击下令确认红圈：`(x,y,t_left)` 列表 |
| `_attack_cursor` | 是否正显示攻击十字光标 |

### 信号

| 信号 | 触发 |
| --- | --- |
| `selection_changed` | 选中变化 |
| `hover_changed(str)` | 状态栏文案 |

## 3. 坐标

```
to_world(p)  = (cam_x + px/zoom, cam_y + py/zoom)
to_screen(wx,wy) = ((wx-cam_x)*zoom, (wy-cam_y)*zoom)
```

`clamp_camera`：相机夹在地图范围内（地图比视口小时允许负侧对齐）。

`center_on` / `center_on_selection`：选中中心或全体我军中心。

## 4. 地形缓存

- 按格写 `QImage`：`color * shade_at`；
- `scaled` 到像素尺寸 + SmoothTransformation；
- `invalidate_terrain()` 换图时清空。

## 5. 绘制顺序 `paintEvent`

1. 背景填充；
2. 地形 pixmap（源矩形 = 相机视口）；
3. 势力 `_draw_territory`（可选）；
4. 网格 `_draw_grid`（可选，步长 `TILE*4`）；
4b. 战争迷雾 `_draw_fog`（默认开，编辑模式恒关）：1/4 分辨率离屏图铺雾色，
    DestinationOut 擦出视野圆再平滑放大贴回（省去每帧 QPainterPath 布尔减法）；
    迷雾开启时视野外敌军（含其交战特效、小地图点、悬停）一律不画，
    可见性判定每帧缓存于 `_vis_cache`（uid→bool，paintEvent 进帧失效）；
5. 我军路径（选中或悬停）；
6. 射程圈 `_draw_attack_ranges`（选中的己方兵团）：贴图按 (兵种, 量化到 0.05 的缩放)
   缓存于 `_range_cache`（上限 24 张），每单位一次 drawPixmap；
7. 交战表现 `_draw_combat`（远程曳光/近战火花）；
7b. 敌情记忆残影 `_draw_memory_ghosts`（迷雾外曾见敌军的淡红虚影，按年龄淡出）；
8. 全部单位 `_draw_unit`；
8b. 指定攻击下令红圈 `_draw_attack_pings`（约 0.38s 脉动）；
9. 画笔预览 `_draw_brush`；
10. 框选矩形；
11. 小地图 `_draw_minimap`（含敌情记忆淡红点）。

### 画笔预览 `_draw_brush`（含障碍高亮）

笔迹本体画黄色光晕 + 亮线；在此之前先做障碍高亮，让玩家下笔时就能看到受阻处：
- 相邻点 `line_clear=False` 的段画**橙色虚线**（该段会被 A* 自动绕行）；
- 笔迹穿过的不可通行格（湖泊）画**红色半透明方块**。

### 阵营样式 `FACTION_STYLE`

每阵营一组色板：`hi/base/lo`（兵牌主体三段渐变，左上受光）、`edge`（深色描边）、`trace`（交战轨迹色）。

### 交战表现 `_draw_combat`

对每个「有目标且目标在射程内」的单位：

| 类型 | 表现 |
| --- | --- |
| ranged | 暗淡全弹道 + 循环飞行的亮色曳光段（按 uid 错相位） |
| melee | 接触中点画脉动径向光斑 + 旋转十字火花 |

### 单位绘制要点

| 元素 | 规则 |
| --- | --- |
| 落地阴影 | 同形黑色半透明，向右下偏移，棋子感 |
| 交战警示环 | 射程内开火时外圈红橙脉动光环 |
| 选中 | 金色双环（宽淡光 + 细亮线）同形描边 |
| 据守光环 | `still_time > 0.4*ENTRENCH_TIME` 且非行军，青绿虚线圈，越久越亮 |
| 主体 | 阵营色三段线性渐变（hi→base→lo）+ 深色描边 + 内圈细高光 |
| 受击 | `flash>0` 渐变三色整体 lighter |
| 朝向 | 非三角画白色楔形箭头（替代细线） |
| 托管标识 | `u.auto` 时右上角青色小圆点 |
| 角标 | zoom>0.7 画 letter，黑色描影 + 白字，CJK 字体族 |
| 血条 | ratio<1 时，暗底边框 + 纵向渐变 + 1/4 刻度线，绿/黄/红阈值 0.5/0.25 |

### 小地图

- 右下约 172px 边长等比，带外框；
- 地形缩略 + **敌情记忆淡红点**（`World.player_memory_ghosts`，alpha 随年龄衰减）+
  交战点闪烁火光 + 双方可见点（阵营 base 色，选中用 hi 色）+ 视口白框；
- `_minimap_rect` 供点击跳转。

## 6. 输入

### 鼠标

| 操作 | 行为 |
| --- | --- |
| 左键点小地图 | `center_on` 该世界点 |
| 左键点我军 | 选中；Shift/Ctrl 加选/切换 |
| 左键空地 | 开始框选；无修饰则先清空 |
| 左键释放且拖够 | 矩形内我军 selected |
| 右键按下（有选中） | 开始画笔 |
| 右键移动 | 间距 > `4/zoom` 加点 |
| 右键释放 | 单击且点在可见敌军 → `issue_attack`（分槽站位）+ 红圈 ping + 状态栏「攻击…」或「N 团集火…」；否则按 `order_mode` 分发；**Shift** 时 `queue=True` 追加队列（琥珀虚线预览） |
| 悬停可见敌军（有选中） | 光标 `CrossCursor`；克制/下一轮伤害区间/TTK；多选时「N团集火」 |
| 中键拖 | 平移相机 |
| 滚轮 | 以光标为锚缩放 |

### 键盘（MapView 内）

| 键 | 行为 |
| --- | --- |
| 方向键 / WASD | 平移（`60/zoom`） |
| Esc | 清空选择 |

全局快捷键在 `BattlePage._build_shortcuts`（空格、Ctrl+A、H、T、G、F5、Tab、Esc 回菜单）。

## 7. 悬停文案 `_hover_text`

地形格坐标 + 速度/防御/攻击倍率 + desc；若悬停单位附加阵营、兵种、HP、据守/行军标签。

## 8. 与 BattlePage 协作

- `hover_changed` → 底栏 status；
- `selection_changed` → `panel.refresh`；
- 工具栏按钮直接调 `toggle_territory` / `toggle_grid` / `center_on_selection`。


## 渲染缓存一览（性能）

| 缓存 | 键/失效 | 收益 |
| --- | --- | --- |
| `_terrain_cache` | `invalidate_terrain()` | 地形只光栅化一次 |
| `_minimap_cache` | 尺寸变化 / invalidate_terrain | 免每帧全图重采样到小地图 |
| `_territory_cache` + `_frontline` | `world.field_version` 变化 | 势力层从 ~4300 次 drawRect/帧 → 1 次 drawPixmap；前线段预提取并视口裁剪 |
| `_vis_cache` | 每帧（paintEvent 进帧清空） | 迷雾可见性每单位每帧只算一次 |
| `_fog_img` | 尺寸变化（内容每帧重画，1/4 分辨率） | 迷雾大圆填充成本降 ~16 倍 |
| `_range_cache` | (兵种, 缩放量化 0.05)，>24 清空 | 渐变射程圈免每帧重建 |

配套：`World.field_version`（refresh_field 时 +1）与 `GameMap.terrain_version` 供缓存失效判断；
`influence.compute_field` 面积统计直接查 tiles + `_PASSABLE` 表，绕开热循环里的枚举构造。