import { t } from '../../i18n';

/**
 * What the Workflows screen knows about node types: the palette, the config a
 * freshly added node starts with, how a branch reads on an edge, and which
 * nodes belong to a loop. Pure functions over the same opaque definition the
 * rest of the screen edits — the server (`WorkflowDefinition.parse`, preflight)
 * stays the only judge of whether it is valid.
 */

export interface RawNode {
  id: string;
  type: string;
  title?: string;
  needs?: string[];
  config?: Record<string, unknown>;
  branch?: Record<string, string[] | string>;
  [key: string]: unknown;
}

export type PaletteGroup = 'model' | 'flow' | 'effect';

export interface PaletteEntry {
  type: string;
  group: PaletteGroup;
  label: string;
  hint: string;
}

/** The node types a person can add by hand. Built in a function so `t()` runs
 *  after the locale is known. */
export function paletteEntries(): PaletteEntry[] {
  return [
    { type: 'agent', group: 'model', label: t('Agent'), hint: t('One agent turn: a prompt, the tools it may call, and an optional output schema.') },
    { type: 'classify', group: 'model', label: t('Classify'), hint: t('Route to one of several branches, with a confidence and a fallback.') },
    { type: 'extract', group: 'model', label: t('Extract'), hint: t('Pull structured fields out of a text, checked against a schema.') },
    { type: 'guard', group: 'model', label: t('Guard'), hint: t('Deterministic checks plus an optional model check; branches pass or fail.') },
    { type: 'loop', group: 'model', label: t('Loop'), hint: t('Repeat a few nodes, at most a fixed number of times, until a condition holds.') },
    { type: 'condition', group: 'flow', label: t('Condition'), hint: t('Compare two values and pass or fail.') },
    { type: 'human_approval', group: 'flow', label: t('Human approval'), hint: t('Pause until a person approves or denies.') },
    { type: 'skill', group: 'effect', label: t('Skill'), hint: t('Run an installed skill script.') },
    { type: 'artifact_store', group: 'effect', label: t('Store artifact'), hint: t('Save an upstream result as a file.') },
    { type: 'deliver', group: 'effect', label: t('Deliver'), hint: t('Send a result to a recipient.') },
  ];
}

export function groupLabel(group: PaletteGroup): string {
  return group === 'model' ? t('Model-driven') : group === 'flow' ? t('Flow') : t('Effects');
}

/** The config a new node starts with: only what the type needs to be
 *  well-formed, never a value that would pass for a real decision. */
export function defaultConfig(type: string): Record<string, unknown> {
  switch (type) {
    case 'agent':
      return { prompt: '', tools: [], max_rounds: 8 };
    case 'classify':
      return { text: '', labels: ['yes', 'no'], threshold: 0.7, fallback: 'no', on_uncertain: 'fallback' };
    case 'extract':
      return { text: '', schema: { type: 'object', properties: {}, required: [] } };
    case 'guard':
      return { text: '', checks: ['secrets'], on_unknown: 'fail' };
    case 'loop':
      return { body: [], budget: { max_iterations: 3, on_exhausted: 'pause' } };
    case 'condition':
      return { when: { left: '', op: 'truthy' } };
    case 'skill':
      return { skill: '' };
    default:
      return {};
  }
}

export function blankDefinition(): Record<string, unknown> {
  return {
    id: 'new-workflow', version: '1.0.0', title: t('New workflow'),
    nodes: [{ id: 'start', type: 'manual', title: t('Start'), needs: [], config: {} }],
  };
}

function nodesOf(definition: Record<string, unknown>): RawNode[] {
  return Array.isArray(definition.nodes) ? (definition.nodes as RawNode[]) : [];
}

export function uniqueNodeId(nodes: RawNode[], base: string): string {
  const taken = new Set(nodes.map((n) => n.id));
  if (!taken.has(base)) return base;
  for (let i = 2; i < 1000; i += 1) if (!taken.has(`${base}_${i}`)) return `${base}_${i}`;
  return `${base}_${Date.now()}`;
}

/** Add a node of `type`. It waits on the last node, when there is one, so a
 *  fresh node is reachable rather than a second root. A loop also gets one
 *  agent node as its body, because a loop with an empty body is refused. */
