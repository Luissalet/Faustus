// Home cards — automations pinned to the Home screen, showing their latest
// result: the `homeCards.ts` adapter exists with the four calls the
// contract defines, the "Your cards" block is mounted first on Home (before
// "Needs your decision"), unpinning is an inline two-step confirm (never
// window.confirm), and Automations has a per-task pin toggle plus a list
// badge for pinned tasks. Static source assertions only, no bundling — the
// same style `nearby-apps.check.mjs` uses.
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';

const adapter = readFileSync(new URL('../src/adapters/homeCards.ts', import.meta.url), 'utf8');
assert.match(adapter, /export interface HomeCard/, 'HomeCard type exists');
assert.match(adapter, /export async function listHomeCards/, 'listHomeCards() exists');
assert.match(adapter, /'\/api\/home\/cards'/, 'listHomeCards() calls GET /api/home/cards');
assert.match(adapter, /export async function pinHomeCard/, 'pinHomeCard() exists');
assert.match(adapter, /export async function unpinHomeCard/, 'unpinHomeCard() exists');
assert.match(adapter, /\/api\/home\/cards\/\$\{encodeURIComponent\(taskId\)\}/, 'unpinHomeCard() calls DELETE /api/home/cards/{task_id}');
assert.match(adapter, /export async function orderHomeCards/, 'orderHomeCards() exists');
assert.match(adapter, /'\/api\/home\/cards\/order'/, 'orderHomeCards() calls PUT /api/home/cards/order');
assert.doesNotMatch(adapter, /window\.confirm/, 'the adapter never uses window.confirm');

const home = readFileSync(new URL('../src/screens/Home.tsx', import.meta.url), 'utf8');
assert.match(home, /import \{ listHomeCards, unpinHomeCard, type HomeCard \} from '\.\.\/adapters\/homeCards'/, 'Home.tsx imports the homeCards adapter');
assert.match(home, /function HomeCards\(/, 'the HomeCards block component exists');
assert.match(home, /<HomeCards cards={cards} onChanged={reloadCards} \/>/, 'the HomeCards block is mounted');
assert.doesNotMatch(home, /window\.confirm/, 'Home.tsx never uses window.confirm');

// The cards block sits before "Needs your decision" in source order — Home
// renders top to bottom, so this is the same as "renders first".
const cardsAt = home.indexOf('<HomeCards');
const approvalsAt = home.indexOf("t('Needs your decision')");
assert.ok(cardsAt > 0 && approvalsAt > 0 && cardsAt < approvalsAt, 'Your cards renders before Needs your decision');

assert.match(home, /confirmUnpin/, 'unpin is a two-step inline confirm, not a single click');
assert.match(home, /fs-modes__confirm/, 'the unpin confirm reuses the shared inline-confirm style');

const automations = readFileSync(new URL('../src/screens/Automations.tsx', import.meta.url), 'utf8');
assert.match(automations, /import \{ listHomeCards, pinHomeCard, unpinHomeCard \} from '\.\.\/adapters\/homeCards'/, 'Automations.tsx imports the homeCards adapter');
assert.match(automations, /togglePin/, 'a pin toggle handler exists');
assert.match(automations, /pinnedIds\.has\(current\.id\) \? t\('Pinned to Home'\) : t\('Pin to Home'\)/, 'the detail pane shows a Pin to Home / Pinned to Home toggle');
assert.match(automations, /fs-au__pin-badge/, 'pinned tasks show a badge in the list row');
assert.match(automations, /pinned={pinnedIds\.has\(x\.id\)}/, 'the Row component receives the pinned state');

console.log('home-cards: ALL OK');
