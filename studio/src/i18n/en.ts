/**
 * English overrides. Keys are the source strings, so English needs no
 * dictionary — except where one English form serves two Spanish ones (a
 * plural that English does not mark), which get a `#`-suffixed key here.
 */
export const en: Record<string, string> = {
  '{n} active#': '{n} active',
  '{n} archived.#': '{n} archived.',
  'All#f': 'All',
  '{n} pinned#': '{n} pinned',
  'Archive#folder': 'Archive',
  '{n} rejected#': '{n} rejected',
  '{n} published#': '{n} published',
  '{n} selected#': '{n} selected',
  'Published {n}#': 'Published {n}',
  '{n} removed#': '{n} removed',
  '{n} rewritten#': '{n} rewritten',
  // Lote 94 (OBJ-6): the project board reuses plain English words ("Open",
  // "Blocked", "Done", "Duplicate", "Blocks", "Link") that already exist as
  // `t()` keys elsewhere with a different sense (a verb, a different
  // gender) -- tagged so both get their own Spanish row without disturbing
  // each other, while English shows the same plain word either way.
  'Open#issue_status': 'Open',
  'Blocked#issue_status': 'Blocked',
  'Done#issue_status': 'Done',
  'Duplicate#issue_status': 'Duplicate',
  'Blocks#issue_link_kind': 'Blocks',
  'Link#issue_link_verb': 'Link',
  'The board is empty#issue_board': 'The board is empty',
  // B1 (CONTRATO_EXCURSOS.md): "N inherited" reads the same in English
  // whether N is 1 or many; Spanish needs "heredado"/"heredados".
  '{n} inherited#': '{n} inherited',
};
