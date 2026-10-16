# AITown3 Bevy Client

This is a minimal real-time Bevy shell for AITown3. The world is one seamless map:
the camera follows the player, every region, item and NPC is in the same scene. It keeps the Python/FastAPI
server as the AI/world brain and uses Bevy for frame-level presentation,
movement, input, and future game-feel systems.

## Run

Start the existing AITown3 server first:

```powershell
python ..\web\server.py --host 127.0.0.1 --port 8765
```

Then run the Bevy client:

```powershell
cargo run
```

The client defaults to `http://127.0.0.1:8765`. Override it with:

```powershell
cargo run -- --server http://127.0.0.1:8765
```

If the server is not reachable, the client starts with an offline demo scene so
the game-side loop can still be developed.

## Controls

- `WASD` / arrow keys: walk anywhere in the world (fixtures block the way).
- `E` / click: interact with whoever or whatever is next to you.
- `M`: world map; pick a region and press `Enter` to walk there (any move key stops).
- `I`: bag. `B`: build. `H`: an NPC's history. `P`: start/stop NPCs (`Shift+P`: LLM).
- `T`: skip an hour (`Shift+T`: six). `R`: force a state refresh. `~`: debug.

The important boundary is intentional: AITown3 returns world state and high-level
actions, while Bevy owns the real-time layer.

## Live updates

The client subscribes to `GET /api/stream` (Server-Sent Events) on its own thread.
The server pushes a full play state after every simulation tick and after any
request that changes the world, so the view stays live even while a long dialogue
request is waiting on the LLM. If the stream drops, the client reconnects with
backoff and falls back to polling `/api/play/state` until it is back.

Each NPC's pending action carries the route the server planned (`pending_path`, world
cells) and when it completes (`pending_completes_at`, sim seconds). `SimClock` estimates
how fast sim time runs in real time, and the adapter walks the NPC along that route so it
arrives as the server's walk ends; without a route the NPC glides to its server cell.
The grey line under an NPC's name is their current goal; walk up to them to see their
reasoning.
