# AITown3

A small open-world play sandbox: locations, people, and items live in **Neo4j**; NPCs plan and talk with an LLM; you play in a browser client.

## What you get

- **Playable web UI** — map, people/things at a location, actions, simulation, character dossiers
- **Template worlds** — JSON scenarios under `worlds/`
- **Director** — sketch an event; AI places people, items, quests and factions on a hand-configured map
- **Living factions** — families, gangs and guilds hold territory, feud, split and rise on their own
- **NPC loop** — goals → PDDL/rule plans → timed actions; dialogue + Chroma memory
- **LLM inspect** — `/inspect` shows Qwen / local GBNF / PDDL traces

## Requirements

- Python 3.11+ (3.13 works)
- [Neo4j](https://neo4j.com/) reachable over Bolt (default `bolt://localhost:7687`)
- Optional: DashScope / OpenAI-compatible key for Qwen (primary LLM)
- Optional: local GGUF + `llama-cpp-python` for GBNF JSON repair

## Setup

```bash
cd AITown3
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` in the project root (run everything from this directory so `load_dotenv` finds it):

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=your_password

# Primary LLM (DashScope OpenAI-compatible)
LLM_PRIMARY=qwen
DASHSCOPE_API_KEY=your_key
OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
OPENAI_MODEL=qwen-flash            # NPC decisions and dialogue: frequent calls, cheap and fast
OPENAI_CREATIVE_MODEL=qwen-plus    # Director / Storyteller / object actions: rare calls, need quality

# Optional local LlamaCpp (GBNF repair only)
# DEFAULT_MODEL_PATH=Models/your-model.gguf

# NPC memory backend: chroma (default) | neo4j (:Memory nodes in the world DB)
# MEMORY_BACKEND=chroma
CHROMA_PATH=data/chroma
DEFAULT_EMBEDDING_MODEL_PATH=sentence-transformers/all-MiniLM-L6-v2
```

Password must match your Neo4j instance or you will get `Unauthorized`.

## Run the play client

From the project root:

```bash
python web/server.py --host 127.0.0.1 --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The main world is **Campfire Camp** (`worlds/town_daily_life.json`). If the database is empty when the server starts, it is built automatically; an existing world is left as it is. Load it again any time with `POST /api/worlds/town_daily_life/load` (this resets the world and NPC memories). The other templates are small demos kept as regression tests.

Useful flags:

| Flag | Meaning |
|------|---------|
| `--reload` | Hot-reload `web/` only (avoid with debugpy breakpoints) |

VS Code / Cursor: launch configs under `.vscode/launch.json` (`AITown3: Web 服务`).

### In the UI

1. **Template worlds** — load a preset from `worlds/*.json` (clears world + memory)
2. **New event** — pick a hand-configured map, then the Director drafts / expands a custom blueprint on it
3. Click exits or map rooms to move; click people/things to act
4. **Simulation** — NPCs (optionally the player) auto-plan with rules or LLM
5. **LLM log** — `/inspect` for decision and dialogue traces

## Template worlds

| Id | Title |
|----|--------|
| `key_and_door` | Key and Door — get key, open vault |
| `sealed_archive` | Sealed Archive — crowbar, cellar key, archive door |
| `sick_girlfriend` | Lila's Blue Herb — clinic → forest herb |
| `town_daily_life` | Campfire Camp — wood, berries, keep the fire |
| `two_npc_talk` | Two-NPC Dialogue Template |
| `triad_streets` | Neon Streets of Sam Shui — two triads fight street by street over protection money (16 places, 15 NPCs, street economy) |

Add a new world by placing a JSON file in `worlds/` (filename stem = world id).

### Character inner life

Persons may carry optional fields that drive emotional dialogue and motivated requests:

```json
{
  "name": "Ash",
  "desire": "To get through one night without waking in a panic.",
  "fear": "The dark — the fire went out the night the old camp burned.",
  "secret": "Sometimes burns tomorrow's wood just to keep the light high.",
  "bonds": { "Rowan": "owes him their life; hates begging him for wood" }
}
```

- Dialogue prompts include desire / fear / bonds with the listener; the secret is only confided to close people (`FRIENDS_WITH` / `FAMILY_OF`, or player trust ≥ 6).
- When an NPC genuinely asks for help, the reply may carry a `favor` → a `Quest` (`kind=favor`) whose description is the personal reason. It completes when the asker holds the item; helping as the player raises trust.
- Dialogue may also update the speaker's `mood`.
- Social relations also accept `RIVALS_WITH` and `FAMILY_OF`.

### Factions

Worlds may declare factions of any size — a family, a gang, a guild, the town watch, the whole town:

```json
"factions": [
  { "name": "Axemen", "kind": "gang", "scale": "small", "parent": "EmberCamp",
    "goal": "Wood comes first — whoever hauls the logs eats first",
    "members": [{ "name": "Rowan", "role": "boss", "loyalty": 9 }],
    "relations": [{ "with": "BerryCircle", "stance": "rival", "reason": "who eats first" }],
    "standing": { "Traveler": 0 } }
]
```

- `kind`: family | gang | guild | crew | order | authority | town | other; `scale`: small | medium | large; `stance`: allied | friendly | neutral | rival | hostile.
- Membership is inherited up `parent` (at half loyalty), so a town's feud reaches every family in it.
- **Attitude**: when two people meet, shared factions, the stances between their factions, and the other person's standing with your factions give a score (loyal / friendly / neutral / wary / hostile). It goes into dialogue and NPC decision prompts, and counts toward whether a secret is confided.
- **Standing ripples**: giving (+2), attacking (−6), talk outcomes and kept favors change the actor's standing with every faction of the target, and with that faction's allies (same way) and rivals / enemies (opposite way). Small factions react hardest; hurting your own faction counts 1.5×.
- Facts may set `"faction"`: that faction's private business, shared with members, maybe with allies, never with rivals.
- The play UI shows your standing with each faction; dossiers list memberships.

#### Territory and traits

- `territory`: `{"Place": strength 0-10}`. A place is **held** by the faction with the highest strength (≥ 1); the map shows a coloured dot and the tooltip names the holder.
- `traits`: `[{"id": "...", "why": "..."}]`, picked from `expansionist`, `insular`, `welcoming`, `militant`, `pacifist`, `vengeful`, `mercantile`, `zealous`, `secretive`. Each one needs a story reason (`why`) and has a mechanical effect:

| trait | effect |
|-------|--------|
| expansionist | claims ground 1.5× faster |
| insular | claims 0.7×, join threshold +2 |
| welcoming | join threshold −2, good deeds ripple 1.25×, recruits less loyal people |
| militant | wears rivals down 1.5× in contested places, escalates faster |
| pacifist | escalates at half speed, condemns any attack (−1 standing for the attacker) |
| vengeful | wrongs ripple 1.5×, escalates 1.5× |
| mercantile | gifts and favours ripple 1.5×, escalates slower |
| zealous | members' loyalty +0.2 per round, escalates faster |
| secretive | join threshold +1 |

- On a faction's own ground it sees everything: standing changes there count 1.25×. Meeting someone from a rival faction on your ground makes you warier of them (−1 attitude); on their ground, −0.5.

#### Factions evolve on their own

Every `FACTION_TICK_HOURS` (default 6) game hours, `agent/faction_sim.py` runs a round. It also catches up, at most 4 rounds at a time, after skipping time.

1. **Territory**: members standing in a place raise their faction's hold (more loyalty, faster). Unwatched ground loses 0.3 per round. Rival or hostile factions in the same place wear each other down.
2. **Tension**: factions sharing ground build tension (allies ease it). At ±6 (about a game day and a half of friction) the stance moves one step along hostile ↔ rival ↔ neutral ↔ friendly ↔ allied. Parent and child factions never clash.
3. **Loyalty**: rises on your own ground; falls away from it, on hostile ground, when your faction loses ground, or when you dislike its leader.
4. **New factions**, at most one per game day:
   - **splinter**: a member whose loyalty falls to 3 or below walks out with like-minded members (towns don't split);
   - **coalition**: two people from different sides, both barely loyal and close to each other, found their own;
   - **arrival**: newcomers settle an unheld place (`FACTION_ARRIVAL_CHANCE`, default 0.15 per round). They keep unnamed members there until driven out.

   The LLM writes the new faction's name, goal, origin and traits, grounded in the founders' lives, the parent faction's history and recent events. Each trait must cite the story. Without an LLM (or with `FACTION_STORY_LLM=0`), rules write them.
5. **Recruiting, growth, decline**: a faction takes in people on its ground whose loyalty elsewhere has faded. It grows small → medium → large with members plus ground, and dissolves with neither.

Every event goes into the chronicle (`:FactionEvent`, shown in the play UI). Members remember it, and it is announced as news, so NPCs reconsider their plans. Dialogue and decision prompts show each faction's traits, territory and latest history.

Knobs: `FACTION_SIM=0` turns evolution off; `FACTION_MAX_ACTIVE` (default 12). `POST /api/factions/tick?ticks=N` pushes history forward right away.

#### Joining factions

Each faction has a joining rule (`join_policy`, `join_standing`, `trial`; defaults depend on `kind`):

| policy | defaults | what it takes |
|--------|----------|---------------|
| `open` | town | standing ≥ threshold, and a member here to ask |
| `sponsor` | guild, crew, other | as `open`, plus a member here who leans ≥ 3 toward you (attitude + personal trust) |
| `trial` | gang, order, authority | as `open`, then a `Quest(kind=trial)`: hand the leader `trial.item` yourself |
| `closed` | family | outsiders cannot join |

- Being in a faction it is hostile to blocks joining; being in a rival adds +2 to the threshold.
- Joining makes you a `recruit`, raises your standing with the faction by 1, shifts allies (+) and rivals / enemies (−1 / −2), and teaches you the faction's facts. At standing ≥ 7 you become a full `member`.
- Leaving costs −3 standing.
- NPCs who are members are told the real rules when they talk to you, so they don't promise what the game won't allow.
- API: `POST /api/factions/{name}/join` (`{"via": "Rowan"}` optional), `POST /api/factions/{name}/leave`. The play UI shows a join / leave button per faction, with the reason in the tooltip when it is disabled.

## Street economy (`content/streets`, `agent/street_economy.py`)

Money is `cash` items: one note is one dollar. Money never appears or disappears; every transaction moves notes from one pocket to another.

| flow | how |
|------|-----|
| buy food / drink | use a note on a shop (`noodle_stall`, `fishball_cart`, `tea_house`, `bar_counter`, `herbal_counter`): stock −1, the note goes to the shop's `owner`, you get the food |
| restock | a sold-out shop alerts its owner (`works_at`), who buys `supplies` at the `wholesale` (the note goes to the wholesaler) and uses them on the shop (+4 stock). Boats refill the wholesale daily. |
| protection | every shop with a `protection_fee` owes that much per day to the **gang that holds its ground** (`(:Faction)-[:EXTORTS {dues}]->(:Item)`). A gang member runs `collect_dues` there to take what is owed; the boss takes half. |
| wages | factions with `wage` (gangs, the dock crew): the leader pays each member every day |
| rent | a `rent_book` item: each tenant pays the landlord every day; arrears are tracked |

How it ties into factions and the story:

- Each payment strengthens the collecting gang's hold on that place (+0.5). Collecting from an unprotected shop starts a new racket there (+1).
- **Muscling in**: collecting on a rival's ground takes the rival's money, costs −3 standing with them, adds tension between the gangs, and is written into the chronicle.
- A police member (`authority` faction) on the spot stops the collection and remembers who tried.
- **Three days behind**: the gang smashes the shop (stock 0, owner hurt and afraid). This adds tension with the owner's guild and goes into the chronicle.
- NPC dialogue and decisions see their cash, what they owe or are owed, and their shop's stock. Routines buy and eat (`"goal": "Use item on", "item_kind": "cash"`, then `"Use item", "item_kind": "food"`) and collect (`"action": "collect_dues"`).
- The play UI shows your cash, the money supply, and every shop's owner, protector, dues and stock. Cash is grouped in the inventory (`Cash ×4`).
- `ECONOMY_SIM=0` turns the daily economy off.

## Project layout

```
AITown3/
├── web/           # FastAPI server + static play / inspect UI
├── worlds/        # Template world JSON
├── agent/         # Goals, planning, dialogue, game loop
├── actions/       # Move / talk / use_on / … + world interaction table
├── director/      # Blueprint plan / validate / materialize
├── neo4j/         # Neo4jWorld graph access
├── planner/       # PDDL / unified-planning / BFS pathfinding
├── memory/        # Chroma NPC memory
├── utility/       # LLM router, GBNF, env loading
└── test/          # Pytest suite
```

## Tests

```bash
# From project root, with Neo4j up for graph-backed tests
pytest test/ -v
```

Some tests skip if Neo4j or `llama_cpp` is unavailable.

> ⚠️ Graph-backed tests **clear and rebuild the Neo4j database** they connect to. Reload a template world in the UI afterwards. Chroma memory tests use a temporary directory and never touch `data/chroma`.

`test/test_sim_regression.py` runs the campfire world for 40 ticks (rule mode, serial and parallel planning) and checks invariants: no errors, every NPC acts, the player never auto-acts.

## Seamless world

The world is one continuous terrain map (`actions/terrain_gen.py`, stored as a `:Terrain` node). Locations divide it the way places divide a real map:

- **Outdoor places** grow out from their centres with noisy borders, so they interlock (the Village wraps around the Forest) instead of being a row of rectangles.
- **Indoor places** (`"indoor": true`, `"parent": "TownSquare"`) are walled rooms in a building that stands inside the parent place. Linked rooms share a building and have doors between them; a room linked to another outdoor place gets a back door onto it.
- **Walls (`#`), hedges/fences (`%`) and water (`~`) are obstacles** and belong to no place. Places are only walkable into each other where the graph says they are adjacent: an open border, a doorway (`+`), or a gate in a fence. Touching places that aren't adjacent get a hedge between them. A locked door item sits in its doorway and blocks it until opened.

```json
{"name": "Forest", "tags": "work,wood", "area": [0, 10, 20, 20]},
{"name": "Tavern", "tags": "food,social", "indoor": true, "parent": "TownSquare", "grid": [16, 12]}
```

`area` / `map` place a location's centre (optional); `grid` sets its rough size. The same world always gets the same map. A world can also hand-draw its map: `"terrain": {"rows": [...], "legend": {"V": "Village", "t": "Tavern"}}` with `#`, `%`, `~`, `+` and spaces as above. Places a questline adds later grow into the wilderness next to their neighbour; existing ground isn't touched.

- **AI reasons in regions** (PDDL, routines, knowledge, the Storyteller): "go to the Forest", "chop the OakTree".
- **The world runs in cells** (`actions/spatial.py`): people have world coordinates; `move` and "walk up to the target first" become A* paths whose length sets the action's duration (`WALK_CELLS_PER_SEC`). The path goes to the client so NPCs walk it in time.
- **Who is around** is distance-based (`VIEW_RADIUS`), not same-region, but walls hide people: someone indoors can't see someone outside, and the other way round.
- **The player walks freely**: the client reports the player's cell (`POST /api/player/position`); crossing into another region changes their region on the server.
- The play state's `world` block carries every region, item and person in world cells.

Coordinates: `Person.x/y` are world cells; `Item.x/y` stay local to the item's region (add the region's `area_x/area_y`, which is the bounding box of its ground on the map). Items are only placed on the location's own ground, never in walls, doorways or a nested building.

## Object actions

Interactive objects (a tree, a dial…) list what can be done with them in their script, as **data**: the same `requires` / `effects` primitives as the content-pack actions.

```json
"script": {
  "initial_state": {"logs": 3},
  "on_tick": [{"op": "add_prop", "prop": "logs", "delta_per_sec": 0.02, "max": 3}],
  "actions": [
    {"id": "chop", "label": "Chop for wood",
     "requires": [{"op": "prop_gte", "entity": "@target", "prop": "logs", "value": 1,
                   "message": "@target has no good branches left; give it a few hours"}],
     "effects": [{"op": "spawn_item", "archetype": "firewood", "held_by": "@actor"},
                 {"op": "add_prop", "entity": "@target", "prop": "logs", "delta": -1, "min": 0}],
     "narrative": "@actor chops a dry log from @target."}
  ]
}
```

Placeholders: `@actor`, `@target` (the object), `@here` (where it stands). The engine adds "actor is alive and next to the object".

- **Same for everyone**: the player's menu, NPC decisions and the planner all see these actions. The play state lists each object's actions with `available` / `reason`, so the player sees what they can do right now and why the rest can't be done.
- **Validated** (`agent/ephemeral_action_dsl.py`); `test/test_authored_actions.py` checks every action in `worlds/*.json`.
- **Planned with**: the PDDL compiler turns each action into a normal PDDL action, so "campfire fuel ≥ 80" is solved as chop → walk back → feed the fire, and an NPC can work out how to solve the dial — no hand-written rules.

**LLM-invented actions are off by default** so the world stays predictable. With `AITOWN3_INVENT_ACTIONS=1`, objects without authored actions get 2–4 actions invented by the LLM in the same format (validated, with one round of error feedback). Authored actions always take priority.

## Questlines (Director v2)

`quest/` adds a questline **into the running world** — new regions, people, items with object actions, clues, and 2–6 stages — without clearing anything.

```bash
curl -X POST http://127.0.0.1:8765/api/questlines/draft -H "Content-Type: application/json" -d "{\"theme\": \"someone steals from the camp at night\"}"
curl -X POST http://127.0.0.1:8765/api/questlines/apply -H "Content-Type: application/json" -d "{}"
curl http://127.0.0.1:8765/api/questlines
```

1. **Draft** (`quest/author.py`): the creative model writes a JSON patch from a digest of the world (people, places, grudges, story arc). Obvious slips are fixed automatically; anything else is sent back with its previous answer so it edits rather than rewrites.
2. **Validate** (`quest/spec.py`): names, places, people and every condition / reward / object action use the same primitives and rules as object actions.
3. **Prove it's playable** (`quest/verify.py`): on an in-memory copy of the world, each stage becomes a PDDL action (*conditions → reward*) and the planner must finish the line as the player. Rejected: unreachable stages (the first one is named), stages already true at the start, and stages that complete the moment the previous one does. The plan is returned as a walkthrough.
4. **Apply** (`quest/apply.py`): written incrementally after re-checking against the world as it is now; new regions are laid out next to the map.
5. **Play** (`quest/runtime.py`, a `GameLoop` hook): each tick the current stage's conditions are checked; on completion the reward is applied, the news goes to everyone and the next stage's giver is prompted. The current objective shows under the Bevy clock.

Only `spatial`/`graph_ops`/`compiler` are reused; the engine sees questlines only through `GameLoop.hooks` and the compiler's generic `extra_actions`.

## Storyteller and plan conflicts

While the simulation runs, the **Storyteller** (`agent/storyteller.py`) reads the camp every few game hours — who is doing what, recent plan conflicts, grudges, public news — and asks the LLM where the story goes next. It can only act through three validated beats:

| Beat | Effect |
|------|--------|
| `announce` | Public event: chronicle entry + a news trigger for everyone |
| `whisper` | Private rumour / thought for one NPC; `urgent` interrupts their current plan |
| `adjust` | Modest change to a number an item's script actually uses (e.g. wind drains campfire `fuel`) |

LLM calls run off the tick thread and apply on a later tick. Without an LLM (rule mode) it falls back to templates. The current arc is in the play state (`simulation.story`) and shown under the Bevy clock.

**Plan conflicts** (`agent/conflicts.py`): when an NPC takes or uses up an item that someone else's plan relies on — after that person decided on the plan — the other plan is interrupted and replanned, both get a trigger, and the loser's `TENSION` toward the taker grows. Grudges appear in each NPC's LLM prompt and feed the Storyteller. Recent conflicts are in `simulation.conflicts`.

## Performance knobs

| Env var | Default | Meaning |
|---------|---------|---------|
| `AITOWN3_PLAN_WORKERS` | `4` | NPCs that decide/plan in parallel per tick (`1` = serial) |
| `AITOWN3_UP_TIMEOUT` | `20` | Seconds before a unified-planning solve falls back |
| `AITOWN3_STORYTELLER` | `1` | `0` turns the Storyteller off |
| `OPENAI_CREATIVE_MODEL` | `OPENAI_MODEL` | Model for the Director, Storyteller and object-action generation |
| `AITOWN3_INVENT_ACTIONS` | `0` | `1` lets the LLM invent actions for objects that have none authored |
| `AITOWN3_STORY_HOURS` | `4` | Game hours between story beats |

Within a tick, identical read queries hit Neo4j once (`Neo4jWorld.read_cache`); any write query invalidates the cache.

## Architecture (short)

```
Browser UI ──HTTP──► web/server.py
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
    Neo4jWorld      WorldSimulator    LLM / GBNF
    (graph)         (GameLoop ticks)  (dialogue + goals)
         ▲               │
         └──── actions / planner / director ────┘
```

- **State** — people, items, locations, facts, quests in Neo4j
- **Interactions** — item `effects_on` × target `accepts_from` compile to temporary `use_on` ops
- **Planning** — goal terms → rule pathfinding and/or PDDL; feasibility gate before commit
- **Memory** — Chroma embeddings for recent actions / dialogue per character

## License

Private / unspecified — add a license if you publish the repo.
