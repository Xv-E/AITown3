# 内容包（Content Packs）

引擎（Python）只提供通用机制；**具体的行为和物品全部在这里用 JSON 定义**。
改内容不需要动引擎代码，重启服务 / 重建世界即可生效。

```
content/
  <pack>/
    pack.json            包说明
    actions/*.json       动作（移动、睡觉、吃东西……）
    states/*.json        持续状态（睡着、昏迷……）
    archetypes/*.json    物品原型（篝火、炉子、木柴……）
    planning.json        PDDL 自动编译的手动剪枝（见第 5 节）
```

默认加载 `base`，再按名字顺序加载 `content/` 下其它带 `pack.json` 的包（如 `streets`、`village`）。

- `village`：边境村落的建设（工地、作坊、面包房……）
- `streets`：街头经济 — `cash` 付给摊档时进摊主口袋（不销毁），摊档卖完向码头 `wholesale` 进 `supplies`，
  `rent_book` 记房租，`collect_dues` 收保护费（结算在 `agent/economy.py`）
环境变量 `AITOWN_CONTENT_PACKS`（逗号分隔）可以显式指定加载哪些包。
**后加载的包覆盖同名定义**，所以想改 `base` 里的某个动作，新建一个包写同名文件即可：

```
AITOWN_CONTENT_PACKS=base,my_mod
```

---

## 1. 引擎与内容的边界

| 引擎负责 | 内容负责 |
|---|---|
| 时间 / routine / trigger / LLM 决策 / 规划器 | 有哪些动作、前置条件、效果 |
| 动作执行管线（check → apply → 时长） | 有哪些状态、什么时候醒 |
| 通用物品交互 `use_on` / `interact` | 物品原型、合成、tick 脚本、警报 |
| 能力（capability）槽位 | 哪个动作填哪个能力槽 |

### 能力（capability）

规划器不认识 "move"、"pick_up" 这些名字，它只按**能力**找动作。
内容动作声明 `"capability": "travel"` 就会被规划器用于寻路。参数必须按下表顺序声明：

| capability | 参数类型 | base 包中的动作 |
|---|---|---|
| `travel`   | Person, Location(from), Location(to) | `move` |
| `take`     | Person, Item, Location | `pick_up` |
| `drop`     | Person, Item, Location | `drop` |
| `give`     | Person, Item, Person, Location | `give` |
| `converse` | Person, Person, Location | `talk` |
| `attack`   | Person, Person, Location | `attack` |
| `consume`  | Person, Item | `use_item` |
| `idle`     | Person | `wait` |

不声明 capability 的动作也完全可用：玩家菜单、LLM 和 routine（`"action": "<name>"`）都能直接调用它。

---

## 2. 动作 `actions/<name>.json`

```json
{
  "name": "sleep",
  "description": "Lie down and sleep until the routine says it is time to get up",
  "duration": 1.0,
  "capability": null,
  "params": [
    { "name": "c", "type": "Person", "source": "self" }
  ],
  "requires": [
    { "op": "prop_gte", "entity": "@c", "label": "Person", "prop": "hp", "value": 1, "message": "@c is dead" }
  ],
  "effects": [],
  "enter_state": "asleep",
  "pddl_preconditions": [],
  "pddl_effects": []
}
```

| 字段 | 说明 |
|---|---|
| `params` | 第一个必须是 `Person`（执行者，也可用 `@actor` 引用） |
| `params[].source` | 玩家菜单如何列出候选：`self` / `here`（当前位置）/ `adjacent`（相邻位置）/ `held`（手上物品）/ `ground`（地上物品）/ `people_here`（同处的人） |
| `requires` | 条件列表，全部成立才能执行；失败时返回第一个失败条件的 `message` |
| `effects` | 操作列表，按顺序执行 |
| `duration` | 游戏内耗时（分钟单位与引擎时钟一致） |
| `enter_state` | 执行后让执行者进入该状态（只能用于单参数动作） |
| `narrative` | 可选，执行成功时的叙述文本，支持 `@` 占位符 |
| `pddl_*` | 可选，只给旧的寻路规划用；新的规划从 `requires` / `effects` 自动编译（见第 5 节） |

