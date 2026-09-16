/**
 * `@faustus/embed` entry point. Importing this module registers
 * `<faustus-chat>` as a side effect (matches how every other Web
 * Component library ships — `import '@faustus/embed'` then use the tag in
 * markup) and also exports the class + helper for callers that want to
 * `new FaustusChatElement()` or guard against double-registration
 * themselves.
 */
import { FaustusChatElement, defineFaustusChat } from './faustus-chat.js';

export { FaustusChatElement, defineFaustusChat };
export type { FaustusChatTheme, TurnStartDetail, TurnEndDetail, EmbedErrorDetail } from './faustus-chat.js';

defineFaustusChat();
