/**
 * Executed tool fences, out of the live bubble.
 *
 * A model asks for a tool by writing a fenced block — ```read_file
 * {"path": …}``` — the agent runs it and reports the result in the rail. The
 * fence itself has already done its job by then, so it is noise; the server
 * strips it from persisted history (`src/tool_parsing.py` builds its regex
 * from `TOOL_TAGS`). What streams live has not been through that, so without
 * this the fence sits in the bubble as a raw code block until a reload makes
 * it vanish — which reads like a rendering bug, because it is one.
 *
 * `TOOL_TAGS` is the single source: the tag list comes from `GET /api/tools`
 * at runtime, never from a copy here. A hand-kept mirror drifts the day
 * someone adds a tool, and the symptom (one tool's fences linger, the rest
 * are fine) is almost impossible to connect to its cause.
 *
 * `bash` and `python` are carved out on purpose: they are languages a person
 * may legitimately have asked the model to show.
 *
 * A fence only goes if its content parses as JSON. That is what separates a
 * tool call from a markdown block that happens to be labelled with the same
 * word, and it is why the check is a parse and not a shape guess.
 */

/** Languages that are never a tool call, whatever the tool list says. */
export const NOT_A_TOOL = new Set(['bash', 'python']);

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** null when there are no tags: a regex over an empty alternation matches everything. */
export function fenceRegex(tags: readonly string[]): RegExp | null {
  const usable = tags.filter((t) => t && !NOT_A_TOOL.has(t));
  if (!usable.length) return null;
  return new RegExp(
    '```(' + usable.map(escapeRe).join('|') + ')(?![\\w-])' +
      '[ \\t]*([\\[{][^\\n]*?)?[ \\t]*(?=\\r?\\n|```)' +
      '\\r?\\n?([\\s\\S]*?)```',
    'gi',
  );
}

/**
 * Remove the executed fences from `text`.
 *
 * The ordinary shape — the tag on the fence line, the arguments in the body —
 * is a tool call and goes: only a tool call is written that way, and the
 * result is already in the rail.
 *
 * Arguments ON the fence line are the ambiguous case, because that is also
 * how markdown carries metadata (```read_file {title="setup"}). There the
 * content has to parse as JSON before anything is removed. Unknown tags are
 * never touched. When in doubt, show what the model wrote.
 */
export function stripExecutedFences(text: string, re: RegExp | null): string {
  if (!re || !text) return text;
  re.lastIndex = 0;
  return text.replace(re, (match, _tag: string, inline: string | undefined, body: string | undefined) => {
    const args = (inline ?? '').trim();
    if (!args) return '';
    const rest = (body ?? '').trim();
    try {
      JSON.parse(rest ? `${args}\n${rest}` : args);
    } catch {
      return match;
    }
    return '';
  });
}

/**
 * The tag list, fetched once per page and shared.
 *
 * Until it resolves — normally well under a second — nothing is stripped,
 * which is the right way round: a fence shown for a moment is a blemish, a
 * paragraph wrongly deleted is a lie. If the fetch fails the promise settles
 * on an empty list and the live path simply does not strip; the persisted
 * path is unaffected, so a reload still renders clean.
 */
let pending: Promise<RegExp | null> | null = null;

export function toolFenceRegex(): Promise<RegExp | null> {
  if (!pending) {
    pending = fetch('/api/tools', { credentials: 'same-origin' })
      .then((r) => (r.ok ? (r.json() as Promise<{ tools?: { id?: string }[] }>) : { tools: [] }))
      .then((d) => fenceRegex((d.tools ?? []).map((t) => String(t?.id ?? '')).filter(Boolean)))
      .catch(() => null);
  }
  return pending;
}

/** For tests, and for a shell that reloads its tool set. */
export function resetToolFenceRegex(): void {
  pending = null;
}
