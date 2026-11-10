---
name: web-to-clean-markdown
description: Turn a web page into clean, readable Markdown (main content only, headings, lists, tables, code and absolute links kept; navigation, ads and clutter removed) before reading, citing or saving it as a note, using Faustus's own readers. Use when the user wants an article saved to their notes, a page summarised without wasting tokens on its menus, or several pages read in one go.
version: 1.0.0
category: research
tags: [web, markdown, clean, article, reader, clipper, extraction, defuddle, reach_read, notas, artículo, guardar página]
status: published
source: imported
---

## When to Use

A raw page is mostly navigation, cookie banners, footers and scripts; reading it whole wastes the context window and pollutes citations. Extract the main content as structured Markdown first, then summarise, cite or save it. Faustus does this natively: `reach_read` (one page, structured Markdown with headings, lists, GitHub tables, fenced code and absolute links, from `src/html_markdown.py`), `reach_read_many` (several pages at once), `reach_extract` / `reach_recipe` (named fields from a page), and `web_fetch` for a quick look.

Adapted from kepano/obsidian-skills (MIT), skill defuddle, mapped onto Faustus's own extraction instead of an external CLI. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Read the main content.** Call `reach_read` with the URL. Check the title, the headings and the length that came back: if it is only a menu or a consent wall, try the page's print or AMP version, or `reach_browser` for a page that needs JavaScript.
2. **Keep metadata.** Note the title, author, site, publication date and canonical URL from the result; they go into the note's properties.
3. **Several pages.** `reach_read_many` for a list of URLs; never paste ten raw pages into the conversation.
4. **Save as a note when asked.** Write it with the `brain` tool (or create it in the user's vault) with frontmatter `source: <url>`, `author`, `published`, `clipped: <today>`, `tags`, then the cleaned Markdown. Keep the original headings; add a one-paragraph summary at the top only if the user wants one, marked as a summary.
5. **Cite from the clean text**, quoting short passages with the link.

## Pitfalls

- Summarising the raw HTML or a cookie banner and calling it the article.
- Dropping tables and code blocks by converting to plain text.
- Losing the URL and date: a saved article without its source cannot be checked later.
- Clipping a paywalled page partially and presenting it as complete — say what was cut.

## Verification

- The extracted text starts with the article title and has its real headings.
- Links in the saved note are absolute and open.
- The note's properties carry the source URL and the clip date.
