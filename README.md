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
OPENAI_MODEL=qwen-turbo

# Optional local LlamaCpp (GBNF repair only)
# DEFAULT_MODEL_PATH=Models/your-model.gguf

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

## Street economy (`content/streets`, `agent/economy.py`)

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
