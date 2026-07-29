# 09 · API 速查表

## terrain.py

```
TILE = 16
Terrain: PLAIN FOREST HILL MOUNTAIN RIVER LAKE
TERRAIN_INFO[Terrain] -> TerrainInfo
NOMINAL_ELEVATION[Terrain] -> float        # 编辑器手绘时的代表高程

fbm(w, h, seed, octaves=5, base_freq=3, persistence=0.5) -> list[list[float]]

GameMap(width=160, height=120, seed=None)
  .width .height .seed .tiles .elevation
  .pixel_width .pixel_height
  .tile_at(wx,wy) .terrain_at .info_at .passable
  .nearest_passable(wx,wy, max_ring=40)
  .shade_at(tx,ty) .tile_cost(tx,ty)
  .set_terrain(tx,ty,terrain, update_shade=True) -> bool    # 编辑：改一格
  .fill(terrain)                                            # 编辑：整图铺
  .line_clear(x0,y0,x1,y1) -> bool
  .find_path(x0,y0,x1,y1, max_expand=30000) -> list[(wx,wy)]
  .smooth_path(origin, pts) -> list[(wx,wy)]
```

## units.py

```
Faction: PLAYER ENEMY
FACTION_COLOR FACTION_NAME
ENTRENCH_TIME=4.0  ENTRENCH_MAX=0.35
UNIT_TYPES: infantry cavalry archer artillery

UnitType(name, max_hp, speed, attack, attack_range, radius, vision, shape, letter)
Unit(uid, type_key, faction, x, y, ...)
  .spec .max_hp .alive .hp_ratio .role .entrench .moving
  .track_stillness(dt) .distance_to(other)
  .set_path(points, map) .advance(dt, map)

resample_path(points, spacing=12) -> points
build_route(start, points, map) -> points
```

## ai.py

```
Difficulty: EASY HARD
DiffParams(...)  DIFFICULTIES[Difficulty]

Commander(faction, params)
  .update(world, dt)
```

## influence.py

```
INF_STEP=3  INF_RADIUS=150.0
InfluenceField(gw, gh, step_px, owner, player_cells, enemy_cells)
  .total_controlled .share() .owner_at(gx,gy)
compute_field(world) -> InfluenceField
```

## world.py

```
INFLUENCE_INTERVAL=0.4   SCENARIO_VERSION=1

World(map_width=160, map_height=120, seed=None, difficulty=EASY, populate=True)
  .map .difficulty .params .units .elapsed .events .commander .field
  .add_unit(type_key, faction, x, y, hp_scale=1, dmg_mult=1)
  .remove_unit(unit) -> bool
  .units_of(f) .unit_by_uid .unit_at .units_in_rect .selected
  .clear_selection() .issue_path(units, points) .issue_move(units, wx, wy)
  .stop(units) .update(dt) .refresh_field() .log(text) .winner()
  .to_dict() -> dict   World.from_dict(dict) -> World   .clone() -> World

save_world(world, path)   load_world(path) -> World      # JSON，无 Qt
```

## editor.py

```
TOOL_TERRAIN TOOL_UNIT TOOL_ERASE      BRUSH_RADII=(0,1,2)

MapEditor(world)
  .world .tool .terrain .faction .unit_type .brush
  .on_press(wx,wy)->bool  .on_drag(wx,wy)->bool  .on_secondary(wx,wy)->bool
  .paint(wx,wy)->bool  .place(wx,wy)->bool  .erase(wx,wy)->bool
  .fill(terrain)  .clear_units()  .regenerate(seed=None)->int
  .counts()->(p,e)  .cursor_radius()->int
```

## mapview.py

```
MIN_ZOOM=0.35  MAX_ZOOM=3.0

MapView(world)
  signals: selection_changed, hover_changed(str), edited
  .zoom .cam_x .cam_y .hovered
  .edit_mode .editor            # 编辑态：注入 MapEditor
  .to_world .to_screen .clamp_camera .center_on .center_on_selection
  .invalidate_terrain .toggle_grid .toggle_territory
```

## ui.py

```
TICK_MS=33     UNIT_GLYPH[type_key] -> str
StrengthBar / SidePanel / BattlePage / EditorPage / StartScreen / GameShell
BattlePage(difficulty, seed, world=None, back_label="主菜单")
StartScreen signals: start_requested, editor_requested
DARK_QSS
run() -> int   # app.exec()
```

## 入口

```
main.py          -> rts.ui.run
smoke_gui.py     -> offscreen 全流程冒烟
rts.__version__  -> "0.1.0"
```