### 占位符

- `@c`：参数 `c` 的值
- `@actor`：第一个参数
- `@i.value`：实体 `@i` 的属性 `value`（在 `delta`、`value`、`message` 中均可用）

### 条件（`requires`、`when`）

所有条件都可以带 `"message"`（支持占位符）作为失败提示。`label` 默认 `Item`。

| op | 字段 | 含义 |
|---|---|---|
| `prop_eq` / `prop_neq` | entity, prop, value, [label], [strict] | 属性等于 / 不等于（`strict`：未设置不视为 false） |
| `prop_gte` / `prop_lte` | entity, prop, value, [label] | 数值 ≥ / ≤ |
| `prop_in` | entity, prop, values, [prefix] | 属性在列表中（`prop: "name"` 匹配名字；`prefix` 按前缀） |
| `has_item` | person, item | person 持有 item |
| `not_held` | item | 没被任何人拿着 |
| `person_at` / `item_at` | person/entity, location | 在某地 |
| `has_rel` / `not_has_rel` | a, b, rel, [a_label], [b_label], [directed] | 存在 / 不存在关系（默认 `ADJACENT_TO`） |
| `neq` | a, b | 两个值不同（如不能攻击自己） |
| `any` | of: [条件...] | 任一成立 |

### 操作（`effects`、`effects_on[].ops`、`on_exit`）

任何操作都可以带 `"when": [条件...]`，条件不全成立就跳过该操作。

| op | 字段 | 含义 |
|---|---|---|
| `set_prop` | entity, prop, value, [label] | 设置属性 |
| `add_prop` | entity, prop, delta, [scale], [min], [max], [label] | 数值加减（`delta` 可为 `@x.prop`） |
| `spawn_item` | name 或 archetype, held_by / located_at, … | 生成物品（重名自动加后缀；原型的 `script`、`effects_on` 一并带上，`script.initial_state` 写成初始属性） |
| `spawn_location` | name, adjacent_to（一个或列表）, [description], [tags], [bidirectional=true] | 新建地点并连到已有地点（已存在则只补连接） |
| `delete_entity` | entity, [label] | 删除实体 |
| `link` / `unlink` | a, b, rel, [a_label], [b_label], [bidirectional] | 建 / 删关系 |
| `relocate` | entity, to, [label] | 移到某地（解除持有） |
| `set_state` | entity, state | 设置人物状态（`null` 清除） |

---

## 3. 状态 `states/<name>.json`

```json
{
  "name": "asleep",
  "label": "Zzz",
  "description": "is asleep",
  "targetable": false,
  "wake_priority": 1.0,
  "ends_on_routine_change": true,
  "on_exit": []
}
```

引擎只知道"状态会挂起决策"，不知道"睡觉"是什么：

- 处于状态中的人**不做决策、不响应普通 trigger**；
- `ends_on_routine_change`：routine 时段切换时结束状态；
- `wake_priority`：优先级 ≥ 该值的 trigger 会打断状态（1.0 = 只有最紧急的事件）；
- `targetable: false`：别人不能对他执行动作（交谈、给东西……），也不会出现在"在场的人"里；
- `on_exit`：结束时执行的操作列表（如恢复体力）；
- `label`：客户端头顶气泡 / 调试面板显示的文字。

Routine 里用 `"action": "sleep"` 让角色在该时段去执行这个动作：

```json
{ "start": "21:00", "end": "06:00", "location": "Cottages", "activity": "sleep", "action": "sleep" }
```

---

## 4. 物品原型 `archetypes/<name>.json`

世界文件里写 `{"name": "Stove", "archetype": "stove", ...}`，原型字段会被深合并到物品下面（物品自己的字段优先）。
原型之间可以用 `"extends": "<另一个原型>"` 继承。

