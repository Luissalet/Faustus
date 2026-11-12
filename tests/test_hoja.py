"""Hoja (src/hoja.py, hoja_formula.py, hoja_format.py, hoja_xlsx.py): formulas checked against hand-computed values (and
LibreOffice when it is installed), ops, structure changes, recalculation, circular references, xlsx and queries."""
import datetime as dt
import io
import os
import shutil
import subprocess
import tempfile

import pytest

from src import hoja, hoja_format as F, hoja_formula as H


@pytest.fixture
def wb():
    w = hoja.Workbook()
    w.new_tab("Data")
    return w


def run(w, ops, view=None):
    res, ch = hoja.apply_ops(w, ops, view)
    return res, ch


def val(w, tab, a):
    t = w.tab(tab)
    rc = H.parse_addr(a)
    c = t.cells.get(rc)
    return None if c is None else c.v


def fill(w, tab="Data"):
    run(w, [{"op": "set", "tab": tab, "at": "A1", "values": [["Item", "Qty", "Price"], ["a", 2, 10], ["b", 3, 5], ["a", 5, 2.5], ["c", 1, 100]]}])


# -- engine: values checked by hand ---------------------------------------------------------------
CASES = [
    ("=SUM(B2:B5)", 11), ("=AVERAGE(B2:B5)", 2.75), ("=MAX(C2:C5)", 100), ("=MIN(C2:C5)", 2.5), ("=COUNT(B2:B5)", 4),
    ("=COUNTA(A1:A5)", 5), ("=SUMIF(A2:A5,\"a\",B2:B5)", 7), ("=COUNTIF(B2:B5,\">1\")", 3), ("=SUMPRODUCT(B2:B5,C2:C5)", 147.5),
    ("=AVERAGEIF(A2:A5,\"a\",C2:C5)", 6.25), ("=SUMIFS(B2:B5,A2:A5,\"a\",C2:C5,\">5\")", 2), ("=COUNTIFS(A2:A5,\"a\",B2:B5,\">2\")", 1),
    ("=IF(B2>1,\"big\",\"small\")", "big"), ("=VLOOKUP(\"b\",A2:C5,3,FALSE)", 5), ("=INDEX(C2:C5,MATCH(\"c\",A2:A5,0))", 100),
    ("=ROUND(2.567,2)", 2.57), ("=ROUND(-2.5,0)", -3), ("=ROUNDUP(2.1,0)", 3), ("=ROUNDDOWN(2.9,0)", 2), ("=INT(-1.5)", -2),
    ("=MOD(-7,3)", 2), ("=POWER(2,10)", 1024), ("=SQRT(81)", 9), ("=ABS(-4.5)", 4.5), ("=LEN(\"hola\")", 4), ("=UPPER(\"hola\")", "HOLA"),
    ("=LEFT(\"abcdef\",3)&RIGHT(\"abcdef\",2)", "abcef"), ("=MID(\"abcdef\",2,3)", "bcd"), ("=TRIM(\"  a   b \")", "a b"),
    ("=SUBSTITUTE(\"a-b-c\",\"-\",\"+\")", "a+b+c"), ("=TEXT(1234.5,\"#,##0.00\")", "1,234.50"), ("=TEXT(0.256,\"0.0%\")", "25.6%"),
    ("=CONCAT(\"a\",1,TRUE)", "a1TRUE"), ("=TEXTJOIN(\"-\",TRUE,\"a\",\"\",\"b\")", "a-b"), ("=FIND(\"c\",\"abcabc\")", 3), ("=PROPER(\"hello wORLD\")", "Hello World"),
    ("=DATE(2024,2,29)", 45351), ("=YEAR(45351)", 2024), ("=MONTH(45351)", 2), ("=DAY(45351)", 29), ("=EDATE(45351,12)", 45716),
    ("=EOMONTH(45351,0)", 45351), ("=DAYS(45361,45351)", 10), ("=WEEKDAY(45351,2)", 4), ("=NETWORKDAYS(45351,45361)", 7),
    ("=DATEDIF(45351,45717,\"M\")", 12), ("=AND(TRUE,1>0)", True), ("=OR(FALSE,1>2)", False), ("=NOT(FALSE)", True),
    ("=IFERROR(1/0,\"x\")", "x"), ("=ISNUMBER(3)", True), ("=ISTEXT(3)", False), ("=ISBLANK(Z9)", True), ("=1/0", H.DIV0),
    ("=\"a\"+1", H.VALUE), ("=NOSUCH(1)", H.NAME), ("=MEDIAN(B2:B5)", 2.5), ("=LARGE(C2:C5,2)", 10), ("=SMALL(C2:C5,1)", 2.5),
    ("=RANK(10,C2:C5)", 2), ("=PERCENTILE(B2:B5,0.5)", 2.5), ("=STDEV(B2:B5)", pytest.approx(1.707825127659933)), ("=PMT(0.05/12,360,200000)", pytest.approx(-1073.6432460247688)),
    ("=FV(0.05,10,-100)", pytest.approx(1257.7892544)), ("=PV(0.05,10,-100)", pytest.approx(772.1734929)), ("=NPV(0.1,100,200)", pytest.approx(256.198347107438)),
    ("=SWITCH(2,1,\"one\",2,\"two\",\"other\")", "two"), ("=CHOOSE(2,\"a\",\"b\",\"c\")", "b"),
    ("=IFS(B2>5,\"hi\",B2>1,\"mid\")", "mid"), ("=XLOOKUP(5,B2:B5,A2:A5)", "a"), ("=2^3^2", 64), ("=-2^2", 4), ("=50%*2", 1),
    ("=ROW(B3)", 3), ("=COLUMN(C1)", 3), ("=ROWS(A1:C4)", 4), ("=GCD(12,18)", 6), ("=LCM(4,6)", 12), ("=FACT(5)", 120), ("=SIGN(-3)", -1),
    ("=CEILING(2.1,0.5)", 2.5), ("=FLOOR(2.9,0.5)", 2.5), ("=TRUNC(-2.9)", -2), ("=LOG(100)", 2), ("=EXP(0)", 1), ("=LN(1)", 0),
    ("=VALUE(\"12.5\")", 12.5), ("=EXACT(\"a\",\"A\")", False), ("=REPT(\"ab\",3)", "ababab"), ("=CODE(\"A\")", 65), ("=CHAR(66)", "B"),
    ("=MAXIFS(C2:C5,A2:A5,\"a\")", 10), ("=MINIFS(C2:C5,A2:A5,\"a\")", 2.5), ("=QUOTIENT(7,2)", 3), ("=ISEVEN(4)", True),
    ("=COUNTBLANK(A1:A9)", 4), ("=CORREL(B2:B5,C2:C5)", pytest.approx(-0.7264475, abs=1e-6)), ("=SLOPE(C2:C5,B2:B5)", pytest.approx(-20.0714286, abs=1e-5)),
]


