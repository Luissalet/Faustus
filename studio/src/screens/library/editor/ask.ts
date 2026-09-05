/**
 * "Ask" — the natural-language command field. The legacy editor parsed a
 * sentence into one of its buttons; the same grammar lives here, with the
 * suggestions the field offers while typing.
 */
export interface AskSuggestion {
  label: string;
  insert: string;
  hint: string;
  aliases: string[];
}

export const ASK_SUGGESTIONS: AskSuggestion[] = [
  { label: 'Rotate 90°', insert: 'rotate 90', hint: 'Turn the image clockwise', aliases: ['ro', 'rotate', 'right', 'clockwise', 'turn'] },
  { label: 'Rotate left', insert: 'rotate left', hint: 'Turn the image counter-clockwise', aliases: ['rotate left', 'left', 'counter clockwise', 'ccw'] },
  { label: 'Rotate 180°', insert: 'rotate 180', hint: 'Flip the canvas upside down', aliases: ['rotate 180', 'upside down'] },
  { label: 'Flip horizontal', insert: 'flip horizontal', hint: 'Mirror left to right', aliases: ['flip', 'mirror', 'horizontal'] },
  { label: 'Flip vertical', insert: 'flip vertical', hint: 'Mirror top to bottom', aliases: ['flip vertical', 'vertical'] },
  { label: 'Remove background', insert: 'remove background', hint: 'Make the background transparent', aliases: ['remove bg', 'background', 'transparent', 'cut out'] },
  { label: 'Upscale', insert: 'upscale 2x', hint: 'Increase the resolution', aliases: ['upscale', 'bigger', 'larger', '2x', '4x'] },
  { label: 'Denoise', insert: 'denoise', hint: 'Reduce grain and noise', aliases: ['denoise', 'noise', 'grain', 'clean up'] },
  { label: 'Sharpen', insert: 'sharpen', hint: 'Make details crisper', aliases: ['sharpen', 'sharp', 'clearer', 'crisp', 'enhance'] },
  { label: 'Enhance face', insert: 'enhance face', hint: 'Restore portrait and skin detail', aliases: ['face', 'portrait', 'skin', 'selfie', 'restore'] },
  { label: 'Style edit', insert: 'style: ', hint: 'Redraw the whole picture from a prompt', aliases: ['style', 'paint', 'anime', 'photo', 'prompt'] },
];

export function matchSuggestions(query: string): AskSuggestion[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return ASK_SUGGESTIONS.map((item) => {
    const hay = [item.label, item.insert, ...item.aliases].map((v) => v.toLowerCase());
    const starts = hay.some((v) => v.startsWith(q));
    const contains = hay.some((v) => v.includes(q));
    if (!starts && !contains) return null;
    return { item, score: starts ? 0 : 1 };
  })
    .filter((x): x is { item: AskSuggestion; score: number } => !!x)
    .sort((a, b) => a.score - b.score || a.item.label.localeCompare(b.item.label))
    .slice(0, 6)
    .map((hit) => hit.item);
}

export type AskAction =
  | { kind: 'rotate'; deg: 90 | 180 | 270 }
  | { kind: 'flip'; axis: 'h' | 'v' }
  | { kind: 'rembg' }
  | { kind: 'upscale'; factor: number }
  | { kind: 'denoise' }
  | { kind: 'face' }
  | { kind: 'sharpen'; amount: number }
  | { kind: 'style'; prompt: string; strength: number };

/** Turn a sentence into an editor action. The order matters: specific before generic. */
export function parseAsk(prompt: string): AskAction | null {
  const p = prompt.trim().toLowerCase();
  if (!p) return null;
  if (/\brotate\b.*\b180\b|\bupside\s*down\b/.test(p)) return { kind: 'rotate', deg: 180 };
  if (/\brotate\b.*\b(left|ccw|counter)\b|\bturn\s+left\b/.test(p)) return { kind: 'rotate', deg: 270 };
  if (/\brotate\b|\bturn\s+right\b|\bclockwise\b/.test(p)) return { kind: 'rotate', deg: 90 };
  if (/\bflip\b.*\b(vertical|v)\b|\bmirror\b.*\b(vertical|v)\b/.test(p)) return { kind: 'flip', axis: 'v' };
  if (/\bflip\b|\bmirror\b/.test(p)) return { kind: 'flip', axis: 'h' };
  if (/\b(remove|erase|cut\s*out|transparent)\b.*\b(bg|background)\b|\b(bg|background)\b.*\b(remove|erase|transparent)\b/.test(p)) return { kind: 'rembg' };
  if (/\b(upscale|higher\s*res|increase\s*resolution|bigger|2x|4x)\b/.test(p)) return { kind: 'upscale', factor: /\b4x\b/.test(p) ? 4 : 2 };
  if (/\b(denoise|noise|grain|grainy|clean\s*up)\b/.test(p)) return { kind: 'denoise' };
  if (/\b(face|portrait|skin|selfie|restore)\b/.test(p)) return { kind: 'face' };
  if (/\b(sharpen|sharp|crisp|clearer|make it look better|enhance|improve|better)\b/.test(p)) return { kind: 'sharpen', amount: 65 };
  const stylePrompt = prompt.replace(/^\s*style\s*:\s*/i, '').trim();
  return { kind: 'style', prompt: stylePrompt || prompt.trim(), strength: /\b(subtle|slight|small)\b/.test(p) ? 35 : 55 };
}
