import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("neo4j", "actions", "planner", "agent", "utility", "memory", "director", "web"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.append(p)
