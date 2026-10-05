/** Small deterministic tools; no eval, model call, persistence or exchange rates. */
export type QuickTool =
  | { kind: 'calculation'; expression: string; value?: number; error?: string }
  | { kind: 'conversion'; value: number; from: string; to: string; result: number }
  | { kind: 'timer'; seconds: number }
  | { kind: 'list'; items: string[] }
  | { kind: 'color'; hex: string; ink: string; contrast: number }
  | { kind: 'split'; cents: number; people: number; base: number; extra: number; currency: string };

export function calculate(expression: string): number {
  if (expression.length > 200 || !/^[\d.,+*/()\s-]+$/.test(expression)) throw Error('Invalid expression');
  const source = expression.replace(/,/g, '.');
  const tokens = source.match(/\d*\.?\d+|[+*/()-]/g) ?? [];
  if (tokens.join('') !== source.replace(/\s/g, '')) throw Error('Incomplete expression');
  let cursor = 0;
  function atom(depth: number): number {
    if (depth > 20) throw Error('Too many parentheses');
    const token = tokens[cursor++];
    if (token === '-' || token === '+') return (token === '-' ? -1 : 1) * atom(depth + 1);
    if (token === '(') {
      const result = sum(depth + 1);
      if (tokens[cursor++] !== ')') throw Error('Incomplete expression');
      return result;
    }
    if (!token || !/^\d*\.?\d+$/.test(token)) throw Error('Incomplete expression');
    return Number(token);
  }
  function product(depth: number): number {
    let result = atom(depth);
    while (tokens[cursor] === '*' || tokens[cursor] === '/') {
      const op = tokens[cursor++], value = atom(depth);
      if (op === '/' && value === 0) throw Error('Division by zero');
      result = op === '*' ? result * value : result / value;
    }
    return result;
  }
  function sum(depth: number): number {
    let result = product(depth);
    while (tokens[cursor] === '+' || tokens[cursor] === '-') {
      const op = tokens[cursor++], value = product(depth);
      result = op === '+' ? result + value : result - value;
    }
    return result;
  }
  const result = sum(0);
  if (cursor !== tokens.length || !Number.isFinite(result)) throw Error('Invalid expression');
  return result;
}

const units: Record<string, [string, number, number]> = {
  km: ['length', 1000, 0], m: ['length', 1, 0], cm: ['length', .01, 0], mm: ['length', .001, 0],
  mi: ['length', 1609.344, 0], millas: ['length', 1609.344, 0], miles: ['length', 1609.344, 0],
  in: ['length', .0254, 0], pulgadas: ['length', .0254, 0], inches: ['length', .0254, 0],
  ft: ['length', .3048, 0], pies: ['length', .3048, 0], feet: ['length', .3048, 0],
  kg: ['mass', 1, 0], g: ['mass', .001, 0], lb: ['mass', .45359237, 0], libras: ['mass', .45359237, 0],
  l: ['volume', 1, 0], ml: ['volume', .001, 0],
  c: ['temperature', 1, 0], f: ['temperature', 5 / 9, -32 * 5 / 9],
};

export function contrast(hex: string): { ink: string; contrast: number } {
  const rgb = hex.slice(1).match(/../g)!.map(v => parseInt(v, 16) / 255)
    .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
  const luminance = .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
  const black = (luminance + .05) / .05, white = 1.05 / (luminance + .05);
  return black >= white ? { ink: '#000000', contrast: black } : { ink: '#ffffff', contrast: white };
}

export function parseQuickTool(draft: string): QuickTool | null {
  if (draft.length > 2000) return null;
  const text = draft.trim();
  const calc = /^(?:=|calcula\s+|calculate\s+)([\d.,+*/()\s-]+)$/i.exec(text);
  if (calc) {
    try { return { kind: 'calculation', expression: calc[1].trim(), value: calculate(calc[1]) }; }
    catch (error) { return { kind: 'calculation', expression: calc[1].trim(), error: (error as Error).message }; }
  }
  const conversion = /^(?:(?:convierte|convert)\s+)?(-?\d+(?:[.,]\d+)?)\s*([a-z°]+)\s+(?:a|en|to)\s+([a-z°]+)$/i.exec(text);
  if (conversion) {
    const from = conversion[2].toLowerCase().replace('°', ''), to = conversion[3].toLowerCase().replace('°', '');
    const a = units[from], b = units[to], value = Number(conversion[1].replace(',', '.'));
    if (a && b && a[0] === b[0] && Number.isFinite(value)) {
      const result = (value * a[1] + a[2] - b[2]) / b[1];
      if (Number.isFinite(result)) return { kind: 'conversion', value, from: conversion[2], to: conversion[3], result };
    }
  }
  const timer = /^(?:temporizador|timer)\s+(\d+(?:[.,]\d+)?)\s*(s|segundos?|seconds?|m|min(?:utos?)?|minutes?|h|horas?|hours?)$/i.exec(text);
  if (timer) {
    const seconds = Number(timer[1].replace(',', '.')) * (/^h/i.test(timer[2]) ? 3600 : /^m/i.test(timer[2]) ? 60 : 1);
    if (seconds >= 1 && seconds <= 86400) return { kind: 'timer', seconds };
  }
  const list = /^(?:lista|checklist|list)\s*:\s*([\s\S]+)$/i.exec(text);
  if (list) {
    const items = list[1].split(/[;\n]/).map(v => v.replace(/^\s*[-*]\s*/, '').trim()).filter(Boolean);
    if (items.length >= 2 && items.length <= 20 && items.every(v => v.length <= 200)) return { kind: 'list', items };
  }
  const color = /^(?:color\s+)?(#[a-f\d]{6}|#[a-f\d]{3})$/i.exec(text);
  if (color) {
    const hex = color[1].length === 4 ? '#' + [...color[1].slice(1)].map(v => v + v).join('') : color[1];
    return { kind: 'color', hex, ...contrast(hex) };
  }
  const split = /^(?:divide|reparte|split)\s+(\d+(?:[.,]\d{1,2})?)\s*(€|\$|eur|usd)\s+(?:entre|between|among)\s+(\d+)\s*(?:personas?|people)?$/i.exec(text);
  if (split) {
    const [whole, fraction = ''] = split[1].replace(',', '.').split('.');
    const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
    const people = Number(split[3]);
    if (Number.isSafeInteger(cents) && cents <= 1e10 && people >= 2 && people <= 1000)
      return { kind: 'split', cents, people, base: Math.floor(cents / people), extra: cents % people, currency: split[2].toUpperCase() };
  }
  return null;
}