export function addNode(definition: Record<string, unknown>, type: string, after?: string | null): { definition: Record<string, unknown>; id: string } {
  const nodes = nodesOf(definition);
  const id = uniqueNodeId(nodes, type);
  const dep = after && nodes.some((n) => n.id === after) ? after : nodes[nodes.length - 1]?.id;
  const palette = paletteEntries().find((p) => p.type === type);
  const added: RawNode[] = [{ id, type, title: palette?.label ?? type, needs: dep ? [dep] : [], config: defaultConfig(type) }];
  if (type === 'loop') {
    const bodyId = uniqueNodeId([...nodes, ...added], `${id}_step`);
    added.push({ id: bodyId, type: 'agent', title: t('Loop step'), needs: [], config: defaultConfig('agent') });
    (added[0].config as Record<string, unknown>).body = [bodyId];
  }
  return { definition: { ...definition, nodes: [...nodes, ...added] }, id };
}

/** Remove a node and everything that pointed at it: other nodes stop waiting
 *  on it, branch gates on it go, and a loop stops listing it in its body. */
export function removeNode(definition: Record<string, unknown>, id: string): Record<string, unknown> {
  const nodes = nodesOf(definition)
    .filter((n) => n.id !== id)
    .map((n) => {
      const next: RawNode = { ...n, needs: (n.needs ?? []).filter((d) => d !== id) };
      if (n.branch && id in n.branch) {
        const branch = { ...n.branch };
        delete branch[id];
        if (Object.keys(branch).length) next.branch = branch;
        else delete next.branch;
      }
      if (n.type === 'loop' && Array.isArray(n.config?.body)) {
        next.config = { ...n.config, body: (n.config!.body as unknown[]).filter((b) => b !== id) };
      }
      return next;
    });
  return { ...definition, nodes };
}

export function updateNode(definition: Record<string, unknown>, id: string, patch: Partial<RawNode>): Record<string, unknown> {
  return { ...definition, nodes: nodesOf(definition).map((n) => (n.id === id ? { ...n, ...patch } : n)) };
}

/** Node id -> the loop that owns it, for every body node. */
export function loopOwners(nodes: RawNode[]): Record<string, string> {
  const owners: Record<string, string> = {};
  for (const n of nodes) {
    if (n.type !== 'loop' || !Array.isArray(n.config?.body)) continue;
    for (const b of n.config!.body as unknown[]) if (typeof b === 'string') owners[b] = n.id;
  }
  return owners;
}

/** The labels a node can branch on: a classify node's declared labels, or
 *  pass/fail for a guard. Empty for everything else. */
export function declaredLabels(node: RawNode | undefined): string[] {
  if (!node) return [];
  if (node.type === 'guard') return ['pass', 'fail'];
  if (node.type !== 'classify' || !Array.isArray(node.config?.labels)) return [];
  const out: string[] = [];
  for (const item of node.config!.labels as unknown[]) {
    if (typeof item === 'string' && item.trim()) out.push(item.trim());
    else if (item && typeof item === 'object' && typeof (item as { name?: unknown }).name === 'string') out.push(String((item as { name: string }).name).trim());
  }
  return out.filter(Boolean);
}

function labelText(source: RawNode | undefined, label: string): string {
  if (source?.type === 'guard') return label === 'pass' ? t('pass') : label === 'fail' ? t('fail') : label;
  const fallback = source?.type === 'classify' && typeof source.config?.fallback === 'string' ? source.config.fallback : '';
  return fallback && label === fallback ? t('{label} (uncertain)', { label }) : label;
}

/** `"dep->node"` -> what the edge says: the option (or pass/fail) the node is
 *  gated on, with the classify fallback marked as the uncertain route. */
export function edgeLabelsOf(nodes: RawNode[]): Record<string, string> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out: Record<string, string> = {};
  for (const n of nodes) {
    for (const [dep, raw] of Object.entries(n.branch ?? {})) {
      const labels = Array.isArray(raw) ? raw : [raw];
      const source = byId.get(dep);
      out[`${dep}->${n.id}`] = labels.map((l) => labelText(source, String(l))).join(' / ');
    }
  }
  return out;
}

/** Branch text for one node's row in the list view, e.g. `triage: billing`. */
export function branchSummary(node: RawNode): string {
  return Object.entries(node.branch ?? {})
    .map(([dep, raw]) => `${dep}: ${(Array.isArray(raw) ? raw : [raw]).join(' / ')}`)
    .join('; ');
}

/** The body-node edges: loop -> each of its body nodes, labelled as repeating. */
export function bodyEdgesOf(nodes: RawNode[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [bodyId, loopId] of Object.entries(loopOwners(nodes))) out[`${loopId}->${bodyId}`] = t('each pass');
  return out;
}
