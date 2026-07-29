# 00 · 项目总览

## 1. 是什么

**笔锋** 是一个平面地图上的小型即时战略（RTS）原型，UI 基于 **PySide6**。

核心玩法：

1. 选中己方兵团；
2. **右键拖拽**在地图上画一条行军路线；
3. 兵团沿笔迹推进，遇湖泊自动 A* 绕行；
4. 战斗偏「僵持」：血厚、伤害低，据守 + 地形防御让正面硬刚极不划算，需集火与包抄。

## 2. 目录结构

```
rts/                          # 工作区根
├── main.py                   # 入口：调用 rts.ui.run()
├── smoke_gui.py              # 离屏冒烟测试
├── README.md                 # 玩家向说明
├── docs/                     # ← 本套代码文档
│   ├── INDEX.md
│   └── ...
└── rts/                      # 游戏包
    ├── __init__.py           # __version__ = "0.1.0"
    ├── terrain.py            # 地形 / 地图生成 / A*（无 Qt 依赖）
    ├── units.py              # 阵营、兵种、Unit、画笔路线
    ├── ai.py                 # 难度 + Commander
    ├── influence.py          # 势力场
    ├── editor.py             # 地图编辑器控制器（无 Qt）
    ├── world.py              # 世界状态与模拟主循环
    ├── mapview.py            # 地图渲染与交互（Qt）
    └── ui.py                 # 开始界面 / 战斗页 / 编辑器页 / 外壳（Qt）
```

## 3. 依赖

| 依赖 | 用途 |
| --- | --- |
| Python 3.10+（建议，代码使用 `list[Unit]`、`X \| Y` 等语法） | 运行时 |
| **PySide6** | GUI、事件、绘制 |

逻辑层（`terrain` / `units` / `ai` / `influence` / `world`）**不依赖 Qt**，可单独测。

安装：

```bash
pip install PySide6
```

## 4. 运行

```bash
python main.py
```

流程：

1. 开始界面选难度（简单 / 困难）、可选地图种子；
2. 「开始战斗」进入 `BattlePage`；
3. 约 30 FPS 的 `QTimer` 驱动 `World.update`；
4. Esc / 战斗结束对话框可回主菜单。

## 5. 冒烟测试

```bash
python smoke_gui.py
```

- 强制 `QT_QPA_PLATFORM=offscreen`，无窗口；
- 走通：选困难 → 开战 → 框选 → 画笔路径 → 模拟 120 帧 → 渲染 → 各类快捷操作 → 回菜单再开简单局；
- 成功打印 `ALL OK`，并写出 `smoke_frame.png`。

断言覆盖：

- 难度与种子传入；
- 框选只含我军；
- 画笔路径非空且全部可通行；
- 返回主菜单、再开简单局。

## 6. 模块职责一览

| 模块 | 职责 | Qt？ |
| --- | --- | --- |
| `terrain` | 地形枚举、FBM 地图、河流/湖泊、通行、A* | 否 |
| `units` | Faction / UnitType / Unit、据守、路径工具 | 否 |
| `ai` | Difficulty、DiffParams、Commander | 否 |
| `influence` | 影响力网格、owner、控制面积 | 否 |
| `editor` | 地图编辑器控制器：地形/兵团/存读档 | 否 |
| `world` | 持有 map+units，移动/索敌/战斗/AI/势力刷新 | 否 |
| `mapview` | 相机、地形缓存、单位形状、势力/小地图、输入、编辑态 | 是 |
| `ui` | StartScreen、BattlePage、EditorPage、SidePanel、GameShell | 是 |

## 7. 坐标与单位约定

| 概念 | 约定 |
| --- | --- |
| 世界坐标 | 像素，原点在地图左上，x 向右、y 向下 |
| 地形格 | `TILE = 16` 像素 / 格 |
| 地图默认 | 160×120 格 → 2560×1920 像素 |
| 模拟步长 | `TICK_MS = 33` ≈ 30 FPS；`dt = 0.033s` |
| 速度 | 像素 / 秒（平原基准） |
| 伤害 | 每秒持续伤害（DPS × dt） |

## 8. 与 README 的关系

- `README.md`：面向玩家的操作与数值简介；
- `docs/`：面向开发者的实现级文档，查函数、公式、调用链时用本套文档。