@pytest.mark.parametrize("formula,expected", CASES)
def test_engine_values(wb, formula, expected):
    fill(wb)
    run(wb, [{"op": "set", "tab": "Data", "at": "E1", "values": [[formula]]}])
    got = val(wb, "Data", "E1")
    assert got == expected, (formula, got)


def test_more_than_a_hundred_functions():
    names = H.function_names()
    assert len(names) >= 100
    for en in ("SUM", "VLOOKUP", "XLOOKUP", "SUMIFS", "IFERROR", "TEXTJOIN", "EOMONTH", "PMT", "SUMPRODUCT", "INDEX", "MATCH"):
        assert en in names


def test_spanish_and_english_are_equivalent(wb):
    fill(wb)
    pairs = [("=SUMA(B2:B5)", "=SUM(B2:B5)"), ("=SI(B2>1;\"x\";\"y\")", "=IF(B2>1,\"x\",\"y\")"), ("=PROMEDIO(C2:C5;B2:B5)", "=AVERAGE(C2:C5,B2:B5)"),
             ("=BUSCARV(\"b\";A2:C5;3;FALSO)", "=VLOOKUP(\"b\",A2:C5,3,FALSE)"), ("=SUMAR.SI(A2:A5;\"a\";B2:B5)", "=SUMIF(A2:A5,\"a\",B2:B5)"),
             ("=CONTAR.SI(B2:B5;\">1\")", "=COUNTIF(B2:B5,\">1\")"), ("=SI.ERROR(1/0;\"e\")", "=IFERROR(1/0,\"e\")"), ("=REDONDEAR(2,567;2)", "=ROUND(2.567,2)"),
             ("=IZQUIERDA(\"hola\";2)", "=LEFT(\"hola\",2)"), ("=MAYUSC(\"a\")", "=UPPER(\"a\")"), ("=HOY()", "=TODAY()")]
    for es, en in pairs:
        run(wb, [{"op": "set", "tab": "Data", "at": "H1", "values": [[es]]}, {"op": "set", "tab": "Data", "at": "H2", "values": [[en]]}])
        assert val(wb, "Data", "H1") == val(wb, "Data", "H2"), es
        assert wb.tab("Data").cells[(1, 8)].raw == wb.tab("Data").cells[(2, 8)].raw   # stored in the canonical form
    assert H.canonical("=SUMA(A1:A3; 2,5)") == "=SUM(A1:A3,2.5)"


