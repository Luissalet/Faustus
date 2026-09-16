/**
 * Shadow-root-only CSS for `<faustus-chat>`. Nothing here ever escapes the
 * shadow root, and no host page stylesheet (Bootstrap, Tailwind reset,
 * whatever the embedding app already loads) reaches in — that isolation is
 * the whole point of using a Shadow DOM instead of a plain custom element
 * with light-DOM children (A21: "styles routing auth and session state
 * remain isolated"). Two `<faustus-chat>` instances on the same page each
 * get their own shadow root and therefore their own copy of this sheet;
 * nothing here is shared module state.
 */
export const EMBED_STYLES = `
:host {
  --fc-bg: #ffffff;
  --fc-fg: #0f172a;
  --fc-muted: #64748b;
  --fc-border: #e2e8f0;
  --fc-accent: #2563eb;
  --fc-accent-fg: #ffffff;
  --fc-bubble-user: #2563eb;
  --fc-bubble-user-fg: #ffffff;
  --fc-bubble-agent: #f1f5f9;
  --fc-bubble-agent-fg: #0f172a;
  --fc-danger: #dc2626;
  --fc-radius: 12px;
  --fc-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;

  display: flex;
  flex-direction: column;
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  min-height: 320px;
  font-family: var(--fc-font);
  color: var(--fc-fg);
  background: var(--fc-bg);
  border: 1px solid var(--fc-border);
  border-radius: var(--fc-radius);
  overflow: hidden;
}

:host([data-fc-theme="dark"]) {
  --fc-bg: #0b1220;
  --fc-fg: #e2e8f0;
  --fc-muted: #94a3b8;
  --fc-border: #1e293b;
  --fc-accent: #60a5fa;
  --fc-accent-fg: #0b1220;
  --fc-bubble-user: #2563eb;
  --fc-bubble-user-fg: #ffffff;
  --fc-bubble-agent: #16213a;
  --fc-bubble-agent-fg: #e2e8f0;
  --fc-danger: #f87171;
}

* {
  box-sizing: border-box;
}

.fc-root {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.fc-header,
.fc-footer {
  flex: 0 0 auto;
  padding: 8px 12px;
  border-bottom: 1px solid var(--fc-border);
  font-size: 13px;
  color: var(--fc-muted);
}

.fc-footer {
  border-bottom: none;
  border-top: 1px solid var(--fc-border);
}

.fc-transcript {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.fc-empty {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--fc-muted);
  font-size: 13px;
  padding: 24px;
  text-align: center;
}

.fc-transcript[data-empty="true"] ~ .fc-empty-slot-host,
.fc-root[data-has-messages="true"] .fc-empty-slot-host {
  display: none;
}

.fc-msg {
  max-width: 88%;
  padding: 8px 12px;
  border-radius: var(--fc-radius);
  font-size: 14px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
}

.fc-msg[data-role="user"] {
  align-self: flex-end;
  background: var(--fc-bubble-user);
  color: var(--fc-bubble-user-fg);
  border-bottom-right-radius: 4px;
}

.fc-msg[data-role="agent"] {
  align-self: flex-start;
  background: var(--fc-bubble-agent);
  color: var(--fc-bubble-agent-fg);
  border-bottom-left-radius: 4px;
}

.fc-msg[data-role="error"] {
  align-self: stretch;
  background: transparent;
  border: 1px solid var(--fc-danger);
  color: var(--fc-danger);
}

.fc-tool {
  align-self: flex-start;
  max-width: 92%;
  border: 1px dashed var(--fc-border);
  border-radius: var(--fc-radius);
  padding: 6px 10px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--fc-muted);
  white-space: pre-wrap;
  word-break: break-word;
}

.fc-ask {
  align-self: stretch;
  border: 1px solid var(--fc-accent);
  border-radius: var(--fc-radius);
  padding: 10px 12px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.fc-ask-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.fc-btn {
  appearance: none;
  border: 1px solid var(--fc-border);
  background: var(--fc-bg);
  color: var(--fc-fg);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
}

.fc-btn:hover {
  border-color: var(--fc-accent);
}

.fc-btn:focus-visible,
.fc-composer textarea:focus-visible,
.fc-btn-send:focus-visible {
  outline: 2px solid var(--fc-accent);
  outline-offset: 1px;
}

.fc-btn-primary {
  background: var(--fc-accent);
  color: var(--fc-accent-fg);
  border-color: var(--fc-accent);
}

.fc-composer {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid var(--fc-border);
}

.fc-composer textarea {
  flex: 1 1 auto;
  resize: none;
  min-height: 36px;
  max-height: 140px;
  border: 1px solid var(--fc-border);
  border-radius: 8px;
  padding: 8px 10px;
  font: inherit;
  font-size: 14px;
  color: var(--fc-fg);
  background: var(--fc-bg);
}

.fc-btn-send {
  flex: 0 0 auto;
  appearance: none;
  border: none;
  background: var(--fc-accent);
  color: var(--fc-accent-fg);
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}

.fc-btn-send:disabled,
.fc-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.fc-status {
  flex: 0 0 auto;
  padding: 4px 12px;
  font-size: 12px;
  color: var(--fc-muted);
  min-height: 18px;
}
`;
