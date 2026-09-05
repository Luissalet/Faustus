/**
 * The hidden commands. `/flip`, `/roll`, `/8ball`, `/fortune`, `/odyssey`,
 * `/ascii`, `/cowsay`, `/wisdom`, `/uptime`, `/color`, `/matrix`.
 *
 * They were in the previous interface and they stay, because a tool you use
 * every day is allowed a couple of jokes. What changes is that they no
 * longer build HTML with inline styles and append it to the transcript:
 * here the answer is data and `screens/studio/Egg.tsx` draws it.
 */

export type EggKind = 'flip' | 'roll' | '8ball' | 'fortune' | 'odyssey' | 'ascii' | 'matrix' | 'cowsay' | 'wisdom' | 'uptime' | 'color';

export interface Egg {
  kind: EggKind;
  /** What to show, already decided: these are one-shot, not live. */
  values?: number[];
  text?: string;
  aside?: string;
  tone?: 'yes' | 'maybe' | 'no';
}

const ANSWERS: [string, Egg['tone']][] = [
  ['It is certain.', 'yes'],
  ['It is decidedly so.', 'yes'],
  ['Without a doubt.', 'yes'],
  ['Yes, definitely.', 'yes'],
  ['You may rely on it.', 'yes'],
  ['As I see it, yes.', 'yes'],
  ['Most likely.', 'yes'],
  ['Outlook good.', 'yes'],
  ['Signs point to yes.', 'yes'],
  ['Reply hazy, try again.', 'maybe'],
  ['Ask again later.', 'maybe'],
  ['Better not tell you now.', 'maybe'],
  ['Cannot predict now.', 'maybe'],
  ['Concentrate and ask again.', 'maybe'],
  ['Do not count on it.', 'no'],
  ['My reply is no.', 'no'],
  ['My sources say no.', 'no'],
  ['Outlook not so good.', 'no'],
  ['Very doubtful.', 'no'],
];

const FORTUNES = [
  'The bug you are looking for is in the file you already read twice.',
  'A quiet log is not the same as a working program.',
  'You will soon delete more code than you write. This is progress.',
  'The machine is doing exactly what you told it. That is the problem.',
  'Someone will thank you for the comment you almost did not write.',
  'Your next good idea arrives while you are doing the dishes.',
  'A test that has never failed has never been read.',
  'The second implementation is the one worth keeping.',
  'Today is a good day to read the error message all the way to the end.',
  'What you name a thing decides how you will think about it.',
];

const ODYSSEY = [
  'Sing to me of the man, Muse, the man of twists and turns.',
  'Of all creatures that breathe and move upon the earth, nothing is bred that is weaker than a man.',
  'There is a time for many words, and there is also a time for sleep.',
  'Even his griefs are a joy long after to one that remembers all that he wrought and endured.',
  'Nothing is sweeter in the end than one’s own country and one’s own parents.',
  'Bear up, my heart: you have endured worse than this.',
  'The blade itself incites to deeds of violence.',
  'A man who has been through bitter experiences and travelled far enjoys even his sufferings after a time.',
];

const WISDOM: [string, string][] = [
  ['The only way to do great work is to love what you do.', 'Steve Jobs'],
  ['Simplicity is the ultimate sophistication.', 'Leonardo da Vinci'],
  ['First, solve the problem. Then, write the code.', 'John Johnson'],
  ['Any fool can write code a computer can understand. Good programmers write code humans can understand.', 'Martin Fowler'],
  ['Talk is cheap. Show me the code.', 'Linus Torvalds'],
  ['Programs must be written for people to read, and only incidentally for machines to execute.', 'Abelson & Sussman'],
  ['The best error message is the one that never shows up.', 'Thomas Fuchs'],
  ['Code is like humour. When you have to explain it, it is bad.', 'Cory House'],
  ['Make it work, make it right, make it fast.', 'Kent Beck'],
  ['Perfection is achieved not when there is nothing more to add, but when there is nothing left to take away.', 'Antoine de Saint-Exupery'],
  ['It works on my machine.', 'Every developer ever'],
  ['There are only two hard things in computer science: cache invalidation, naming things, and off-by-one errors.', 'Anonymous'],
  ['To understand recursion, you must first understand recursion.', 'Anonymous'],
];

const pick = <T,>(list: T[]): T => list[Math.floor(Math.random() * list.length)];

/* ── The five-row banner font ── */