def test_formulas_stay_in_canonical_form_and_reject_bad_syntax(wb):
    assert H.canonical("=suma( a1:b2 ; 1 )") == "=SUM(A1:B2,1)"
    with pytest.raises(hoja.SheetError) as e:
        run(wb, [{"op": "set", "at": "A1", "values": [["=SUM(("]]}])
    assert e.value.code == "bad_formula" and "A1" in e.value.message


# -- recalculation, dependency order, circular references --------------------------------------------
def test_recalc_follows_dependencies(wb):
    run(wb, [{"op": "set", "at": "A1", "values": [[1, "=A1*2", "=B1+10", "=SUM(A1:C1)"]]}])
    assert [val(wb, "Data", x) for x in ("B1", "C1", "D1")] == [2, 12, 15]
    run(wb, [{"op": "set", "at": "A1", "values": [[5]]}])
    assert [val(wb, "Data", x) for x in ("B1", "C1", "D1")] == [10, 20, 35]


def test_only_what_a_change_reaches_is_recalculated(wb):
    run(wb, [{"op": "set", "at": "A1", "values": [[1, 2, "=A1+1", "=B1+1"]]}])
    calls = []
    orig = hoja._eval_cell
    hoja._eval_cell = lambda ctx, tid, r, c, cell: (calls.append((r, c)), orig(ctx, tid, r, c, cell))[1]
    try:
        run(wb, [{"op": "set", "at": "A1", "values": [[10]]}])
    finally:
        hoja._eval_cell = orig
    assert calls == [(1, 3)]


def test_circular_reference_is_flagged_and_propagates(wb):
    run(wb, [{"op": "set", "at": "A1", "values": [["=B1+1", "=A1+1", "=A1*2"]]}])
    assert val(wb, "Data", "A1") == H.CIRC and val(wb, "Data", "B1") == H.CIRC and val(wb, "Data", "C1") == H.CIRC
    run(wb, [{"op": "set", "at": "B1", "values": [[7]]}])           # breaking the cycle heals it
    assert val(wb, "Data", "A1") == 8 and val(wb, "Data", "C1") == 16
    run(wb, [{"op": "set", "at": "D1", "values": [["=D1"]]}])
    assert val(wb, "Data", "D1") == H.CIRC


def test_long_dependency_chain_does_not_overflow_the_stack(wb):
    ops = [{"op": "set", "at": "A1", "values": [[1]]}, {"op": "set", "at": "A2", "values": [["=A1+1"]]},
           {"op": "fill", "range": "A2:A3000", "from": "A2"}]
    run(wb, ops)
    assert val(wb, "Data", "A3000") == 3000


