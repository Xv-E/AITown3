"""
Action 基类 + 通用工具

每个具体动作继承自 Action，提供 4 类信息：
  1. 元数据：name / description / category / parameters
  2. 运行时谓词：check(world, **kwargs) -> (ok, reason)
  3. 运行时副作用：apply(world, **kwargs) -> None
  4. 静态描述：preconditions_pddl / effects_pddl + to_pddl()

设计目标：
  - 同一份代码既能在 Neo4j 上「执行」，也能给 LLM/规划器输出 PDDL/自然语言描述
  - 单个 Action 实例无状态，可被 Registry 共享
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    # 仅在类型检查时引入，避免运行时与 PyPI neo4j 包名冲突
    from neo4j_world import Neo4jWorld


@dataclass(frozen=True)
class Param:
    """动作参数：名字 + Neo4j 标签类型。"""

    name: str
    type: str  # Person / Location / Item


@dataclass
class Action:
    """所有动作的基类。子类需要重写 check / apply。"""

    name: str
    description: str
    parameters: List[Param]
    category: str = "general"
    preconditions_pddl: List[str] = field(default_factory=list)
    effects_pddl: List[str] = field(default_factory=list)

    def check(self, world: "Neo4jWorld", **kwargs) -> Tuple[bool, str]:
        """检查 precondition。返回 (ok, reason)。"""
        raise NotImplementedError

    def apply(self, world: "Neo4jWorld", **kwargs) -> None:
        """应用 effect（修改 Neo4j）。"""
        raise NotImplementedError

    def execute(self, world: "Neo4jWorld", **kwargs) -> Tuple[bool, str]:
        """check + apply。失败时不改图。"""
        ok, reason = self.check(world, **kwargs)
        if not ok:
            return False, reason
        self.apply(world, **kwargs)
        return True, "ok"

    # ---------- 描述输出 ----------

    def signature(self) -> str:
        """例如 'move(c: Person, from: Location, to: Location)'。"""
        params = ", ".join(f"{p.name}: {p.type}" for p in self.parameters)
        return f"{self.name}({params})"

    def to_pddl(self) -> str:
        """生成 PDDL :action 块。"""
        params_part = " ".join(f"?{p.name} - {p.type.lower()}" for p in self.parameters)
        precs = "\n    ".join(self.preconditions_pddl) if self.preconditions_pddl else ""
        effs = "\n    ".join(self.effects_pddl) if self.effects_pddl else ""

        precondition_block = f"(and\n    {precs})" if precs else "()"
        effect_block = f"(and\n    {effs})" if effs else "()"

        return (
            f"(:action {self.name}\n"
            f"  :parameters ({params_part})\n"
            f"  :precondition {precondition_block}\n"
            f"  :effect {effect_block})"
        )


# ---------------- Cypher 工具 ----------------
# Neo4jWorld 没暴露删除单条关系的方法，这里集中封装，避免散落在各动作里。


def get_person_location(world: "Neo4jWorld", person: str) -> Optional[str]:
    """返回 person 当前 LOCATED_AT 的 Location 名（无则 None）。"""
    rows = world.run_query(
        "MATCH (p:Person {name: $name})-[:LOCATED_AT]->(l:Location) "
        "RETURN l.name AS loc LIMIT 1",
        name=person,
    )
    return rows[0]["loc"] if rows else None


def get_item_location(world: "Neo4jWorld", item: str) -> Optional[str]:
    """返回 item 通过 LOCATED_AT 所处的 Location 名（无则 None，比如已被持有）。"""
    rows = world.run_query(
        "MATCH (i:Item {name: $name})-[:LOCATED_AT]->(l:Location) "
        "RETURN l.name AS loc LIMIT 1",
        name=item,
    )
    return rows[0]["loc"] if rows else None


def get_item_holder(world: "Neo4jWorld", item: str) -> Optional[str]:
    """返回当前持有该 item 的 Person 名（无则 None）。"""
    rows = world.run_query(
        "MATCH (p:Person)-[:HAS_ITEM]->(i:Item {name: $name}) "
        "RETURN p.name AS holder LIMIT 1",
        name=item,
    )
    return rows[0]["holder"] if rows else None


def remove_relationship(
    world: "Neo4jWorld",
    a_name: str,
    a_label: str,
    b_name: str,
    b_label: str,
    rel_type: str,
    directed: bool = True,
) -> int:
    """删除 a -[rel]-> b 的关系，返回删除条数。directed=False 时删两个方向。"""
    if directed:
        cypher = (
            f"MATCH (a:{a_label} {{name: $a}})-[r:{rel_type}]->(b:{b_label} {{name: $b}}) "
            f"DELETE r RETURN count(r) AS n"
        )
    else:
        cypher = (
            f"MATCH (a:{a_label} {{name: $a}})-[r:{rel_type}]-(b:{b_label} {{name: $b}}) "
            f"DELETE r RETURN count(r) AS n"
        )
    rows = world.run_query(cypher, a=a_name, b=b_name)
    return rows[0]["n"] if rows else 0


def get_property(world: "Neo4jWorld", name: str, label: str, prop: str):
    """读取节点属性，节点不存在或属性缺失时返回 None。

    整个节点的属性一次取回：tick 读缓存下，同一实体的其他属性直接命中缓存。
    """
    rows = world.run_query(
        f"MATCH (n:{label} {{name: $name}}) RETURN properties(n) AS p LIMIT 1",
        name=name,
    )
    return (rows[0]["p"] or {}).get(prop) if rows else None


def set_property(world: "Neo4jWorld", name: str, label: str, prop: str, value) -> None:
    """写入节点属性。"""
    world.run_query(
        f"MATCH (n:{label} {{name: $name}}) SET n.{prop} = $v",
        name=name,
        v=value,
    )
