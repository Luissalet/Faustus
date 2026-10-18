"""CPU-only tests of the real guard AST, without app/settings/model imports."""
import ast
from collections import Counter
from pathlib import Path
import re
import unicodedata
import unittest


def load_guard(source_bytes=None):
    source = Path(__file__).resolve().parents[1] / "src/llm_core.py"
    names = {
        "_DEGENERATE_WORD_RE", "_DEGENERATE_RAW_REPEAT_MIN_CHARS",
        "_DEGENERATE_RAW_TAIL_WINDOW", "_GIBBERISH_WINDOW_CHARS_DEFAULT",
        "_GIBBERISH_THRESHOLD_DEFAULT", "_SCRIPT_GROUPS", "_CODE_LINE_HINT_RE",
        "_unexpected_script_fraction", "_script_class", "_token_soup_reason",
        "_vocabulary_collapse_reason", "DegenerateOutput", "_DegenerateStreamGuard",
        "_reasoning_loop_repeats",
    }
    nodes = []
    for node in ast.parse(source.read_bytes() if source_bytes is None else source_bytes).body:
        defined = ({node.name} if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else
                   {t.id for t in node.targets if isinstance(t, ast.Name)} if isinstance(node, ast.Assign) else set())
        if defined & names or any(name.startswith("_REASONING_") for name in defined):
            nodes.append(node)
    namespace = {"re": re, "unicodedata": unicodedata, "Counter": Counter}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    namespace["_gibberish_guard_settings"] = lambda: (
        namespace["_GIBBERISH_WINDOW_CHARS_DEFAULT"], namespace["_GIBBERISH_THRESHOLD_DEFAULT"])
    return namespace


ENV = load_guard()
Guard, Error = ENV["_DegenerateStreamGuard"], ENV["DegenerateOutput"]
ANCHOR = "Every completed revision preserves the exact binary contents of the earlier durable record."
CODE = "def verify_record_checksum_against_expected_header(value: bytes) -> bool:"


def feed(guard, text, size=7, method="check_reasoning"):
    for offset in range(0, len(text), size):
        getattr(guard, method)(text[offset:offset + size])