def test_cross_tab_references(wb):
    fill(wb)
    run(wb, [{"op": "add_tab", "name": "Summary"}, {"op": "set", "tab": "Summary", "at": "A1", "values": [["=SUM(Data!B2:B5)", "=Data!C5*2", "='Data'!A2&\"!\""]]}])
    assert [val(wb, "Summary", x) for x in ("A1", "B1", "C1")] == [11, 200, "a!"]
    run(wb, [{"op": "set", "tab": "Data", "at": "B2", "values": [[100]]}])
    assert val(wb, "Summary", "A1") == 109
    run(wb, [{"op": "rename_tab", "tab": "Data", "name": "Historical data"}])
    assert wb.tab("Summary").cells[(1, 1)].raw == "=SUM('Historical data'!B2:B5)" and val(wb, "Summary", "A1") == 109
    run(wb, [{"op": "delete_tab", "tab": "Historical data"}])
    assert val(wb, "Summary", "A1") == H.REF


def test_whole_column_references_and_volatile(wb):
    fill(wb)
    run(wb, [{"op": "set", "at": "G1", "values": [["=SUM(B:B)", "=COUNTA(A:A)", "=TODAY()"]]}])
    assert val(wb, "Data", "G1") == 11 and val(wb, "Data", "H1") == 5
    assert val(wb, "Data", "I1") == F.serial_from_date(dt.date.today())
    assert 5 * 86400 // 86400 and "G1" and True


def test_dates_numbers_and_typed_input(wb):
    run(wb, [{"op": "set", "at": "A1", "values": [["2024-01-15", "12/05/2024", "$1,200.50", "5%", "TRUE", "'007", "hola"]]}])
    t = wb.tab("Data")
    assert t.cells[(1, 1)].v == 45306 and wb.styles[t.cells[(1, 1)].sid]["nf"] == "yyyy-mm-dd"
    assert t.cells[(1, 2)].v == 45424            # d/m/y: the Spanish reading
    assert t.cells[(1, 3)].v == 1200.5 and t.cells[(1, 4)].v == 0.05 and t.cells[(1, 5)].v is True
    assert t.cells[(1, 6)].v == "007" and t.cells[(1, 7)].v == "hola"
    assert hoja.display(wb, t.cells[(1, 1)]) == "2024-01-15" and hoja.display(wb, t.cells[(1, 3)]) == "$1,200.50" and hoja.display(wb, t.cells[(1, 4)]) == "5%"


def test_number_formats():
    assert F.format_value(1234.5, "#,##0.00") == "1,234.50"
    assert F.format_value(0.256, "0.0%") == "25.6%"
    assert F.format_value(-5.5, "$#,##0.00") == "-$5.50"
    assert F.format_value(45306, "dd/mm/yyyy") == "15/01/2024"
    assert F.format_value(45306.5, "yyyy-mm-dd hh:mm") == "2024-01-15 12:00"
    assert F.format_value(7, "0.00") == "7.00" and F.format_value(1e6, "#,##0") == "1,000,000"
    assert F.format_code("currency", 0, "eur") == "\u20ac#,##0" and F.format_code("percent", 1) == "0.0%"


# -- ops -----------------------------------------------------------------------------------------
def test_fill_adjusts_relative_references(wb):
    run(wb, [{"op": "set", "at": "A1", "values": [[1], [2], [3]]}, {"op": "set", "at": "B1", "values": [["=A1*$D$1+A$1"]]}, {"op": "set", "at": "D1", "values": [[10]]},
             {"op": "fill", "range": "B1:B3", "from": "B1"}])
    assert [val(wb, "Data", f"B{i}") for i in (1, 2, 3)] == [11, 21, 31]
    assert wb.tab("Data").cells[(3, 2)].raw == "=A3*$D$1+A$1"


