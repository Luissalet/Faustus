"""Research-service presentation over the shared, durable research runtime.

Only the rich Markdown presentation differs from chat. Persistence, confined
paths, cancellation, timeouts and citation continuation live in one implementation.
"""
from typing import Optional

from src.research_handler import ResearchHandler as SharedResearchHandler
from src.research_utils import is_low_quality, strip_thinking


class ResearchHandler(SharedResearchHandler):
    """Keep service source lists without maintaining a second research engine."""

    def start_research(self, session_id, query, llm_endpoint, llm_model,
                       max_time=None, llm_headers=None, **options):
        # Preserve the service's legacy sixth positional argument (headers).
        return super().start_research(
            session_id, query, llm_endpoint, llm_model, max_time=max_time,
            llm_headers=llm_headers, **options)

    def _format_completed_report(self, query, report, stats, elapsed, researcher):
        return self._format_research_report(
            query, report, stats, elapsed,
            findings=researcher.findings,
            evolving_report=researcher.evolving_report,
            analyzed_urls=getattr(researcher, "analyzed_urls", None),
        )

    def _format_research_report(
        self, query: str, full_report: str, stats: dict, elapsed: float,
        findings: Optional[list] = None, evolving_report: Optional[str] = None,
        analyzed_urls: Optional[list] = None,
    ) -> str:
        """Format research report with sources list and expandable raw findings."""
        full_report = strip_thinking(full_report)
        findings = [f for f in (findings or []) if isinstance(f, dict)]
        if analyzed_urls is not None:
            analyzed_urls = [f for f in analyzed_urls if isinstance(f, dict)]
        summary_lines = [
            f"**Duration:** {elapsed:.1f}s",
            f"**Rounds:** {stats.get('Rounds', stats.get('Findings', '?'))}",
            f"**Queries:** {stats.get('Queries', stats.get('Searches', '?'))}",
            f"**URLs Analyzed:** {stats.get('URLs', '?')}",
        ]
        for key in ('Citations', 'Claims cited'):
            if stats.get(key) is not None:
                summary_lines.append(f"**{key}:** {stats[key]}")
        summary_text = " | ".join(summary_lines)

        # Build sources list with clickable links. Keep the curated Sources
        # section filtered for citation quality, but also list every unique URL
        # the research run inspected so the "URLs Analyzed" count is auditable.
        sources_section = ""
        analyzed_urls_section = ""
        url_items = analyzed_urls if analyzed_urls is not None else findings
        if findings or url_items:
            seen_urls = set()
            source_lines = []
            analyzed_seen = set()
            analyzed_lines = []
            for f in findings or []:
                url = f.get("url", "")
                title = f.get("title", "") or url
                summary = f.get("summary", "") or f.get("evidence", "")
                if url and url not in seen_urls and not is_low_quality(summary):
                    seen_urls.add(url)
                    source_lines.append(f"- [{title}]({url})")
            for item in url_items or []:
                url = item.get("url", "")
                title = item.get("title", "") or url
                if url and url not in analyzed_seen:
                    analyzed_seen.add(url)
                    analyzed_lines.append(f"{len(analyzed_lines) + 1}. [{title}]({url})")
            if source_lines:
                sources_section = "\n### Sources\n\n" + "\n".join(source_lines) + "\n"
            if analyzed_lines:
                analyzed_urls_section = "\n### Analyzed URLs\n\n" + "\n".join(analyzed_lines) + "\n"

        # Build raw findings section (individual extractions per source)
        raw_findings_section = ""
        if findings:
            parts = []
            for i, f in enumerate(findings, 1):
                url = f.get("url", "")
                title = f.get("title", "") or "Untitled"
                summary = f.get("summary", "")
                evidence = f.get("evidence", "")
                content = summary if summary else (evidence[:2000] if evidence else "(no content)")
                parts.append(f"**{i}. [{title}]({url})**\n\n{content}")
            raw_findings_section = "\n\n".join(parts)

        # Build expandable collected info section
        collected_section = ""
        if evolving_report or raw_findings_section:
            collected_section = "\n<details>\n<summary><strong>Raw collected findings ({} sources)</strong></summary>\n\n".format(
                len(findings) if findings else 0
            )
            if raw_findings_section:
                collected_section += raw_findings_section + "\n"
            collected_section += "\n</details>\n"

        formatted = f"""---

## Research Summary

{summary_text}

---

{full_report}

{sources_section}
{analyzed_urls_section}
{collected_section}
---

**The AI has analyzed all research findings above. Ask me anything about: "{query}"**
"""
        return formatted
