/**
 * The one sentence that tells a user what the agent's shell can reach from
 * their folder — kept in its own module so it can be tested without the DOM.
 *
 * It used to be a hard-coded string: "shell commands are not sandboxed and can
 * reach outside it". With `agent_sandbox_execution` on that is false, and a
 * security note that is wrong in the *safe* direction is still wrong: it is
 * the line someone reads before deciding what to let the agent run.
 *
 * Three states, and the middle one is the one worth writing carefully. On, and
 * the backend is not there, does NOT mean "unsandboxed" — Faustus refuses the
 * command instead of running it on the host, and the note says so, because a
 * user who reads "not available" reasonably assumes the fallback is the old
 * behaviour.
 *
 * `state` may be null (not fetched yet, or the request failed). Null takes the
 * cautious branch by design: while we do not know, we do not reassure.
 */
export function shellNote(state, { html = true } = {}) {
  const b = (t) => (html ? `<strong>${t}</strong>` : t);

  if (!state || !state.enabled) {
    // Only this branch carries the caveat, and it is true here: with no
    // sandbox a workspace really is a scope and not a boundary.
    return `Shell commands start here but are ${b('not sandboxed')} and can reach outside it. `
         + 'A workspace scopes the tools; it is not a security boundary.';
  }

  if (!state.ready) {
    const why = state.detail || 'reason unknown';
    return `Shell commands are set to run ${b('sandboxed')}, but the sandbox is not available `
         + `right now (${why}) — they will be ${b('refused')}, not run here.`;
  }

  const net = state.network ? 'the network open' : 'no network';
  const image = state.image || 'the configured image';
  return `Shell commands run ${b('in a container')} (${image}) with only this folder `
       + `mounted and ${net}.`;
}