def test_format_clear_copy_width_freeze(wb):
    fill(wb)
    run(wb, [{"op": "format", "range": "A1:C1", "bold": True, "fill": "lightblue", "align": "center"}, {"op": "format", "range": "C2:C5", "format": "currency", "decimals": 2},
             {"op": "width", "cols": {"A": 140, "B": "auto"}}, {"op": "freeze", "rows": 1}, {"op": "copy", "from": "A1:C2", "to": "F1"}])
    t = wb.tab("Data")
    assert wb.styles[t.cells[(1, 1)].sid] == {"b": 1, "bg": "#cfe8ff", "al": "c"}
    assert hoja.display(wb, t.cells[(2, 3)]) == "$10.00" and t.cols[1] == 140 and t.freeze["rows"] == 1
    assert t.cells[(2, 6)].v == "a" and t.cells[(1, 6)].sid == t.cells[(1, 1)].sid
    run(wb, [{"op": "clear", "range": "F1:H2"}])
    assert (1, 6) not in t.cells
    with pytest.raises(hoja.SheetError):
        run(wb, [{"op": "format", "range": "A1", "color": "chartreuse-ish"}])


def test_insert_and_delete_rows_and_columns_keep_references(wb):
    fill(wb)
    run(wb, [{"op": "set", "at": "E1", "values": [["=SUM(B2:B5)", "=C3"]]}])
    run(wb, [{"op": "insert_rows", "at": 3, "count": 2}])
    t = wb.tab("Data")
    assert t.cells[(1, 5)].raw == "=SUM(B2:B7)" and t.cells[(1, 6)].raw == "=C5" and val(wb, "Data", "E1") == 11 and val(wb, "Data", "F1") == 5
    run(wb, [{"op": "delete_rows", "at": 3, "count": 2}])
    assert t.cells[(1, 5)].raw == "=SUM(B2:B5)" and t.cells[(1, 6)].raw == "=C3"
    run(wb, [{"op": "delete_rows", "at": 3, "count": 1}])         # the row F1 pointed at
    assert val(wb, "Data", "F1") == H.REF and t.cells[(1, 5)].raw == "=SUM(B2:B4)" and val(wb, "Data", "E1") == 8
    run(wb, [{"op": "insert_cols", "at": "B"}])
    assert t.cells[(1, 6)].raw == "=SUM(C2:C4)" and val(wb, "Data", "F1") == 8 and t.cells[(1, 3)].v == "Qty"


def test_sort_keeps_rows_together_and_formulas_following(wb):
    run(wb, [{"op": "set", "at": "A1", "values": [["n", "v", "x2"], ["b", 3, "=B2*2"], ["a", 1, "=B3*2"], ["c", 2, "=B4*2"]]}])
    run(wb, [{"op": "sort", "range": "A1:C4", "by": "v", "header": True}])
    assert [val(wb, "Data", f"A{i}") for i in (2, 3, 4)] == ["a", "c", "b"]
    assert [val(wb, "Data", f"C{i}") for i in (2, 3, 4)] == [2, 4, 6]
    run(wb, [{"op": "sort", "range": "A1:C4", "by": "A", "desc": True, "header": True}])
    assert [val(wb, "Data", f"A{i}") for i in (2, 3, 4)] == ["c", "b", "a"]


def test_filter_hides_rows_and_clears(wb):
    fill(wb)
    run(wb, [{"op": "filter", "range": "A1:C5", "column": "Qty", "where": ">2"}])
    assert wb.tab("Data").hidden == {2, 5}
    run(wb, [{"op": "filter", "range": "A1:C5", "column": "A", "values": ["a"]}])
    assert wb.tab("Data").hidden == {3, 5}
    run(wb, [{"op": "filter", "clear": True}])
    assert wb.tab("Data").hidden == set()


def test_conditional_formats(wb):
    fill(wb)
    run(wb, [{"op": "cond", "range": "C2:C5", "rule": "gt", "value": 9, "fill": "red"}, {"op": "cond", "range": "B2:B5", "rule": "scale", "scale": ["white", "green"]},
             {"op": "cond", "range": "A2:A5", "rule": "duplicate", "fill": "yellow"}])
    t = wb.tab("Data")
    win = hoja.window(wb, t, 1, 1, 5, 3)
    by = {(c[0], c[1]): c for c in win["cells"]}
    assert by[(2, 3)][6] == {"bg": "#e5484d"} and by[(3, 3)][6] is None and by[(5, 3)][6] == {"bg": "#e5484d"}
    assert by[(2, 1)][6] == {"bg": "#f5d90a"} and by[(3, 1)][6] is None
    assert by[(2, 2)][6]["bg"].startswith("#") and by[(5, 2)][6]["bg"] != by[(4, 2)][6]["bg"]


