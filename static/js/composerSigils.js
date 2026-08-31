// static/js/composerSigils.js
// The composer's leading-character shortcuts, as pure predicates so both the
// dispatcher and its tests can reach them without a DOM.
//
//   "/"  slash commands            → slashCommands.js
//   "@"  workspace file mentions   → fileMentions.js (activeQuery)
//   "#"  remember this rule        → here, handled by slashCommands._cmdRemember

/**
 * True when `str` is a "#" memory line: one line, exactly one leading "#",
 * and something after it.
 *
 * Two or more hashes stay a Markdown heading and a multi-line message is prose
 * the user meant to send — hijacking either would make "#" feel like a trap.
 */
export function isMemoryLine(str) {
  const s = String(str == null ? '' : str);
  if (s.includes('\n')) return false;
  return /^#(?!#)\s*\S/.test(s.trim());
}

export default { isMemoryLine };
