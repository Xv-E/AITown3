# AITown3

A small open-world play sandbox: locations, people, and items live in **Neo4j**; NPCs plan and talk with an LLM; you play in a browser client.

## What you get

- **Playable web UI** — map, people/things at a location, actions, simulation, character dossiers
- **Template worlds** — JSON scenarios under `worlds/`
- **Director** — sketch an event; AI expands it into a playable blueprint
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
2. **New event** — Director drafts / expands a custom blueprint
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