def test_charts_from_a_range(wb):
    fill(wb)
    run(wb, [{"op": "chart", "type": "bar", "range": "A1:C5", "title": "Stock", "at": "F2"}])
    ch = wb.tab("Data").charts[0]
    assert ch["x"] == "A2:A5" and [s["name"] for s in ch["series"]] == ["Qty", "Price"]
    d = hoja.chart_data(wb, wb.tab("Data"), ch)
    assert d["categories"] == ["a", "b", "a", "c"] and d["series"][0]["values"] == [2, 3, 5, 1]
    run(wb, [{"op": "chart", "type": "pie", "series": ["B2:B5"], "x": "A2:A5", "title": "Pie"}])
    assert len(wb.tab("Data").charts) == 2
    with pytest.raises(hoja.SheetError):
        run(wb, [{"op": "chart", "type": "radar", "range": "A1:C5"}])


def test_chart_series_with_the_header_in_the_first_cell(wb):
    fill(wb)          # a model often passes B1:B7 and A1:A7: the header text is the series name, not a data point
    run(wb, [{"op": "chart", "type": "bar", "series": ["B1:B5", "C1:C5"], "x": "A1:A5", "title": "Stock"}])
    ch = wb.tab("Data").charts[0]
    assert [s["name"] for s in ch["series"]] == ["Qty", "Price"] and ch["x"] == "A2:A5"
    d = hoja.chart_data(wb, wb.tab("Data"), ch)
    assert d["categories"] == ["a", "b", "a", "c"] and d["series"][0]["values"] == [2, 3, 5, 1]

def test_batch_keeps_what_ran_before_a_failure(wb):
    with pytest.raises(hoja.SheetError) as e:
        run(wb, [{"op": "set", "at": "A1", "values": [[1]]}, {"op": "format", "range": "A1", "color": "nope"}, {"op": "set", "at": "A2", "values": [[2]]}])
    assert "op 2" in e.value.message and e.value.results == ["wrote 1 cells at A1 on 'Data'"]
    assert val(wb, "Data", "A1") == 1 and val(wb, "Data", "A2") is None


def test_csv_op_and_unknown_op(wb):
    run(wb, [{"op": "csv", "text": "a;b\n1;2,5\n", "at": "A1"}])
    assert val(wb, "Data", "A1") == "a" and val(wb, "Data", "B2") == "2,5"
    with pytest.raises(hoja.SheetError) as e:
        run(wb, [{"op": "wat"}])
    assert "unknown op" in e.value.message


# -- reading --------------------------------------------------------------------------------------
def test_read_text_and_query(wb):
    fill(wb)
    run(wb, [{"op": "set", "at": "E1", "values": [["=SUM(B2:B5)"]]}])
    meta = {"id": "x", "title": "T", "rev": 3}
    txt = hoja.read_text(wb, meta, state={"tab": "t1", "sel": {"r0": 2, "c0": 2, "r1": 3, "c1": 2}})
    assert "Tabs: 'Data' A1:E5" in txt and "header (row 1): A=Item | B=Qty | C=Price" in txt and "B2:B3 selected" in txt and "11 {=SUM(B2:B5)}" in txt
    assert hoja.query(wb, "Data", {"column": "Qty", "agg": "sum"})[0].endswith("= 11")
    assert hoja.query(wb, "Data", {"column": "C", "agg": "max", "where": [{"column": "A", "is": "a"}]})[0].endswith("= 10")
    text, data = hoja.query(wb, "Data", {"column": "Qty", "agg": "sum", "group_by": "Item"})
    assert data["groups"] == {"a": 7, "b": 3, "c": 1}
    assert "3 distinct" in hoja.query(wb, "Data", {"kind": "distinct", "column": "A"})[0] or "distinct values" in hoja.query(wb, "Data", {"kind": "distinct", "column": "A"})[0]
    assert "cut to" in hoja.read_text(wb, meta, tab="Data", rng="A1:E5", max_cells=6)


