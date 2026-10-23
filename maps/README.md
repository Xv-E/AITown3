# Maps

Maps are configured by hand. The Director never invents places or connections; it only places people, items, quests and factions on a map you choose.

The Director's map picker lists:

- `current`: the map loaded in Neo4j right now (the default)
- every `maps/*.json` file
- every `worlds/*.json` template world (only its places and connections are used)

## Format

```json
{
  "id": "riverside",
  "title": "Riverside",
  "description": "A mill town on a slow river.",
  "locations": [
    { "name": "Square", "description": "Cobbled market square.", "tags": "hub", "map": [0, 0], "grid": [20, 12] },
    { "name": "Mill",   "description": "Water mill by the weir.",  "map": [3, 1], "grid": [16, 10] },
    { "name": "Crypt",  "description": "Sealed family crypt.",     "map": [-3, 2], "grid": [12, 8] }
  ],
  "adjacencies": [
    { "a": "Square", "b": "Mill" },
    { "a": "Square", "b": "Crypt", "locked": true, "key": "CryptKey" }
  ]
}
```

- `map`: position on the overview map; `grid`: size of the place's own tile grid.
- `tags`: a place tagged `hub` is where the player starts when the story does not say.
- Door puzzles need a dead end: a place with at most one open exit. When the map has none, the Director turns a door puzzle into a delivery.
- Factions claim territory on these places (`factions[].territory` in a world, or claimed during play).
