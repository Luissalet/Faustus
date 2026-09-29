"""Conservative exclusions for memory fallbacks without speaker resolution."""

import re


def unquoted_memory_text(text: str) -> str:
    """Keep the user's prose, excluding quoted examples and Markdown code.

    This is deliberately not a general Markdown or speaker parser. It prefers
    omitting an ambiguous claim to attributing another person's claim to a user.
    """
    # A transformation request supplies text to work on, not autobiographical
    # evidence. Omit the whole ambiguous message, including unquoted payloads.
    if re.match(
        r"\s*(?:(?:please|por favor)[,\s]+)?"
        r"(?:translate|rewrite|summarize|summarise|correct|proofread|"
        r"traduce|traducir|reescribe|resume|corrige)\b", text, re.I
    ):
        return ""
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?(?:^\s*\1\s*$|\Z)", "\n", text)
    text = re.sub(r"(?m)^\s*>[^\n]*", "", text)
    text = re.sub(r"(`+)[^\n]*?\1", " ", text)
    return re.sub(r'"[^"]*"|“[^”]*”|«[^»]*»|‘[^’]*’|(?<!\w)\'[^\'\n]+\'(?!\w)',
                  " ", text)