const GLYPHS: Record<string, string> = {
  A: '  #  \n # # \n#####\n#   #\n#   #', B: '#### \n#   #\n#### \n#   #\n#### ', C: ' ####\n#    \n#    \n#    \n ####',
  D: '#### \n#   #\n#   #\n#   #\n#### ', E: '#####\n#    \n###  \n#    \n#####', F: '#####\n#    \n###  \n#    \n#    ',
  G: ' ####\n#    \n# ###\n#   #\n ####', H: '#   #\n#   #\n#####\n#   #\n#   #', I: '#####\n  #  \n  #  \n  #  \n#####',
  J: '#####\n    #\n    #\n#   #\n ### ', K: '#   #\n#  # \n###  \n#  # \n#   #', L: '#    \n#    \n#    \n#    \n#####',
  M: '#   #\n## ##\n# # #\n#   #\n#   #', N: '#   #\n##  #\n# # #\n#  ##\n#   #', O: ' ### \n#   #\n#   #\n#   #\n ### ',
  P: '#### \n#   #\n#### \n#    \n#    ', Q: ' ### \n#   #\n# # #\n#  # \n ## #', R: '#### \n#   #\n#### \n#  # \n#   #',
  S: ' ####\n#    \n ### \n    #\n#### ', T: '#####\n  #  \n  #  \n  #  \n  #  ', U: '#   #\n#   #\n#   #\n#   #\n ### ',
  V: '#   #\n#   #\n#   #\n # # \n  #  ', W: '#   #\n#   #\n# # #\n## ##\n#   #', X: '#   #\n # # \n  #  \n # # \n#   #',
  Y: '#   #\n # # \n  #  \n  #  \n  #  ', Z: '#####\n   # \n  #  \n #   \n#####',
  '0': ' ### \n#  ##\n# # #\n##  #\n ### ', '1': '  #  \n ##  \n  #  \n  #  \n#####', '2': ' ### \n#   #\n  ## \n #   \n#####',
  '3': ' ### \n#   #\n  ## \n#   #\n ### ', '4': '#   #\n#   #\n#####\n    #\n    #', '5': '#####\n#    \n#### \n    #\n#### ',
  '6': ' ### \n#    \n#### \n#   #\n ### ', '7': '#####\n   # \n  #  \n #   \n#    ', '8': ' ### \n#   #\n ### \n#   #\n ### ',
  '9': ' ### \n#   #\n ####\n    #\n ### ', ' ': '     \n     \n     \n     \n     ',
  '!': '  #  \n  #  \n  #  \n     \n  #  ', '?': ' ### \n#   #\n  ## \n     \n  #  ',
};

export function banner(text: string): string {
  const source = (text || 'Faustus').slice(0, 14).toUpperCase();
  const glyphs = source.split('').map((c) => (GLYPHS[c] ?? GLYPHS['?']).split('\n'));
  return [0, 1, 2, 3, 4].map((row) => glyphs.map((g) => g[row] ?? '     ').join(' ')).join('\n');
}

export function cowsay(text: string): string {
  const line = (text || 'moo').replace(/\s+/g, ' ').slice(0, 60);
  const width = Math.max(line.length, 3);
  const top = ` ${'_'.repeat(width + 2)}`;
  const mid = `< ${line}${' '.repeat(width - line.length)} >`;
  const bottom = ` ${'-'.repeat(width + 2)}`;
  return [top, mid, bottom, '        \\   ^__^', '         \\  (oo)\\_______', '            (__)\\       )\\/\\', '                ||----w |', '                ||     ||'].join('\n');
}

/** `2d20`, `d6`, `20` or nothing. At most 20 dice of at most 1000 sides. */
export function roll(spec: string): { values: number[]; sides: number } {
  const raw = (spec || '6').toLowerCase().trim();
  const match = /^(\d+)?d(\d+)$/.exec(raw);
  const count = match ? Math.min(Math.max(Number.parseInt(match[1] || '1', 10), 1), 20) : 1;
  const sides = Math.min(Math.max(match ? Number.parseInt(match[2], 10) : Number.parseInt(raw, 10) || 6, 2), 1000);
  return { values: Array.from({ length: count }, () => Math.floor(Math.random() * sides) + 1), sides };
}

