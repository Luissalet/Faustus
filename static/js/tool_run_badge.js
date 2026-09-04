/**
 * Where a tool call actually ran, shown on its card.
 *
 * `sandbox_exec` already puts `sandboxed`, `image`, `isolation`, `network` and
 * `duration_ms` in the result dict, and nothing painted them — so the one
 * place a person looks after running a command could not tell them whether it
 * had gone into a container or straight onto their machine.
 *
 * The rule for when to say nothing is the interesting part. A missing
 * `sandboxed` key means one of two things — an event recorded before this
 * existed, or a tool that never goes near the sandbox — and those are not the
 * same as "it ran on the host". Labelling them would be inventing a fact
 * about someone's history, so the badge stays silent unless the event itself
 * says where it ran. The workspace note carries the global state; this
 * carries the per-run one.
 */
export function runBadge(ev, { html = true } = {}) {
  if (!ev || typeof ev !== 'object') return '';

  if (ev.sandbox_refused === true) {
    return _wrap('refused · not run', 'refused', html);
  }
  if (ev.sandboxed !== true) return '';       // unknown is not "the host"

  const bits = [];
  bits.push(ev.isolation === 'container' ? 'container' : String(ev.isolation || 'sandboxed'));
  if (ev.image) bits.push(String(ev.image));
  bits.push(ev.network ? 'network' : 'no network');
  if (Number.isFinite(ev.duration_ms)) bits.push(`${(ev.duration_ms / 1000).toFixed(1)}s`);
  return _wrap(bits.join(' · '), 'sandboxed', html);
}

function _wrap(text, kind, html) {
  if (!html) return text;
  const safe = String(text).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]
  ));
  return `<span class="agent-thread-where is-${kind}" title="Where this command ran">${safe}</span>`;
}
