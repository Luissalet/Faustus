"""view:{text:true} — the page as reading lines. Synthetic AX only, never a browser."""
import json

from src.browser_relay.protocol import validate_arguments
from src.browser_relay.server import mcp_result
from test_presentation import node, payload, snapshot


def text_of(value, view):
    result = mcp_result(value, detail="compact", view=view)
    data = json.loads(result.content[0].text)
    assert data == result.structuredContent
    return data["result"]


def shop_page():
    tree = snapshot()
    tree["nodes"] = [
        node("root", "RootWebArea", "Carrefour", ["h", "row1", "row2"]),
        node("h", "heading", "Lista “Favoritos”"),
        node("row1", "generic", children=["t1", "p1", "b1"]),
        node("t1", "link", "Pokémon Caja Mew", ["t1s"], properties={"focusable": True, "url": "https://shop.test/mew/p"}),
        node("t1s", "StaticText", "Pokémon Caja Mew"),
        node("p1", "StaticText", "49,99 €"),
        node("b1", "button", "Agotado temporalmente", ["b1s"], properties={"disabled": True}),
        node("b1s", "StaticText", "Agotado temporalmente"),
        node("row2", "generic", children=["t2", "p2", "b2"]),
        node("t2", "StaticText", "Estantería Roble"),
        node("p2", "StaticText", "26,90 €"),
        node("b2", "button", "Añadir", properties={"focusable": True}),
    ]
    return payload(tree)


def test_a_product_list_reads_as_name_price_button_with_refs():
    result = text_of(shop_page(), {"text": True})
    assert result["text"].split("\n") == [
        "# Lista “Favoritos”",
        "[link t1] Pokémon Caja Mew -> https://shop.test/mew/p", "49,99 €", "[button b1] Agotado temporalmente (disabled)",
        "Estantería Roble", "26,90 €", "[button b2] Añadir",
    ]
    assert "nodes" not in result and result["selection"] == {"kind": "text", "total_lines": 7, "matches": 7,
                                                              "offset": 0, "next_offset": None}
    assert result["tab"]["id"] == 17                       # snapshot identity stays


def test_query_and_paging_work_on_lines():
    result = text_of(shop_page(), {"text": True, "query": "€"})
    assert result["text"] == "49,99 €\n26,90 €" and result["selection"]["matches"] == 2
    first = text_of(shop_page(), {"text": True, "limit": 3})
    assert first["text"].count("\n") == 2 and first["selection"]["next_offset"] == 3
    rest = text_of(shop_page(), {"text": True, "offset": 3, "limit": 10})
    assert rest["text"].startswith("[button b1]") and rest["selection"]["next_offset"] is None


def test_out_of_order_lists_follow_the_tree():
    page = shop_page()
    nodes = page["result"]["nodes"]
    page["result"]["nodes"] = [nodes[0]] + list(reversed(nodes[1:]))
    assert text_of(page, {"text": True})["text"].split("\n")[0] == "# Lista “Favoritos”"


def test_the_schema_accepts_text_and_other_views_are_unchanged():
    assert validate_arguments({"action": "snapshot", "tab_id": 3, "view": {"text": True, "query": "€", "limit": 5}})
    flat = text_of(payload(), {"query": "Library"})
    assert flat["selection"]["kind"] == "flat" and flat["nodes"]


def test_visible_keeps_only_what_is_on_screen_and_keeps_the_viewport():
    page = shop_page()
    on_screen = {"root", "h", "row1", "t1", "t1s", "p1", "b1", "b1s"}
    for item in page["result"]["nodes"]:
        item["in_view"] = item["ref"] in on_screen
    page["result"]["viewport"] = {"x": 0, "y": 900, "width": 800, "height": 600, "page_width": 800, "page_height": 3000}
    result = text_of(page, {"text": True, "visible": True})
    assert result["text"].split("\n") == ["# Lista “Favoritos”", "[link t1] Pokémon Caja Mew -> https://shop.test/mew/p",
                                          "49,99 €", "[button b1] Agotado temporalmente (disabled)"]
    assert result["viewport"]["y"] == 900
    assert result["selection"]["off_screen_lines"] == 3 and result["selection"]["total_lines"] == 4
    everything = text_of(page, {"text": True})
    assert "Estantería Roble" in everything["text"] and "off_screen_lines" not in everything["selection"]
    flat = text_of(page, {"query": "€", "visible": True})
    assert [row["name"] for row in flat["nodes"]] == ["49,99 €"]
    assert validate_arguments({"action": "scroll", "tab_id": 3, "direction": "down", "view": {"text": True, "visible": True}})