```json
{
  "archetype": "vegetable",
  "item_kind": "ingredient",
  "portable": true,
  "value": 4,
  "effects_on": [
    {
      "target": "kind:stove",
      "consume": true,
      "ops": [ { "op": "spawn_item", "archetype": "meal", "held_by": "@actor" } ]
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| `item_kind` | 类别；`effects_on.target` 用 `kind:<kind>` 匹配该类全部物品 |
| `portable` | `false` = 固定物（炉子、篝火），不能捡，客户端按"设施"显示 |
| `effects_on` | 把此物品 `use_on` 到目标上时发生什么（合成、加燃料……）；`@actor` 为使用者 |
| `accepts_from` | 固定物可接受的物品类别，用于提示 NPC / 玩家 |
| `script.initial_state` | 生成时写入的初始属性（如 `{"need_log": 8}`） |
| `script.on_tick` | 随时间变化（`add_prop` + `delta_per_sec`，`derive_bool`，`ensure_kind`，`rule`） |
| `script.alerts` | 属性达到阈值时发出警报，作为突发 trigger 推给 NPC（`critical` / `urgent`） |
| `script.lore` | 给 LLM 看的说明 |

`spawn_item` 和 `ensure_kind` 的生成规则里也可以写 `"archetype"`。

### 规则 `rule`（让世界"发展"）

`on_tick` 里的 `rule`：条件全部成立时执行一串操作，可选广播一条新闻。

```json
{
  "op": "rule",
  "id": "built",
  "when": [ { "op": "prop_lte", "entity": "@self", "prop": "need_log", "value": 0 } ],
  "do": [
    { "op": "spawn_location", "name": "Workshop", "adjacent_to": "@here" },
    { "op": "spawn_item", "archetype": "workbench", "located_at": "Workshop" },
    { "op": "delete_entity", "entity": "@self" }
  ],
  "announce": "The Workshop is finished!"
}
```

- `@self` = 物品本身，`@here` = 物品所在地点；
- `once`（默认 `true`）：只触发一次；
- `announce`：写进世界大事记（客户端弹出 "News: …"，调试面板 `~` 里有 "Village news"），并作为 `news` trigger 推给所有 NPC。

建设玩法就是这么搭的（见 `content/village/`）：工地有 `need_*` 属性 + 警报（`prefer_kind` 指明要什么材料），
材料原型的 `effects_on` 把 `need_*` 减 1，`rule` 在全部满足后生成新地点、新设施和下一个工地。

### 大地图 / 区域格子 / 蓝图（Elin 式建设）

**区域 = Location**。世界文件里给地点写大地图坐标和格子尺寸：

```json
{ "name": "Village", "tags": "hub,build", "map": [0, 0], "grid": [24, 13] }
```

- `map`：大地图坐标。旅行动作（capability `travel`）的耗时 = `duration` × 两地距离（至少 1）。
- `grid`：区域格子尺寸（默认 20×12，四周一圈留给出口）。
- 物品可写 `"pos": [x, y]`（左上角格子）；不写则自动找空位。固定物按 `footprint: [w, h]` 占格子，可携带物只占一格、互不重叠。

**蓝图**：原型里带 `blueprint` 就能被玩家（B 键）或规则放到格子上：

```json
"footprint": [3, 2],
"blueprint": {
  "label": "Bakery",
  "description": "Bakes wheat into bread.",
  "zone_tags": ["build"],
  "requires": [ { "op": "count", "archetype": "workbench", "gte": 1, "message": "Build a Workshop first" } ],
  "order": 20
}
```

- `zone_tags`：只有带这些 tag 的区域能放；`requires`：条件（`@actor` = 放置者，`@here` = 区域）。
- 引擎只负责放置；怎么建成由工地自己的 `alerts` + `rule` 决定（见 `content/village/`）。
- 建成时在原位生成建筑：`{"op": "spawn_item", "archetype": "oven", "located_at": "@here", "x": "@self.x", "y": "@self.y"}`，再删掉工地。
- `spawn_item` 不写坐标时用 `"near": "@self"` 放在某物附近。

**NPC 自己发起建设**：用一个固定物（如 `village_plan`）的 `rule` + `count` 条件，在条件满足时 `spawn_item` 下一个工地。

条件 `count`：`{"op": "count", "archetype": "oven" 或列表, "kind": "...", "location": "@here", "gte": 1, "lte": 0}` —— 统计物品数量（生成的物品会记下自己的 `archetype`）。

接口：`GET /api/build/blueprints`（当前区域可建列表及不可建原因），`POST /api/build {"archetype", "x", "y"}`。

### Routine 里的物品目标

```json
{ "start": 12, "end": 13, "location": "Field", "goal": "Use item", "item_kind": "food", "craft": true, "once": true }
```

- `item_kind`：要用掉的物品类别；
- `craft: true`：手边没有时允许按配方（`effects_on` 里 `spawn_item` 给 `@actor` 的交互）自己做；不写则只拿现成的（例如顾客只等厨师做好的饭）。

---

## 5. PDDL 自动编译 `planning.json`

动作的 `requires` / `effects` 和物品的 `effects_on` 会被 `planner/compiler.py` 自动翻译成 PDDL，
不需要手写 `pddl_*`（那两个字段只给旧的寻路规划用）。自动翻译之后，再用 `planning.json` 手动剪枝。

### 怎么翻译

| 数据 | PDDL |
|---|---|
| `person_at` / `item_at` / `has_item` / `has_rel` / `link` / `unlink` / `relocate` | 谓词 `located-at` / `has-item` / `adjacent-to` … |
| 会被效果改写的数值属性（`add_prop`，如 `hp`、`need_log`） | 数值函数，用 ENHSP 求解 |
| 会被改写的布尔 / 枚举属性（`set_prop`、`set_state`） | 谓词 `is-<prop>` / `<prop>-is-<value>` |
| 从不被改写的属性条件（`item_kind`、`portable` …） | 静态谓词，事实在 problem 里预先算好 |
| `spawn_item` | 预留的「未出生」对象池；计划里写成 `kind:<类别>`，执行时绑定 |
| `any`、`when` | 拆成多个动作变体（`use_item--v1` …） |
| 每条 `effects_on` | 一个 `use_on--<工具类别>--<目标>` 动作 |

另外两点：
- 每个动作的执行者都限定为正在规划的角色，计划里不会让别人替他做事。
- 编译时会按目标做相关性剪枝：只保留能把目标所需事实往需要方向推动的动作。

翻译不了的部分会被丢弃，并写进报告：
- `count` 条件；
- 读取同一动作前面效果的 `when`；
- `min` / `max` 截断（当作近似处理）；
- `interact`（它的效果是 LLM 写的 Cypher）。

### 剪枝配置

```json
{
  "exclude_actions": ["attack", "talk", "use_on--*--site"],
  "actions": {
    "use_item": { "drop_requires": [2], "drop_effects": ["set_prop:mood"] },
    "move": { "pddl": { "pre": ["(located-at ?c ?from)", "(adjacent-to ?from ?to)"],
                        "eff": ["(not (located-at ?c ?from))", "(located-at ?c ?to)"] } }
  },
  "drop_props": ["hp"],
  "drop_relations": ["VISITED"],
  "exclude_items": [], "exclude_kinds": ["note"],
  "people": "all",
  "spawn_pool": 2, "spawn_pool_by": { "plank": 4 },
  "max_variants": 8,
  "relevance": true,
  "fallback": ["Collect item"]
}
```

| 键 | 作用 |
|---|---|
| `exclude_actions` | 不参与规划的动作，支持通配符（`use_on` 排除全部交互，`use_on--*--site` 只排除交给工地的） |
| `actions.<名>.drop_requires` / `drop_effects` | 丢掉某些条件 / 效果：写下标，或 `"op"` / `"op:<prop、rel、archetype 或 state>"` |
| `actions.<名>.pddl` | 自动版本不满意时手写 `pre` / `eff`（用编译器的谓词名，参数名是 `?<参数名>`） |
| `drop_props` / `drop_relations` | 整个属性 / 关系不参与规划（例如去掉 `hp` 后问题变成纯 STRIPS，改用 Fast Downward，更快） |
| `exclude_items` / `exclude_kinds` / `people` | 从规划对象里去掉物品 / 人（`people` 可写 `"all"` 或名单，规划者本人总在） |
| `spawn_pool` / `spawn_pool_by` | 每种可生成物品预留几个对象（需要 4 块木板就至少给 4） |
| `max_variants` | `any` / `when` 拆分变体的上限，超出时丢掉该效果 |
| `relevance` | 是否按目标做相关性剪枝 |
| `fallback` | 规则规划失败后改用编译 PDDL 重试的目标（目前支持 `Collect item` 的 `kind:` 目标） |
| `narrow_types` | 物品类别变成 PDDL 子类型（`k-log` 等），参数能收窄时自动收窄；只有一个可选对象（例如规划者本人）时直接代入常量 |
| `negative_needs` | `"initial"`：删除效果只有在初始状态里有对应事实时，才算对否定前提有帮助（能剪掉大量无关动作）；`"all"`：保守，全部保留 |
| `prune_objects` | 去掉与剩余动作无关类别的物品 |
| `max_hops` | 只保留离规划者 N 步以内的地点及其中的物品（`null` 表示不限） |
| `max_per_kind` | 每类物品最多保留几个，离得近的优先（`null` 表示不限；基础包默认 12） |
| `knowledge` | `"belief"`（默认）：规划只用人物知道的地点和物品，位置取他以为的位置；`"omniscient"`：全知 |

### 人物知识（`knowledge: "belief"`）

每个人在图里有两类信念：`(Person)-[:KNOWS_PLACE]->(Location)`（知道的地点），
以及 `(Person)-[:SAW {loc, holder, t, source}]->(Item)`（最后一次看到或听说某个物品在哪）。

- **开局**：知道起始地点及相邻地点，以及那里的东西。世界文件里可以按人物补充：
  `"knows": { "places": ["Market", "Farm"], "items": ["SecretKey"] }`，或者 `"knows": { "all": true }` 表示全知。
- **观察**：游戏循环每次扫描、每个动作完成后，都会看一眼当前地点。新看到的东西写成信念；
  以为在这里、其实已经不在的东西删掉。有变化时写一条 `observation` 记忆，LLM 回忆和对话时能检索到。
- **过时**：信念可能是错的。比如东西被别人拿走了，到现场 `pick_up` 会失败，此时信念已被纠正，重新规划即可。
- **对话**：说话者的一句话里同时提到了物品（名字或类别）和它在哪（地点或持有人），听者就学到这条信念；
  说出的地点名也会成为听者知道的地点。
- **规划**：规则规划（`item_chain`）和编译 PDDL 都只看到知道的地点、当前地点的出口，以及有信念的物品；
  自己背包里的东西总是知道的。找不到时错误信息会提示"去探索或问人"。

数值问题由 ENHSP 求解，搜索参数默认是 `-h hadd -s WAStar`，可以用环境变量 `AITOWN3_ENHSP_PARAMS` 覆盖。
不加参数时，ENHSP 默认用贪心搜索，计划会长一倍左右。

各包的 `planning.json` 按加载顺序深合并：对象逐键合并，列表整体替换。

### 用法

- 看生成结果并试剪枝：`python scripts/pddl_compile.py <角色> <goal.json> [--set '<planning 片段>'] --solve`。
  会写出 `data/pddl/domain.pddl`、`problem.pddl`、`report.txt`。
- 规模测试：`python scripts/pddl_bench.py --scales 1 5 20 [--modes raw typed pruned capped] [--goals plank site] [--dump]`。
  它会合成不同规模的世界，比较各剪枝档位的求解时间和计划长度。
- 目标 `Achieve`：用条件描述想要的世界状态，写法同 `requires`。
  其中 `@self` 指规划者本人，`kind:<类别>` 表示任一该类物品。routine 里也能用：

```json
{ "start": 8, "end": 12, "location": "Village", "goal": "Achieve",
  "conditions": [ { "op": "prop_lte", "entity": "WorkshopSite", "prop": "need_log", "value": 0 } ] }
```

---

## 6. 新增内容的步骤

1. 新动作：在 `actions/` 放一个 JSON；想让规划器使用就填 `capability`，并按能力签名排参数。
2. 新状态：在 `states/` 放一个 JSON，再写一个 `"enter_state"` 指向它的动作。
3. 新物品：在 `archetypes/` 放原型，在世界文件里用 `"archetype"` 引用。
4. 重启 web 服务并重建世界。加载错误（参数签名不符、引用了不存在的状态等）会在启动日志里列出。