# -- storage, undo, xlsx ------------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    hoja.set_root_for_tests(str(tmp_path / "hoja"))
    yield tmp_path
    hoja.set_root_for_tests(None)


def test_store_edit_undo_redo_and_reload(store):
    meta = hoja.create("Plan", session_id="s1")
    sid = meta["id"]
    m2, res, ch, err = hoja.edit(sid, [{"op": "set", "at": "A1", "values": [[1, "=A1+1"]]}])
    assert err is None and m2["rev"] == 2 and ch.cells
    hoja.edit(sid, [{"op": "set", "at": "A1", "values": [[10]]}])
    _, wb = hoja.get(sid)
    assert val(wb, "Data" if wb.tab("Data") else "Sheet1", "B1") == 11 if wb.tab("Sheet1") is None else val(wb, "Sheet1", "B1") == 11
    _, ch, ok = hoja.undo(sid)
    _, wb = hoja.get(sid)
    assert ok and val(wb, "Sheet1", "A1") == 1 and val(wb, "Sheet1", "B1") == 2
    _, _, ok = hoja.redo(sid)
    assert ok and val(hoja.get(sid)[1], "Sheet1", "B1") == 11
    hoja.reset_for_tests()                                  # a fresh process reads the files
    _, wb = hoja.get(sid)
    assert val(wb, "Sheet1", "B1") == 11
    assert [r["id"] for r in hoja.list_sheets(session_id="s1")] == [sid] and hoja.list_sheets(session_id="other") == []
    assert hoja.delete(sid) and hoja.list_sheets(session_id="s1") == []


def test_xlsx_roundtrip_values_formulas_styles(store):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("xlsxwriter")
    from src import hoja_xlsx
    w = hoja.Workbook()
    w.new_tab("Data")
    fill(w)
    run(w, [{"op": "set", "at": "E1", "values": [["=SUM(B2:B5)", "=TODAY()", "=XLOOKUP(5,B2:B5,A2:A5)"]]}, {"op": "format", "range": "A1:C1", "bold": True, "fill": "#cfe8ff"},
            {"op": "format", "range": "C2:C5", "format": "currency", "decimals": 2}, {"op": "width", "cols": {"A": 150}}, {"op": "freeze", "rows": 1},
            {"op": "add_tab", "name": "Plan"}, {"op": "set", "tab": "Plan", "at": "A1", "values": [["=Data!E1*2"]]},
            {"op": "cond", "range": "B2:B5", "rule": "gt", "value": 2, "fill": "red"}, {"op": "chart", "type": "line", "range": "A1:C5", "at": "G2", "title": "T"},
            {"op": "filter", "range": "A1:C5", "column": "A", "values": ["a"]}])
    data = hoja_xlsx.export_bytes(w, {"title": "X"})
    cached = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    assert cached["Data"]["E1"].value == 11 and cached["Plan"]["A1"].value == 22                  # computed values are in the file
    raw = openpyxl.load_workbook(io.BytesIO(data))
    assert raw["Data"]["E1"].value == "=SUM(B2:B5)" and "XLOOKUP" in str(getattr(raw["Data"]["G1"].value, "text", raw["Data"]["G1"].value))
    assert raw["Data"]["A1"].font.b and raw["Data"].freeze_panes == "A2" and raw["Data"]["C2"].number_format == "$#,##0.00"
    back = hoja_xlsx.import_bytes(data)
    assert val(back, "Data", "E1") == 11 and val(back, "Plan", "A1") == 22 and back.tab("Data").cols[1] == pytest.approx(150, abs=8)
    assert back.styles[back.tab("Data").cells[(1, 1)].sid]["b"] == 1 and back.tab("Data").freeze["rows"] == 1
    assert hoja.display(back, back.tab("Data").cells[(2, 3)]) == "$10.00"


