/**
 * What the agent's `ui_control` tool asks the screen to do
 * (`src/ai_interaction.py`): the tool result carries `ui_event` plus its
 * fields on the `tool_output` stream event (`src/agent_loop.py`).
 *
 * Studio never read them: «pon el tema forest» answered "Theme changed to
 * 'forest'" and nothing changed; the same for open_panel, toggles, the mode,
 * the model and the email reply draft. `uiEventFrom` decodes them; Studio
 * (screens/Studio.tsx) applies them, with the helpers in shell/uiEvents.ts
 * for the parts that do not touch its own state.
 */
export interface UiEvent {
  kind: string;
  themeName?: string;
  colors?: Record<string, unknown>;
  bg?: Record<string, unknown>;
  toggleName?: string;
  state?: boolean;
  mode?: string;
  model?: string;
  endpointUrl?: string;
  panel?: string;
  uid?: string;
  folder?: string;
  accountId?: string;
  body?: string;
  selector?: string;
  label?: string;
}

const s = (v: unknown): string | undefined => (typeof v === 'string' && v ? v : undefined);
const obj = (v: unknown): Record<string, unknown> | undefined => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined);

export function uiEventFrom(raw: Record<string, unknown>): UiEvent | undefined {
  const kind = s(raw.ui_event);
  if (!kind) return undefined;
  return {
    kind,
    themeName: s(raw.theme_name),
    colors: obj(raw.colors),
    bg: obj(raw.bg),
    toggleName: s(raw.toggle_name),
    state: typeof raw.state === 'boolean' ? raw.state : undefined,
    mode: s(raw.mode),
    model: s(raw.model),
    endpointUrl: s(raw.endpoint_url),
    panel: s(raw.panel),
    uid: raw.uid === undefined || raw.uid === null ? undefined : String(raw.uid),
    folder: s(raw.folder),
    accountId: raw.account_id === undefined || raw.account_id === null ? undefined : String(raw.account_id),
    body: s(raw.body),
    selector: s(raw.selector),
    label: s(raw.label),
  };
}