export function hexColour(input: string): string {
  const raw = (input || '').trim().replace(/^#/, '');
  if (/^[0-9a-f]{3}$/i.test(raw)) return `#${raw.split('').map((c) => c + c).join('')}`.toLowerCase();
  if (/^[0-9a-f]{6}$/i.test(raw)) return `#${raw.toLowerCase()}`;
  return `#${Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0')}`;
}

/** `3h 12m 04s`, from a start stamp. */
export function since(startedAt: number, now = Date.now()): string {
  const total = Math.max(0, Math.floor((now - startedAt) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h ? `${h}h ` : ''}${m}m ${String(s).padStart(2, '0')}s`;
}

/** Everything an egg needs, decided once so it never changes under you. */
export function egg(kind: EggKind, args: string, startedAt: number): Egg {
  switch (kind) {
    case 'flip': {
      const edge = Math.random() < 0.002;
      return { kind, text: edge ? '!' : Math.random() < 0.5 ? 'H' : 'T', aside: edge ? 'The coin landed on its edge.' : undefined };
    }
    case 'roll': {
      const { values, sides } = roll(args);
      return { kind, values, aside: `${values.length}d${sides}${values.length > 1 ? ` = ${values.reduce((a, b) => a + b, 0)}` : ''}` };
    }
    case '8ball': {
      const [answer, tone] = pick(ANSWERS);
      return { kind, text: answer, aside: args.trim(), tone };
    }
    case 'fortune':
      return { kind, text: pick(FORTUNES), values: Array.from({ length: 6 }, () => Math.floor(Math.random() * 90) + 10) };
    case 'odyssey':
      return { kind, text: pick(ODYSSEY), aside: 'Homer, The Odyssey' };
    case 'wisdom': {
      const [quote, who] = pick(WISDOM);
      return { kind, text: quote, aside: who };
    }
    case 'ascii':
      return { kind, text: banner(args) };
    case 'cowsay':
      return { kind, text: cowsay(args) };
    case 'uptime':
      return { kind, text: since(startedAt), values: [Math.min(100, ((Date.now() - startedAt) / 86400000) * 100)] };
    case 'color':
      return { kind, text: hexColour(args) };
    default:
      return { kind };
  }
}

/**
 * The rain of `/matrix`, on a canvas. It lives here and not in the
 * component because the colour literals of a canvas are pixels, not design
 * tokens, and pixels belong in a `.ts` (tests/test_studio_guards.py). It
 * reads what it can from the page's own tokens and falls back to these.
 */
export function rain(canvas: HTMLCanvasElement, reduced: boolean): () => void {
  const context = canvas.getContext('2d');
  if (!context) return () => undefined;
  const styles = getComputedStyle(canvas);
  const token = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback;
  const ink = token('--fs-success', '#00ff41');
  const bright = token('--fs-text-1', '#ffffff');
  const ground = token('--fs-canvas', '#000000');
  const glyphs = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#$%^&*';

  const settle = () => {
    context.fillStyle = 'rgba(0, 0, 0, 0.72)';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = ink;
    context.font = '14px monospace';
    context.fillText('Wake up, Neo...', canvas.width / 2 - 68, canvas.height / 2);
  };

  if (reduced) {
    context.fillStyle = ground;
    context.fillRect(0, 0, canvas.width, canvas.height);
    settle();
    return () => undefined;
  }

  context.fillStyle = ground;
  context.fillRect(0, 0, canvas.width, canvas.height);
  const columns = Math.floor(canvas.width / 12);
  const drops = Array.from({ length: columns }, () => Math.random() * -20);
  let frames = 0;
  let last = 0;
  let raf = 0;
  const step = (now: number) => {
    raf = requestAnimationFrame(step);
    if (now - last < 50) return;
    last = now;
    context.fillStyle = 'rgba(0, 0, 0, 0.08)';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.font = '12px monospace';
    for (let i = 0; i < columns; i += 1) {
      context.fillStyle = Math.random() > 0.82 ? bright : ink;
      context.fillText(glyphs[Math.floor(Math.random() * glyphs.length)], i * 12, drops[i] * 14);
      if (drops[i] * 14 > canvas.height && Math.random() > 0.97) drops[i] = 0;
      drops[i] += 0.5 + Math.random() * 0.5;
    }
    frames += 1;
    if (frames > 140) {
      cancelAnimationFrame(raf);
      raf = 0;
      settle();
    }
  };
  raf = requestAnimationFrame(step);
  return () => {
    if (raf) cancelAnimationFrame(raf);
  };
}
