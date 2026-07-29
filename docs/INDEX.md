# 笔锋 · 代码文档索引

> 项目：`笔锋` — PySide6 实时战略原型  
> 版本：`0.1.0`（见 `rts/__init__.py`）  
> 入口：`python main.py`

本文档按模块分门别类，便于日后查阅实现细节、数值与调用关系。

---

## 快速导航

| 文档 | 内容 |
| --- | --- |
| [00-overview](00-overview.md) | 项目总览、目录结构、依赖、运行与测试 |
| [01-architecture](01-architecture.md) | 分层架构、主循环、数据流、模块依赖图 |
| [02-terrain](02-terrain.md) | 地形类型、地图生成、通行判定、A* 寻路 |
| [03-units](03-units.md) | 阵营、兵种、Unit 状态、据守、画笔路线 |
| [04-world](04-world.md) | World 模拟：生成、指令、索敌、战斗、碰撞 |
| [05-ai](05-ai.md) | 难度参数、Commander 决策逻辑 |
| [06-influence](06-influence.md) | 势力场计算、领土归属、统计 |
| [07-mapview](07-mapview.md) | 相机、渲染层、鼠标键盘交互、小地图 |
| [08-ui](08-ui.md) | 开始界面、战斗页、侧栏、外壳与样式 |
| [09-api-cheatsheet](09-api-cheatsheet.md) | 类 / 函数 / 常量速查表 |
| [10-constants](10-constants.md) | 全部数值常量与公式汇总 |
| [11-editor](11-editor.md) | 地图编辑器：地形手绘、摆兵、存读档、试玩 |

---

## 源码文件 ↔ 文档对照

```
main.py                 → 00-overview
smoke_gui.py            → 00-overview（冒烟测试）
rts/__init__.py         → 00-overview
rts/terrain.py          → 02-terrain
rts/units.py            → 03-units
rts/world.py            → 04-world
rts/spatial_hash.py     → 04-world（空间哈希近邻查询）
rts/ai.py               → 05-ai
rts/influence.py        → 06-influence
rts/mapview.py          → 07-mapview
rts/ui.py               → 08-ui
rts/editor.py           → 11-editor
```

---

## 设计关键词（全局概念）

| 概念 | 一句话 | 主要实现 |
| --- | --- | --- |
| **画笔行军** | 右键拖出轨迹 → 重采样/绕障 → 平移到各兵脚下 | `units.build_route` / `World.issue_path` |
| **僵持战斗** | 高血低伤 + 地形防 + 据守，正面难啃 | `World._resolve_combat` / `Unit.entrench` |
| **据守** | 静止约 4 秒满级，防御最高 +35% | `Unit.track_stillness` / `entrench` |
| **势力图层** | 影响力硬划分领土，无中立带，描前线 | `influence.compute_field` / `MapView._draw_territory` |
| **指挥官 AI** | 按难度参数统一指挥敌军 | `ai.Commander` |
| **空间哈希** | 索敌/碰撞/视野只翻附近桶，O(n²)→O(n·k) | `spatial_hash.SpatialHash` / `World._ensure_grid` |
| **势力脏检查** | 战场签名没变就跳过势力场重算 | `World._field_signature` / `refresh_field` |
| **地图编辑器** | 手绘地形、摆双方兵团、存读档、一键试玩 | `editor.MapEditor` / `ui.EditorPage` |
