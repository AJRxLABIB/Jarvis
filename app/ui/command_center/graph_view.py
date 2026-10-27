"""View-model of the knowledge-graph browser (no Qt): which nodes to list, and the details of one node with every
relationship and its evidence. It only reads the graph that ``app.core.knowledge_graph.build`` made from data Jarvis
already has; nothing is invented and nothing is stored."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.knowledge_graph import Graph, Node

KIND_LABEL = {"project": "Projects", "technology": "Technologies", "file": "Files", "memory": "Memories",
              "decision": "Decisions", "note": "Notes", "goal": "Goals", "mission": "Missions",
              "request": "Requests", "action": "Actions"}
SINGULAR = {"project": "Project", "technology": "Technology", "file": "File", "memory": "Memory",
            "decision": "Decision", "note": "Note", "goal": "Goal", "mission": "Mission", "request": "Request",
            "action": "Action"}
LIMIT = 300


def _one(kind: str) -> str:
    return SINGULAR.get(kind, kind.title())


@dataclass(frozen=True)
class Row:
    id: str
    kind: str
    name: str
    links: int


def kinds(graph: Graph) -> list[tuple[str, str, int]]:
    """(kind, label, count) for the filter, only kinds that exist."""
    counts: dict[str, int] = {}
    for node in graph.nodes.values():
        counts[node.kind] = counts.get(node.kind, 0) + 1
    return [(k, KIND_LABEL.get(k, k.title()), n) for k, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def rows(graph: Graph, kind: str = "", query: str = "") -> tuple[list[Row], int]:
    """Nodes of ``kind`` (all when empty) containing ``query``, most connected first. Returns (rows, total matches)."""
    degree: dict[str, int] = {}
    for edge in graph.edges:
        degree[edge.source] = degree.get(edge.source, 0) + 1
        degree[edge.target] = degree.get(edge.target, 0) + 1
    q = " ".join(query.lower().split())
    matches = [n for n in graph.nodes.values() if (not kind or n.kind == kind) and (not q or q in n.name.lower())]
    matches.sort(key=lambda n: (-degree.get(n.id, 0), n.name.lower()))
    return [Row(n.id, n.kind, n.name, degree.get(n.id, 0)) for n in matches[:LIMIT]], len(matches)


def details(graph: Graph, node_id: str) -> str:
    node: Node | None = graph.nodes.get(node_id)
    if node is None:
        return "Select an item to see how it connects."
    lines = [f"{_one(node.kind)}: {node.name}", f"Source: {node.source or 'not recorded'}"]
    for key, value in node.attrs.items():
        lines.append(f"{key}: {value}")
    links = graph.neighbours(node_id)
    lines += ["", f"Connections ({len(links)}):" if links else "No connections found."]
    for edge, other in links:
        arrow = "->" if edge.source == node_id else "<-"
        lines.append(f"  {arrow} {edge.relation} {_one(other.kind).lower()}: {other.name}"
                     + (f"   [{edge.evidence}]" if edge.evidence else ""))
    return "\n".join(lines)


def neighbour_ids(graph: Graph, node_id: str) -> list[str]:
    return [other.id for _, other in graph.neighbours(node_id)]
