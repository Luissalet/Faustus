"""Lossless-detail choice for AX presentation; never executes browser commands."""
from __future__ import annotations


def compact_snapshot(snapshot: dict) -> dict:
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list):
        return snapshot
    # The extension owns AX parsing. If an unfamiliar graph cannot be projected
    # safely, preserve its original full representation rather than invent refs.
    if any(not isinstance(node, dict) or "ref" not in node or node["ref"] is not None and not isinstance(node["ref"], str) for node in nodes):
        return snapshot
    addressable = [node for node in nodes if isinstance(node.get("ref"), str)]
    by_ref = {node["ref"]: node for node in addressable}
    if len(by_ref) != len(addressable):
        return snapshot
    parents = {ref: [] for ref in by_ref}
    for node in nodes:
        children = node.get("child_refs", [])
        if not isinstance(children, list) or any(not isinstance(child, str) or child not in by_ref for child in children):
            return snapshot
        if node.get("ref") is not None:
            for child in children:
                parents[child].append(node["ref"])

    def duplicate_inline(node):
        if node.get("role") != "InlineTextBox" or node.get("properties"):
            return False
        texts = [node[key] for key in ("name", "value") if isinstance(node.get(key), str) and node[key]]
        if not texts:
            return False
        represented = set()
        stack, seen = list(parents[node["ref"]]), {node["ref"]}
        while stack:
            ref = stack.pop()
            if ref in seen:
                continue
            seen.add(ref)
            ancestor = by_ref[ref]
            for text in texts:
                if any(isinstance(ancestor.get(key), str) and text in ancestor[key] for key in ("name", "value")):
                    represented.add(text)
            if len(represented) == len(set(texts)):
                return True
            stack.extend(parents[ref])
        return False

    removed = set()
    known = {"ref", "role", "name", "value", "properties", "ignored", "child_refs"}
    for node in addressable:
        empty_wrapper = (node.get("role") in {"none", "generic", "Ignored", ""}
                         and not node.get("name") and not node.get("value") and not node.get("properties")
                         and not (set(node) - known))
        if empty_wrapper or duplicate_inline(node):
            removed.add(node["ref"])

    def promoted(children):
        result, seen, stack = [], set(), list(reversed(children))
        while stack:
            ref = stack.pop()
            if ref in seen:
                continue
            seen.add(ref)
            if ref in removed:
                stack.extend(reversed(by_ref[ref].get("child_refs", [])))
            else:
                result.append(ref)
        return result

    projected = []
    for node in nodes:
        # ref:null nodes have no resolvable ancestry in the extension DTO. Keep
        # their content even if a global StaticText happens to have the same text.
        if node["ref"] in removed:
            continue
        item = dict(node)
        children = promoted(node.get("child_refs", []))
        if children:
            item["child_refs"] = children
        else:
            item.pop("child_refs", None)
        for key, empty in (("name", ""), ("value", ""), ("properties", {}), ("ignored", False)):
            if item.get(key) == empty:
                item.pop(key, None)
        projected.append(item)
    return {**snapshot, "nodes": projected}


def _context_nodes(nodes: list[dict], selected: list[dict]) -> list[dict]:
    # Indices, not refs, identify rows: repeated/null refs remain original rows.
    selected_refs = {node["ref"] for node in selected if isinstance(node.get("ref"), str)}
    by_ref = {}
    for index, node in enumerate(nodes):
        ref = node.get("ref")
        if isinstance(ref, str):
            by_ref.setdefault(ref, []).append(index)
    kept = set()
    for index, node in enumerate(nodes):
        children = node.get("child_refs")
        if not isinstance(children, list) or not any(isinstance(child, str) and child in selected_refs for child in children):
            continue
        kept.add(index)
        for child in children:
            if isinstance(child, str):
                kept.update(by_ref.get(child, ()))
    # One hop only. Keep raw child_refs, order and fields, never synthesize text.
    return [node for index, node in enumerate(nodes) if index in kept]


def filtered_snapshot(snapshot: dict, view: dict) -> dict:
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        return snapshot
    query = view.get("query", "").casefold()
    roles = view.get("roles")
    query_matches = [node for node in nodes
                     if not query or any(query in str(node[key]).casefold()
                                         for key in ("name", "value") if node.get(key) is not None)]
    matches = [node for node in query_matches if roles is None or node.get("role") in roles]
    offset = int(view.get("offset", 0))
    end = offset + int(view["limit"]) if "limit" in view else len(matches)
    page = matches[offset:end]
    selected = [{key: value for key, value in node.items() if key != "child_refs"} for node in page]
    next_offset = offset + len(selected)
    selection = {"kind": "flat", "total": len(nodes), "matches": len(matches),
                 "offset": offset, "next_offset": next_offset if next_offset < len(matches) else None}
    if roles is not None and not matches and query_matches:
        matched_roles = {}
        for node in query_matches:
            role = node.get("role")
            if isinstance(role, str):
                matched_roles[role] = matched_roles.get(role, 0) + 1
        selection["matched_roles"] = matched_roles
    result = {**snapshot, "nodes": selected, "selection": selection}
    if view.get("context", False):
        result["context_nodes"] = _context_nodes(nodes, page)
    return result


def present_payload(payload: dict, detail: str, view: dict | None = None) -> dict:
    if not payload.get("ok") or not isinstance(payload.get("result"), dict):
        return payload
    if view is None and detail == "full":
        return payload
    def project(snapshot):
        return filtered_snapshot(snapshot, view) if view is not None else compact_snapshot(snapshot)

    result = project(payload["result"])
    if isinstance(result.get("snapshot"), dict):
        result = {**result, "snapshot": project(result["snapshot"])}
    return {**payload, "result": result}
