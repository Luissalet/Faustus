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
    known = {"ref", "role", "name", "value", "properties", "ignored", "child_refs", "in_view"}
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
    if view.get("visible"):
        query_matches = [node for node in query_matches if node.get("in_view") is not False]
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


TEXT_CONTROL_ROLES = {"button", "link", "checkbox", "radio", "switch", "textbox", "searchbox", "combobox", "listbox",
                      "menuitem", "menuitemcheckbox", "menuitemradio", "option", "tab", "spinbutton", "slider"}
TEXT_PLAIN_ROLES = {"StaticText", "InlineTextBox", "text"}
TEXT_DEFAULT_LIMIT = 400


def _reading_order(nodes: list[dict]) -> list[dict]:
    """Document order. The extension lists nodes depth-first; when the list disagrees with the tree, walk the tree."""
    by_ref = {node["ref"]: node for node in nodes if isinstance(node.get("ref"), str)}
    children = {child for node in nodes for child in (node.get("child_refs") or []) if isinstance(child, str)}
    walked, seen = [], set()
    stack = [node["ref"] for node in reversed(nodes) if isinstance(node.get("ref"), str) and node["ref"] not in children]
    while stack:
        ref = stack.pop()
        if ref in seen or ref not in by_ref:
            continue
        seen.add(ref)
        walked.append(ref)
        stack.extend(reversed([c for c in (by_ref[ref].get("child_refs") or []) if isinstance(c, str)]))
    listed = [node["ref"] for node in nodes if isinstance(node.get("ref"), str)]
    if walked == listed:
        return nodes                                  # null-ref rows keep their place
    return [by_ref[ref] for ref in walked] + [node for node in nodes if not isinstance(node.get("ref"), str)]


def text_view(snapshot: dict, view: dict) -> dict:
    """The page as reading lines: text in document order, one line per text, controls as ``[role ref] label``.

    Built from the compact projection (no duplicated inline text, no empty wrappers), so a list of products reads as
    name, price, button. ``query`` keeps only lines containing it (case-insensitive); ``offset``/``limit`` page the lines
    (default limit 400). Refs are the snapshot's own, usable with click/type/press."""
    compact = compact_snapshot(snapshot)
    nodes = compact.get("nodes")
    if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes):
        return compact
    lines: list[str] = []
    last_label = ""
    on_screen_only, off_screen = bool(view.get("visible")), 0
    for node in _reading_order(nodes):
        if node.get("ignored"):
            continue
        role = node.get("role")
        name = " ".join(str(node.get("name") or "").split())
        value = node.get("value")
        value_text = " ".join(str(value).split()) if value not in (None, "", False) else ""
        if role in TEXT_CONTROL_ROLES:
            label = name or value_text
            if not label:
                continue
            props = node.get("properties") or {}
            flags = [flag for flag in ("disabled", "checked", "selected", "expanded") if props.get(flag) is True]
            line = f"[{role} {node.get('ref')}] {label}"
            if value_text and value_text != label:
                line += f" = {value_text}"
            if flags:
                line += f" ({', '.join(flags)})"
            url = props.get("url")
            if role == "link" and isinstance(url, str) and url.startswith(("http://", "https://")):
                line += f" -> {url}"                  # where it goes: open it with open_tab without clicking
            last_label = label
        elif role == "heading" and name:
            line, last_label = "# " + name, name
        elif role in TEXT_PLAIN_ROLES and name:
            if name == last_label:
                continue                              # the label of the control just written
            line, last_label = name, name
        else:
            continue
        if on_screen_only and node.get("in_view") is False:
            off_screen += 1                           # above or below the viewport now
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    query = str(view.get("query", "")).casefold()
    numbered = [(i, line) for i, line in enumerate(lines) if not query or query in line.casefold()]
    offset = int(view.get("offset", 0))
    limit = int(view.get("limit", TEXT_DEFAULT_LIMIT))
    page = numbered[offset:offset + limit]
    next_offset = offset + len(page)
    result = {key: value for key, value in compact.items() if key != "nodes"}
    result["text"] = "\n".join(line for _i, line in page)
    result["selection"] = {"kind": "text", "total_lines": len(lines), "matches": len(numbered), "offset": offset,
                           "next_offset": next_offset if next_offset < len(numbered) else None}
    if on_screen_only:
        result["selection"]["off_screen_lines"] = off_screen
    return result


def present_payload(payload: dict, detail: str, view: dict | None = None) -> dict:
    if not payload.get("ok") or not isinstance(payload.get("result"), dict):
        return payload
    if view is None and detail == "full":
        return payload
    def project(snapshot):
        if view is not None and view.get("text"):
            return text_view(snapshot, view)
        return filtered_snapshot(snapshot, view) if view is not None else compact_snapshot(snapshot)

    result = project(payload["result"])
    if isinstance(result.get("snapshot"), dict):
        result = {**result, "snapshot": project(result["snapshot"])}
    return {**payload, "result": result}
