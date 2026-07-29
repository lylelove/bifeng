# 02 · 地形与地图（`rts/terrain.py`）

**依赖：** 仅标准库。无 Qt。

## 1. 常量

| 名 | 值 | 含义 |
| --- | --- | --- |
| `TILE` | `16` | 一格世界像素边长 |

## 2. `Terrain`（IntEnum）

| 枚举 | 值 | 中文 |
| --- | --- | --- |
| `PLAIN` | 0 | 平原 |
| `FOREST` | 1 | 森林 |
| `HILL` | 2 | 丘陵 |
| `MOUNTAIN` | 3 | 山地 |
| `RIVER` | 4 | 河流 |
| `LAKE` | 5 | 湖泊 |

## 3. `TerrainInfo`（frozen dataclass）

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `name` | str | 显示名 |
| `color` | (r,g,b) | 渲染底色 |
| `speed` | float | 移动速度倍率 |
| `defense` | float | 受伤减免倍率（**越大越耐打**） |
| `attack` | float | 在此地形输出倍率 |
| `vision` | float | 视野倍率（影响索敌 reach） |
| `passable` | bool | 是否可走（湖泊 False） |
| `desc` | str | 图例说明 |

### 3.1 数值表（`TERRAIN_INFO`）

| 地形 | 色 | 速 | 防 | 攻 | 视 | 通行 |
| --- | --- | --- | --- | --- | --- | --- |
| 平原 | (126,166,84) | 1.00 | 1.00 | 1.00 | 1.00 | ✓ |
| 森林 | (58,105,62) | 0.65 | **1.30** | 0.95 | 0.75 | ✓ |
| 丘陵 | (150,137,92) | 0.80 | 1.20 | 1.15 | 1.25 | ✓ |
| 山地 | (122,112,104) | 0.45 | **1.55** | 1.25 | 1.40 | ✓ |
| 河流 | (86,148,190) | 0.35 | **0.60** | 0.60 | 1.00 | ✓ |
| 湖泊 | (46,96,152) | 0.00 | 1.00 | 1.00 | 1.00 | ✗ |

设计意图：山地/森林利于据守；河流「半渡而击」极脆。

## 4. 噪声生成

### 4.1 `_smoothstep(t)`

Hermite 平滑：`t²(3-2t)`，用于双线性插值权重。

### 4.2 `_value_noise(width, height, freq, rng)`

- 在 `(freq+1)²` 随机格点上取值；
- 双线性 + smoothstep 插值到 `width×height`。

### 4.3 `fbm(..., octaves=5, base_freq=3, persistence=0.5)`

分形布朗运动：多层 value noise 叠加后归一化到 **[0, 1]**。

- 高程：`seed`，5 倍频；
- 湿度：`seed ^ 0x5bf03635`，4 倍频，`base_freq=4`。

## 5. `GameMap`

### 5.1 构造

```python
GameMap(width=160, height=120, seed=None)
```

- `seed is None` → `random.randrange(1<<30)`；
- 流程：
  1. `fbm` 高程 + 湿度；
  2. 按阈值分类地形；
  3. `_carve_rivers`；
  4. `_cleanup_puddles`。

### 5.2 分类规则（格）

| 条件 | 结果 |
| --- | --- |
| `elevation < 0.20` | 湖泊 |
| `elevation > 0.76` | 山地 |
| `elevation > 0.63` | 丘陵 |
| `moisture > 0.60`（否则） | 森林 |
| 其余 | 平原 |

### 5.3 河流 `_carve_rivers`

- 源：`elevation > 0.70` 的内部格，shuffle 后取 `count = max(3, width//45)` 条；
- 每步走向 8 邻中「高程 + 噪声」最低处；
- 写入 `RIVER`，约 35% 概率拓宽邻格；
- 遇地图外 / 湖泊结束；洼地无更低邻 → `_fill_lake` 半径 2–4。

### 5.4 清坑 `_cleanup_puddles`

孤立湖泊（8 邻水格 ≤ 1）改回平原。

## 6. 查询 API

| 方法 / 属性 | 返回 | 说明 |
| --- | --- | --- |
| `pixel_width` / `pixel_height` | int | `width*TILE` 等 |
| `tile_at(wx, wy)` | `(tx, ty)` | 世界坐标 → 格索引，夹紧边界 |
| `terrain_at(wx, wy)` | `Terrain` | 当前地形枚举 |
| `info_at(wx, wy)` | `TerrainInfo` | 当前地形信息 |
| `passable(wx, wy)` | bool | 界外或不可通行 → False |
| `nearest_passable(wx, wy, max_ring=40)` | `(wx, wy)` | 环扫吸附到可通行格中心 |
| `shade_at(tx, ty)` | float | `0.82 + 0.36*elevation`，渲染明暗 |
| `tile_cost(tx, ty)` | float | `1/speed`，不可通 → `inf` |

## 7. 寻路

### 7.1 `line_clear(x0,y0,x1,y1)`

沿线段按 `TILE*0.5` 步进采样，全部 `passable` 才 True。  
用途：画笔直达段、路径漏斗简化。

### 7.2 `find_path(x0,y0,x1,y1, max_expand=30000)`

- 起终点先 `nearest_passable` 再 `tile_at`；
- **A\*** 8 邻，代价 = `tile_cost`，斜向 ×√2；
- **禁止贴障斜穿**：斜移时正交两侧任一不可通则跳过；
- 启发：对角距离  
  `(dx+dy) + (√2-2)*min(dx,dy)`；
- 超 `max_expand` 或堆空 → `[]`；
- 回溯后 `smooth_path`。

返回：**不含起点** 的世界坐标航点列表（格中心）。

### 7.3 `smooth_path(origin, pts)`

漏斗：从当前点尽量跳到最远 `line_clear` 可达点，去掉网格锯齿。

## 8. 调用方

| 调用方 | 用途 |
| --- | --- |
| `units.build_route` / `Unit.advance` | 通行、速度、绕障 |
| `world` 生成与战斗 | 地形攻防、spawn 吸附 |
| `ai._defensive_spot` | 找高防格 |
| `influence` | 统计时跳过不可通行 |
| `mapview` | 颜色、缓存、网格 |
| `ui.SidePanel` | 图例 |
