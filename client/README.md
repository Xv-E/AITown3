# AITown3 Bevy Client

This is a minimal real-time Bevy shell for AITown3. It keeps the Python/FastAPI
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

- `WASD` / arrow keys: move the local avatar around the current room.
- `Tab`: cycle adjacent destinations.
- `Enter`: ask AITown3 to move the player to the selected destination.
- `E`: pick up the first available item in the room.
- `T`: talk to the first available person in the room.
- `R`: force a state refresh.

The important boundary is intentional: AITown3 returns world state and high-level
actions, while Bevy owns the real-time layer.
