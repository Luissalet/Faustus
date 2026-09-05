// The week as a timetable (studio/src/lib/agenda.ts): minutes to pixels,
// overlapping events into columns, snapping, and what a search matches.
// Bundled with esbuild on the fly; run by tests/test_studio_agenda_js.py,
// or by hand:
//   node studio/checks/agenda.check.mjs
import { pathToFileURL, fileURLToPath } from 'node:url';
import { dirname, resolve, join } from 'node:path';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { build } = await import(pathToFileURL(join(root, 'node_modules', 'esbuild', 'lib', 'main.js')).href);
const out = join(mkdtempSync(join(tmpdir(), 'fs-agenda-')), 'agenda.mjs');
await build({ entryPoints: [join(root, 'studio', 'src', 'lib', 'agenda.ts')], bundle: true, format: 'esm', platform: 'node', outfile: out, logLevel: 'silent' });
const a = await import(pathToFileURL(out).href);

let failed = 0;
const assert = (c, msg) => {
  if (!c) {
    failed += 1;
    console.error('FAIL:', msg);
  } else console.log('ok:', msg);
};

// ── Minutes, pixels and the clock ──
{
  assert(a.minutesOf(new Date(2026, 8, 5, 13, 45)) === 825, 'minutes from midnight');
  assert(a.snap(823) === 825 && a.snap(818) === 825 && a.snap(816) === 810, 'a drag lands on the quarter hour');
  assert(a.clampMinutes(-40) === 0 && a.clampMinutes(99999) === a.DAY_MINUTES, 'nothing escapes the day');
  assert(a.yFromMinutes(90, 46) === 69, 'an hour and a half at 46px per hour is 69px');
  assert(Math.round(a.minutesFromY(69, 46)) === 90, 'and back again');
  assert(a.minutesFromY(50, 0) === 0, 'an hour of zero height does not divide by zero');
  assert(a.clockOf(825) === '13:45' && a.clockOf(0) === '00:00', 'the clock is padded');
  assert(a.clockOf(a.DAY_MINUTES) === '00:00', 'midnight at the far end reads as midnight');
}

// ── The slice of an event that falls on one day ──
{
  const day = new Date(2026, 8, 5, 0, 0, 0, 0);
  const inside = a.spanOn(new Date(2026, 8, 5, 9, 0), new Date(2026, 8, 5, 10, 30), day);
  assert(inside.from === 540 && inside.to === 630, 'an event inside the day is itself');

  const fromYesterday = a.spanOn(new Date(2026, 8, 4, 22, 0), new Date(2026, 8, 5, 1, 0), day);
  assert(fromYesterday.from === 0 && fromYesterday.to === 60, 'one that started yesterday begins at midnight');

  const intoTomorrow = a.spanOn(new Date(2026, 8, 5, 23, 0), new Date(2026, 8, 6, 2, 0), day);
  assert(intoTomorrow.from === 1380 && intoTomorrow.to === a.DAY_MINUTES, 'one that runs over ends at midnight');

  const allDay = a.spanOn(new Date(2026, 8, 5, 0, 0), new Date(2026, 8, 6, 0, 0), day);
  assert(allDay.from === 0 && allDay.to === a.DAY_MINUTES, 'a whole day fills the column');

  const instant = a.spanOn(new Date(2026, 8, 5, 9, 0), new Date(2026, 8, 5, 9, 0), day);
  assert(instant.to - instant.from >= a.MIN_MINUTES, 'an event with no length is still visible');
}

// ── Overlaps share the width ──
{
  const span = (from, to) => ({ from, to });
  const placedFor = (items) => {
    const out = a.layout(items, (i) => i.span);
    const by = {};
    for (const p of out) by[p.item.id] = p;
    return by;
  };

  const alone = placedFor([{ id: 'a', span: span(540, 600) }]);
  assert(alone.a.column === 0 && alone.a.columns === 1, 'one event takes the whole column');

  const two = placedFor([
    { id: 'a', span: span(540, 660) },
    { id: 'b', span: span(600, 720) },
  ]);
  assert(two.a.columns === 2 && two.b.columns === 2, 'two that overlap are two halves');
  assert(two.a.column !== two.b.column, 'and they are not on top of each other');

  const backToBack = placedFor([
    { id: 'a', span: span(540, 600) },
    { id: 'b', span: span(600, 660) },
  ]);
  assert(backToBack.a.columns === 1 && backToBack.b.columns === 1, 'one ending as the next starts is not an overlap');

  const three = placedFor([
    { id: 'a', span: span(540, 720) },
    { id: 'b', span: span(560, 600) },
    { id: 'c', span: span(610, 660) },
  ]);
  assert(three.a.columns === 2 && three.b.columns === 2 && three.c.columns === 2, 'a long one plus two short ones is two columns wide');
  assert(three.b.column === three.c.column, 'the two short ones reuse the freed column');

  const groups = a.layout(
    [
      { id: 'a', span: span(0, 60) },
      { id: 'b', span: span(30, 90) },
      { id: 'c', span: span(600, 660) },
    ],
    (i) => i.span,
  );
  const c = groups.find((p) => p.item.id === 'c');
  assert(c.columns === 1, 'a later event is not narrowed by an earlier crowd');
  assert(groups.length === 3, 'nothing is dropped or duplicated');
}

// ── Dragging ──
{
  const noon = { from: 720, to: 780 };
  const moved = a.moveTo(noon, 545);
  assert(moved.from === 540 && moved.to === 600, 'a move keeps the length and snaps');
  assert(a.moveTo(noon, -100).from === 0, 'it cannot be dragged off the top');
  const bottom = a.moveTo(noon, a.DAY_MINUTES + 500);
  assert(bottom.to === a.DAY_MINUTES && bottom.to - bottom.from === 60, 'nor off the bottom, and it keeps its length');

  const grown = a.resizeTo(noon, 905);
  assert(grown.from === 720 && grown.to === 900, 'a resize keeps the start');
  assert(a.resizeTo(noon, 700).to === 720 + a.MIN_MINUTES, 'and never shrinks past the minimum');
  assert(a.resizeTo(noon, a.DAY_MINUTES + 300).to === a.DAY_MINUTES, 'nor past midnight');

  const down = a.spanFromDrag(540, 660);
  const up = a.spanFromDrag(660, 540);
  assert(down.from === up.from && down.to === up.to, 'a drag drawn upwards is the same span');
  const tap = a.spanFromDrag(540, 542);
  assert(tap.to - tap.from === a.MIN_MINUTES, 'a tap on empty space is a quarter of an hour, not nothing');
}

// ── What the search box matches ──
{
  const ev = { summary: 'Dentista', description: 'Llevar la radiografía', location: 'Calle Mayor 3', calendarName: 'Personal' };
  assert(a.matches(ev, 'dent'), 'the title');
  assert(a.matches(ev, 'RADIOGRAF'), 'the notes, ignoring case');
  assert(a.matches(ev, 'mayor'), 'the place');
  assert(a.matches(ev, 'personal'), 'the calendar it belongs to');
  assert(a.matches(ev, '  ') && a.matches(ev, ''), 'an empty box matches everything');
  assert(!a.matches(ev, 'banco'), 'and a word that is nowhere matches nothing');
  assert(!a.matches({ summary: undefined }, 'x'), 'an event with no fields does not throw');
}

console.log(failed ? `${failed} CHECK(S) FAILED` : 'ALL OK');
process.exit(failed ? 1 : 0);