class ReasoningCycles(unittest.TestCase):
    def test_separated_contract_quotes_with_new_work(self):
        text = "".join(f"Revision {i} examines a different branch and records independent evidence.\n```\n{ANCHOR}\n```\n" for i in range(3))
        for size in (1, 7, 61, len(text)):
            with self.subTest(size=size):
                feed(Guard("CPU"), text, size)

    def test_short_progress_breaks_interval_equality(self):
        text = f"{ANCHOR}\nDone one.\n{ANCHOR}\nDone two.\n{ANCHOR}\n"
        for size in (1, 7, len(text)):
            with self.subTest(size=size):
                feed(Guard("CPU"), text, size)

    def test_old_new_drafts_share_a_quote_without_being_a_cycle(self):
        text = "".join(f"Draft {i}.\nold:\n{ANCHOR}\nnew:\nThe replacement introduces branch {i} with its own observed result.\n" for i in range(3))
        feed(Guard("CPU"), text)

    def test_three_consecutive_complete_intervals_raise(self):
        block = ANCHOR + "\nA short unchanged checkpoint.\n"
        for size in (1, 7, 113, len(block)):
            with self.subTest(size=size):
                guard = Guard("CPU")
                feed(guard, block * 2, size)
                with self.assertRaisesRegex(Error, "reasoning loop:"):
                    feed(guard, block, size)

    def test_same_long_sentence_three_times_still_raises(self):
        guard = Guard("CPU")
        feed(guard, (ANCHOR + "\n") * 2)
        with self.assertRaisesRegex(Error, "reasoning loop:"):
            feed(guard, ANCHOR + "\n")

    def test_only_recent_intervals_need_to_match(self):
        guard = Guard("CPU")
        feed(guard, ANCHOR + "\nAn earlier different step.\n")
        stable = ANCHOR + "\nA stable checkpoint.\n"
        feed(guard, stable * 2)
        with self.assertRaisesRegex(Error, "reasoning loop:"):
            feed(guard, stable)

    def test_different_interval_lengths_are_progress(self):
        text = f"{ANCHOR}\nOne.\n{ANCHOR}\nOne.\nTwo.\n{ANCHOR}\n"
        feed(Guard("CPU"), text)

    def test_historical_five_paragraph_cycle(self):
        # Generic own prose; no private reasoning or real contract quotation.
        paragraphs = [
            "First examine the input names and map their order to the corresponding destination entries.",
            "Then collect the previous checksums and compare each one with the newly serialized document.",
            "Next evaluate the ownership marker before opening any temporary staging output for this batch.",
            "After that publish the completed snapshot using the already prepared replacement operation.",
            "Finally report the independently measured results and describe the remaining recovery questions.",
        ]
        block = "\n\n".join(paragraphs) + "\n\n"
        guard = Guard("CPU")
        feed(guard, block * 2)
        with self.assertRaisesRegex(Error, "reasoning loop:"):
            feed(guard, block)

    def test_code_keeps_six_anchor_threshold(self):
        self.assertEqual(ENV["_reasoning_loop_repeats"](CODE), 6)
        block = "A short stable heading.\n" + CODE + "\n"
        for size in (1, 7, len(block)):
            with self.subTest(size=size):
                guard = Guard("CPU")
                feed(guard, block * 5, size)
                with self.assertRaisesRegex(Error, "reasoning loop:"):
                    feed(guard, block, size)

    def test_six_code_drafts_with_short_new_progress_are_not_a_cycle(self):
        text = "".join(f"Changed {i}.\n{CODE}\n" for i in range(6))
        feed(Guard("CPU"), text)

    def test_normalization_and_delimiters(self):
        guard = Guard("CPU")
        first = ANCHOR + "\r\n"
        feed(guard, first)
        feed(guard, "  " + ANCHOR.upper().replace(" ", "   ") + "\r\n")
        with self.assertRaisesRegex(Error, "reasoning loop:"):
            feed(guard, first)

    def test_content_channel_does_not_use_reasoning_cycles(self):
        feed(Guard("CPU"), (ANCHOR + "\n") * 5, method="check")

    def test_other_guards_keep_their_detection(self):
        cases = [("0" * 140, 1, "repeated unit"), ("loopword " * 40, 9, "repeated '")]
        for text, size, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(Error, reason):
                feed(Guard("CPU"), text, size)

    def test_history_is_bounded_and_does_not_join_discarded_progress(self):
        guard = Guard("CPU")
        feed(guard, ANCHOR + "\n")
        feed(guard, "".join(f"stage-{i:x}-advance\n" for i in range(5000)), size=71)
        feed(guard, ANCHOR + "\n")
        feed(guard, "A distinct final checkpoint.\n" + ANCHOR + "\n")
        self.assertLessEqual(len(guard._reasoning_units), ENV["_REASONING_LOOP_HISTORY_UNITS"])
        self.assertLessEqual(guard._reasoning_unit_chars, ENV["_REASONING_LOOP_HISTORY_CHARS"])
        self.assertEqual(guard._reasoning_unit_chars, sum(map(len, guard._reasoning_units)))
        self.assertTrue(all(len(p) <= 6 for p in guard._reasoning_positions.values()))

    def test_truncated_partial_units_are_barriers_not_equal_suffixes(self):
        suffix = " ".join(f"detail{i:04x}" for i in range(650))
        text = "".join(f"Different prefix {i} " + suffix + "\n" for i in range(3))
        guard = Guard("CPU")
        feed(guard, text, size=701)
        self.assertLessEqual(len(guard._sentence_buf), 4000)

    def test_oversize_units_are_conservative_for_whole_or_split_chunks(self):
        unit = " ".join(f"detail{i:04x}" for i in range(650)) + "\n"
        for size in (701, len(unit)):
            with self.subTest(size=size):
                guard = Guard("CPU")
                feed(guard, unit * 3, size)
                self.assertEqual(guard._reasoning_units, [])


if __name__ == "__main__":
    unittest.main()
