"""
Convert Neo4j query results to vis-network nodes/edges JSON.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

# Node type colors (vis-network group)
# Dark chalk doodle fills (chalk outline via vis border)
LABEL_COLORS: Dict[str, str] = {
    "Person": "#1e2e38",
    "Location": "#1e2e22",
    "Item": "#4a3a22",
    "Quest": "#2e2438",
    "Fact": "#3a241c",
    "Rumor": "#2a2620",
    "Meta": "#242018",
    "Entity": "#2a2620",
}

LABEL_SHAPES: Dict[str, str] = {
    "Person": "dot",
    "Location": "box",
    "Item": "triangle",
    "Quest": "diamond",
    "Fact": "star",
    "Rumor": "star",
    "Meta": "square",
}

_LABEL_PRIORITY = ("Person", "Location", "Item", "Quest", "Fact", "Rumor", "Meta")


def _primary_label(labels: List[str]) -> str:
    for preferred in _LABEL_PRIORITY:
        if preferred in labels:
            return preferred
    return labels[0] if labels else "Entity"


def node_id(label: str, name: str) -> str:
    return f"{label}:{name}"


def _node_from_record(labels: List[str], name: str, props: Dict[str, Any]) -> Dict[str, Any]:
    label = _primary_label(labels)
    nid = node_id(label, name)
    title_parts = [f"{label}: {name}"]
    for k, v in sorted(props.items()):
        if k != "name" and v is not None:
            title_parts.append(f"{k} = {v}")
    return {
        "id": nid,
        "label": name,
        "group": label,
        "title": "\n".join(title_parts),
        "font": {"color": "#e8dcc8"},
        "shape": LABEL_SHAPES.get(label, "dot"),
        "color": {
            "background": LABEL_COLORS.get(label, "#2a2620"),
            "border": "#e8dcc8",
            "highlight": {
                "background": "#3a241c",
                "border": "#e25a3c",
            },
        },
        "_meta": {"label": label, "name": name, "properties": props},
    }


def fetch_graph(world, limit: int = 800) -> Dict[str, Any]:
    """Fetch graph data; returns {nodes, edges, stats}."""
    rows = world.run_query(
        """
        MATCH (a)-[r]->(b)
        RETURN labels(a) AS la, a.name AS an, properties(a) AS ap,
               type(r) AS rt, properties(r) AS rp,
               labels(b) AS lb, b.name AS bn, properties(b) AS bp
        LIMIT $limit
        """,
        limit=limit,
    )

    nodes_map: Dict[str, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []
    edge_keys: Set[str] = set()

    for row in rows:
        an, bn = row.get("an"), row.get("bn")
        if not an or not bn:
            continue
        la = _primary_label(row.get("la") or [])
        lb = _primary_label(row.get("lb") or [])
        ap = dict(row.get("ap") or {})
        bp = dict(row.get("bp") or {})
        ap.setdefault("name", an)
        bp.setdefault("name", bn)

        fid = node_id(la, an)
        tid = node_id(lb, bn)
        if fid not in nodes_map:
            nodes_map[fid] = _node_from_record(row.get("la") or [la], an, ap)
        if tid not in nodes_map:
            nodes_map[tid] = _node_from_record(row.get("lb") or [lb], bn, bp)

        rt = row.get("rt") or "RELATED"
        ekey = f"{fid}|{rt}|{tid}"
        if ekey in edge_keys:
            continue
        edge_keys.add(ekey)
        rp = row.get("rp") or {}
        edges.append(
            {
                "id": ekey,
                "from": fid,
                "to": tid,
                "label": rt,
                "title": str(rp) if rp else rt,
                "arrows": "to",
                "font": {"align": "middle", "size": 10},
                "color": {"color": "#cfc3ae"},
            }
        )

    # Isolated nodes
    isolated = world.run_query(
        """
        MATCH (n)
        WHERE n.name IS NOT NULL AND NOT n:Memory AND NOT n:Terrain AND NOT (n)--()
        RETURN labels(n) AS labels, n.name AS name, properties(n) AS props
        LIMIT 50
        """
    )
    for row in isolated:
        name = row.get("name")
        if not name:
            continue
        label = _primary_label(row.get("labels") or [])
        nid = node_id(label, name)
        if nid not in nodes_map:
            props = dict(row.get("props") or {})
            props.setdefault("name", name)
            nodes_map[nid] = _node_from_record(row.get("labels") or [label], name, props)

    nodes = list(nodes_map.values())
    # vis-network does not need _meta on the client; inspect API queries it separately
    for n in nodes:
        n.pop("_meta", None)

    stats = {
        "nodes": len(nodes),
        "edges": len(edges),
        "persons": sum(1 for n in nodes if n.get("group") == "Person"),
        "locations": sum(1 for n in nodes if n.get("group") == "Location"),
        "items": sum(1 for n in nodes if n.get("group") == "Item"),
    }
    return {"nodes": nodes, "edges": edges, "stats": stats}


def inspect_entity(world, label: str, name: str) -> Dict[str, Any]:
    """Return entity details plus adjacent relationship text."""
    entity = world.get_entity_by_name(name, label=label)
    if entity is None:
        return {"found": False, "label": label, "name": name}

    if isinstance(entity, dict):
        props = {k: v for k, v in entity.items() if v is not None}
    else:
        props = {k: v for k, v in dict(entity).items() if v is not None}

    rel_rows = world.run_query(
        f"""
        MATCH (n:{label} {{name: $name}})-[r]-(other)
        RETURN type(r) AS rel, other.name AS other_name, labels(other) AS other_labels,
               properties(r) AS rprops,
               startNode(r).name = $name AS out_from_self
        ORDER BY rel, other_name
        """,
        name=name,
    )

    relationships = []
    for row in rel_rows:
        other_labels = row.get("other_labels") or []
        other_label = _primary_label(other_labels)
        rel_type = row.get("rel", "")
        direction = "->" if row.get("out_from_self") else "<-"
        relationships.append(
            {
                "type": rel_type,
                "direction": direction,
                "other_label": other_label,
                "other_name": row.get("other_name"),
                "properties": dict(row.get("rprops") or {}),
            }
        )

    context = world.get_entity_context_text(name, label=label, natural_language=True)
    result: Dict[str, Any] = {
        "found": True,
        "label": label,
        "name": name,
        "properties": props,
        "relationships": relationships,
        "context_text": context,
    }
    if label == "Person":
        try:
            from character_profile import get_character_profile

            prof = get_character_profile(world, name)
            if prof.get("found"):
                result["profile"] = prof
        except Exception:
            pass
    return result


def list_entity_names(world) -> Dict[str, List[str]]:
    return {
        "Person": world.list_entities("Person"),
        "Location": world.list_entities("Location"),
        "Item": world.list_entities("Item"),
    }
