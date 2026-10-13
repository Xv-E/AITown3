"""从当前 Neo4j 世界自动编译 PDDL，写出 domain / problem / report，可选求解。

用法:
  python scripts/pddl_compile.py Kai '[{"op": "has_item", "person": "@self", "item": "kind:plank"}]' --solve
  python scripts/pddl_compile.py Kai goal.json --set '{"drop_props": ["hp"]}' --out data/pddl

goal 是条件列表（写法同动作 requires；@self = 规划的角色，kind:<类别> = 任一该类物品），
可以直接写 JSON，也可以给 JSON 文件路径。--set 临时叠加在 planning.json 上，方便试剪枝。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("neo4j", "actions", "planner", "agent", "utility"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from compiler import compile_pddl, load_config, load_snapshot  # noqa: E402
from neo4j_world import Neo4jWorld  # noqa: E402


def _load_json(raw: str):
    path = Path(raw)
    return json.loads(path.read_text(encoding="utf-8") if path.is_file() else raw)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("character")
    ap.add_argument("goal", help="条件列表 JSON 或文件路径")
    ap.add_argument("--set", dest="overrides", default="", help="临时叠加的 planning.json 片段")
    ap.add_argument("--out", default=str(ROOT / "data" / "pddl"))
    ap.add_argument("--solve", action="store_true")
    args = ap.parse_args()

    overrides = _load_json(args.overrides) if args.overrides else None
    world = Neo4jWorld()
    try:
        snap = load_snapshot(world)
    finally:
        world.close()
    compiled = compile_pddl(snap, args.character, _load_json(args.goal), config=load_config(overrides))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "domain.pddl").write_text(compiled.domain, encoding="utf-8")
    (out / "problem.pddl").write_text(compiled.problem, encoding="utf-8")
    (out / "report.txt").write_text(compiled.report.text() + "\n", encoding="utf-8")
    print(f"actions: {', '.join(compiled.action_names())}")
    print(f"wrote {out / 'domain.pddl'}, problem.pddl, report.txt")
    print("--- report ---")
    print(compiled.report.text())

    if args.solve:
        from solver import solve_pddl

        t0 = time.perf_counter()
        result = solve_pddl(compiled.domain, compiled.problem, engines=["unified_planning"])
        took = time.perf_counter() - t0
        print(f"--- solve: {'ok' if result['success'] else 'FAILED'} ({result.get('engine')}, {took:.1f}s)")
        if not result["success"]:
            print(result.get("error"))
            return 1
        for name, step_args in compiled.decode(result["plan"]):
            print(f"  {name}({', '.join(step_args)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