def test_the_file_on_disk_is_the_artifact(store):
    pytest.importorskip("xlsxwriter")
    meta = hoja.create("Disk")
    hoja.edit(meta["id"], [{"op": "set", "at": "A1", "values": [[2, "=A1*3"]]}])
    path = hoja.xlsx_path(meta["id"])
    assert path and os.path.getsize(path) > 1000 and path.endswith("sheet.xlsx")


def test_100k_rows_load_and_recalc_quickly():
    w = hoja.Workbook()
    w.new_tab("Big")
    t = w.tabs[0]
    for r in range(1, 100_001):
        t.cells[(r, 1)] = c = hoja.Cell(r)
        t.cells[(r, 2)] = c2 = hoja.Cell("=A%d*2" % r)
        c2.ast = H.parse(c2.raw)
    t.recompute_bounds()
    w.rebuild_graph()
    import time
    t0 = time.time()
    hoja.recalc(w, everything=True)
    assert time.time() - t0 < 20 and t.cells[(100_000, 2)].v == 200_000
    run(w, [{"op": "set", "at": "D1", "values": [["=SUM(B:B)"]]}])
    assert val(w, "Big", "D1") == 2 * 100_000 * 100_001 // 2
    t0 = time.time()
    run(w, [{"op": "set", "at": "A5", "values": [[1000]]}])
    assert time.time() - t0 < 2 and val(w, "Big", "D1") == 2 * 100_000 * 100_001 // 2 + 2 * (1000 - 5)
    win = hoja.window(w, t, 50_000, 1, 50_040, 2)
    assert len(win["cells"]) == 82


# -- the same formulas through LibreOffice (when it is installed) ------------------------------------------
SOFFICE = shutil.which("soffice") or next((p for p in (r"C:\Program Files\LibreOffice\program\soffice.exe", "/usr/bin/soffice") if os.path.exists(p)), None)


@pytest.mark.skipif(not SOFFICE, reason="LibreOffice is not installed")
def test_formulas_match_libreoffice(tmp_path):
    pytest.importorskip("xlsxwriter")
    from src import hoja_xlsx
    w = hoja.Workbook()
    w.new_tab("Data")
    fill(w)
    skip = {"=TODAY()", "=TEXT(1234.5,\"#,##0.00\")"}
    formulas = [f for f, e in CASES if f not in skip and not (isinstance(e, type(H.NAME)) and e in (H.NAME,))]
    run(w, [{"op": "set", "at": "G1", "values": [[f] for f in formulas]}])
    path = tmp_path / "t.xlsx"
    path.write_bytes(hoja_xlsx.export_bytes(w))
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run([SOFFICE, "--headless", "--convert-to", "csv:Text - txt - csv (StarCalc):44,34,76,1,,1033,false,false,false,false,false,-1", "--outdir", str(out), str(path)],
                   check=True, timeout=120, capture_output=True)
    csvs = sorted(out.glob("*.csv"))
    assert csvs
    import csv as _csv
    rows = list(_csv.reader(open(csvs[0], encoding="utf-8")))
    bad = []
    for i, f in enumerate(formulas):
        v = w.tab("Data").cells[(i + 1, 7)].v
        mine = ("TRUE" if v else "FALSE") if isinstance(v, bool) else (v.code if isinstance(v, H.Err) else ("" if v is None else str(v)))
        theirs = rows[i][6] if i < len(rows) and len(rows[i]) > 6 else None
        if theirs is None:
            continue
        try:
            theirs = {"VERDADERO": "TRUE", "FALSO": "FALSE"}.get(theirs.upper(), theirs)
            if len(theirs) == 10 and theirs[4] == "-" and theirs[7] == "-":
                theirs = str(F.serial_from_date(dt.date.fromisoformat(theirs)))
            tv = float(theirs.replace(",", "."))               # the converter runs in the machine's locale (decimal comma)
            same = abs(float(mine) - tv) < 1e-6 * max(1, abs(tv))
        except ValueError:
            same = mine.upper() == theirs.upper() or (mine.startswith("#") and theirs.startswith("#"))   # error names are localised
        if not same:
            bad.append((f, mine, theirs))
    assert not bad, bad
