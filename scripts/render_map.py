"""
把一个世界的地图画成像素画 PNG。

    python scripts/render_map.py worlds/riverford.json riverford.png [--scale 2] [--no-labels]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "actions"))

from terrain_art import render_world_json  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("world")
    ap.add_argument("out")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--no-labels", action="store_true")
    a = ap.parse_args()
    print(render_world_json(a.world, a.out, scale=a.scale, labels=not a.no_labels))
