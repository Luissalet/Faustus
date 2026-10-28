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


def present_payload(payload: dict, detail: str) -> dict:
    if detail == "full" or not payload.get("ok") or not isinstance(payload.get("result"), dict):
        return payload
    result = compact_snapshot(payload["result"])
    if isinstance(result.get("snapshot"), dict):
        result = {**result, "snapshot": compact_snapshot(result["snapshot"])}
    return {**payload, "result": result}
