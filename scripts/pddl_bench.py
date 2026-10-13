"""编译 PDDL 的规模基准：按倍数合成大世界，比较各剪枝档位的编译 / 求解耗时。

用法:
  python scripts/pddl_bench.py                      # 默认倍数 1 5 20，全部档位
  python scripts/pddl_bench.py --scales 1 10 --modes pruned capped --timeout 30
  python scripts/pddl_bench.py --scales 5 --dump    # 另外写出 data/pddl_bench/<档位>-<倍数>/ 下的 domain / problem

档位：
  raw     只做动作相关性剪枝（旧行为）
  typed   + 物品类别收窄为类型
  pruned  + 对象剪枝（默认配置）
  capped  + max_hops=3, max_per_kind=4
世界不需要 Neo4j；不依赖 Neo4j，全在内存里合成。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("actions", "planner", "agent", "utility"):
    p = str(ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

MODES = {
    "raw": {"narrow_types": False, "prune_objects": False},
    "typed": {"prune_objects": False},
    "pruned": {},
    "capped": {"max_hops": 3, "max_per_kind": 4},
}
JUNK = ["flower", "pebble", "feather", "mushroom", "shell", "bone", "rope", "cloth", "nail", "herb"]
GOALS = {
    "plank": [{"op": "has_item", "person": "@self", "item": "kind:plank"}],
    "site": [{"op": "prop_lte", "entity": "Site", "prop": "need_log", "value": 0}],
}


def _item(arch: str, **extra):
    from content import apply_archetype

    spec = apply_archetype({"archetype": arch, **extra})
    props = {
        k: (json.dumps(v) if k in ("effects_on", "accepts_from") else v)
        for k, v in spec.items()
        if k not in ("script", "blueprint", "footprint")
    }
    props.update(((spec.get("script") or {}).get("initial_state") or {}))
    props.update(extra)
    return ("Item", props)


def synth(scale: int, seed: int = 7):
    """4×scale 个区域连成一条带支路的链；Village 在一端，木头散在各处，外加杂物和路人。"""
    from compiler import Snapshot

    rng = random.Random(seed)
    snap = Snapshot()
    zones = ["Village"] + [f"Zone{i}" for i in range(1, 4 * scale)]
    edges = set()
    for i, z in enumerate(zones):
        snap.objects[z] = ("Location", {})
        if i:
            parent = zones[i - 1] if i % 5 else zones[max(0, i - 3)]
            edges |= {(z, parent), (parent, z)}
    snap.edges["ADJACENT_TO"] = edges

    def put(name, obj, where):
        snap.objects[name] = obj
        snap.location[name] = where

    put("Kai", ("Person", {"hp": 100, "power_level": 5}), "Village")
    for i in range(3 + scale):
        put(f"Walker{i}", ("Person", {"hp": 100, "power_level": 5}), rng.choice(zones))
    put("Workshop", _item("workbench"), "Village")
    put("Bakery", _item("oven"), "Village")
    put("Site", _item("house_site", need_log=3), "Village")
    for i in range(8 * scale):
        put(f"Log{i}", _item("log"), rng.choice(zones[1:] or zones))
    for i in range(6 * scale):
        put(f"Stone{i}", _item("stone"), rng.choice(zones))
    for i in range(6 * scale):
        put(f"Wheat{i}", _item("wheat"), rng.choice(zones))
    for i in range(20 * scale):
        kind = JUNK[i % len(JUNK)]
        put(f"{kind.title()}{i}", ("Item", {"item_kind": kind, "portable": True, "value": 1}), rng.choice(zones))
    return snap


def run(scale: int, mode: str, goal: str, timeout: float, dump: bool):
    from compiler import PlanningConfig, compile_pddl
    from solver import solve_pddl

    snap = synth(scale)
    t0 = time.perf_counter()
    compiled = compile_pddl(snap, "Kai", GOALS[goal], config=PlanningConfig.from_dict(MODES[mode]))
    compile_s = time.perf_counter() - t0
    comp = compiled.compiler
    if dump:
        out = ROOT / "data" / "pddl_bench" / f"{mode}-{goal}-{scale}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "domain.pddl").write_text(compiled.domain, encoding="utf-8")
        (out / "problem.pddl").write_text(compiled.problem, encoding="utf-8")
        (out / "report.txt").write_text(compiled.report.text() + "\n", encoding="utf-8")
    t0 = time.perf_counter()
    result = solve_pddl(compiled.domain, compiled.problem, engines=["unified_planning"])
    solve_s = time.perf_counter() - t0
    plan = compiled.decode(result.get("plan") or []) if result.get("success") else []
    return {
        "objects": f"{len(comp.objs)}/{len(snap.objects) + sum(1 for o in comp.objs.values() if o.pool)}",
        "actions": len(comp.schemas),
        "init": compiled.problem.count("\n    ("),
        "compile": f"{compile_s:.2f}s",
        "solve": f"{solve_s:.1f}s" if result.get("success") else ("timeout" if "超时" in str(result.get("error")) else "FAIL"),
        "engine": str(result.get("engine") or "").replace("up:", ""),
        "steps": len(plan),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scales", type=int, nargs="+", default=[1, 5, 20])
    ap.add_argument("--modes", nargs="+", default=list(MODES), choices=list(MODES))
    ap.add_argument("--goals", nargs="+", default=list(GOALS), choices=list(GOALS))
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args()
    os.environ["AITOWN3_UP_TIMEOUT"] = str(args.timeout)
    from unified_planning.shortcuts import get_environment

    get_environment().credits_stream = None

    cols = ["goal", "scale", "mode", "objects", "actions", "init", "compile", "solve", "engine", "steps"]
    print(" | ".join(f"{c:>8}" for c in cols))
    for goal in args.goals:
        for scale in args.scales:
            for mode in args.modes:
                row = {"goal": goal, "scale": scale, "mode": mode, **run(scale, mode, goal, args.timeout, args.dump)}
                print(" | ".join(f"{str(row[c]):>8}" for c in cols), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
