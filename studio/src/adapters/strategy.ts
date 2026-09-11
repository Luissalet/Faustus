import { ApiError, getJson, responseReason } from './api';

/**
 * CMP-09/CMP-12: GET/PUT /api/strategy/profile, POST /api/strategy/preview,
 * GET /api/recipes, POST /api/recipes/from-run/{run_id}
 * (routes/strategy_routes.py, src/strategy_policy.py, src/recipes.py).
 *
 * The active profile/recipe are PERSISTED server-side per owner (optionally
 * scoped to one `session_id`) rather than travelling with `sendTurn` like
 * `autonomyPreset` does — this adapter reads/writes the persisted choice
 * directly and `Composer.tsx` reflects it via `loadStrategyProfile`, for the
 * picker itself. The backend ALSO emits a `strategy` SSE event at the start
 * of each turn (src/agent_loop.py) for the same decision, live: `chat.ts`
 * decodes it (`case 'strategy':`, landed in W3-A), `studio/src/screens/
 * studio/model.ts` folds it into `turn.strategy`, and `Transcript.tsx`'s
 * `StrategyLine` renders it per-turn — that live path no longer goes
 * through this adapter at all, by design (the two are independent: one is
 * "what did THIS turn decide", the other is "what is the persisted
 * default").
 */

export type StrategyProfile = 'fast' | 'balanced' | 'deep_review';
export const STRATEGY_PROFILES: StrategyProfile[] = ['fast', 'balanced', 'deep_review'];

export type StrategyMethod =
  | 'direct_edit' | 'plan_then_execute' | 'research' | 'specialised_review' | 'explore_alternatives';

export interface StrategyBudget {
  tokens: number;
  timeS: number;
  calls: number;
}

export interface Strategy {
  method: StrategyMethod;
  steps: string[];
  budget: StrategyBudget;
  modelsHint: string[];
  permissionsNeeded: string[];
  closeCriteria: string[];
  reasons: string[];
}

export interface Recipe {
  id: string;
  title: string;
  inputs: string[];
  steps: string[];
  tools: string[];
  successConditions: string[];
  optionalResources: string[];
  license: string | null;
  status: string;
}

interface RawStrategy {
  method?: string;
  steps?: string[];
  budget?: { tokens?: number; time_s?: number; calls?: number };
  models_hint?: string[];
  permissions_needed?: string[];
  close_criteria?: string[];
  reasons?: string[];
}

interface RawRecipe {
  id?: string;
  title?: string;
  inputs?: string[];
  steps?: string[];
  tools?: string[];
  success_conditions?: string[];
  optional_resources?: string[];
  license?: string | null;
  status?: string;
}

function strategyFrom(raw: RawStrategy): Strategy {
  return {
    method: (raw.method as StrategyMethod) || 'plan_then_execute',
    steps: Array.isArray(raw.steps) ? raw.steps : [],
    budget: {
      tokens: typeof raw.budget?.tokens === 'number' ? raw.budget.tokens : 0,
      timeS: typeof raw.budget?.time_s === 'number' ? raw.budget.time_s : 0,
      calls: typeof raw.budget?.calls === 'number' ? raw.budget.calls : 0,
    },
    modelsHint: Array.isArray(raw.models_hint) ? raw.models_hint : [],
    permissionsNeeded: Array.isArray(raw.permissions_needed) ? raw.permissions_needed : [],
    closeCriteria: Array.isArray(raw.close_criteria) ? raw.close_criteria : [],
    reasons: Array.isArray(raw.reasons) ? raw.reasons : [],
  };
}

function recipeFrom(raw: RawRecipe): Recipe {
  return {
    id: raw.id ?? '',
    title: raw.title ?? '',
    inputs: Array.isArray(raw.inputs) ? raw.inputs : [],
    steps: Array.isArray(raw.steps) ? raw.steps : [],
    tools: Array.isArray(raw.tools) ? raw.tools : [],
    successConditions: Array.isArray(raw.success_conditions) ? raw.success_conditions : [],
    optionalResources: Array.isArray(raw.optional_resources) ? raw.optional_resources : [],
    license: raw.license ?? null,
    status: raw.status ?? 'published',
  };
}

export interface ActiveStrategy {
  profile: StrategyProfile;
  recipeId: string | null;
}

export async function loadStrategyProfile(sessionId?: string | null): Promise<ActiveStrategy> {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  const body = await getJson<{ profile?: string; recipe_id?: string | null }>(`/api/strategy/profile${qs}`);
  const profile = STRATEGY_PROFILES.includes(body.profile as StrategyProfile)
    ? (body.profile as StrategyProfile)
    : 'balanced';
  return { profile, recipeId: body.recipe_id ?? null };
}

export async function saveStrategyProfile(
  update: { profile?: StrategyProfile; recipeId?: string | null },
  sessionId?: string | null,
): Promise<ActiveStrategy> {
  const qs = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
  const response = await fetch(`/api/strategy/profile${qs}`, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      profile: update.profile,
      recipe_id: update.recipeId === undefined ? undefined : (update.recipeId ?? ''),
    }),
  });
  if (!response.ok) {
    throw new ApiError(await responseReason(response, '/api/strategy/profile'), response.status);
  }
  const body = (await response.json()) as { profile?: string; recipe_id?: string | null };
  const profile = STRATEGY_PROFILES.includes(body.profile as StrategyProfile)
    ? (body.profile as StrategyProfile)
    : 'balanced';
  return { profile, recipeId: body.recipe_id ?? null };
}

export async function previewStrategy(
  taskText: string,
  profile: StrategyProfile,
  recipeId?: string | null,
): Promise<Strategy> {
  const response = await fetch('/api/strategy/preview', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_text: taskText, profile, recipe_id: recipeId || undefined }),
  });
  if (!response.ok) {
    throw new ApiError(await responseReason(response, '/api/strategy/preview'), response.status);
  }
  const body = (await response.json()) as { strategy?: RawStrategy };
  return strategyFrom(body.strategy ?? {});
}

export async function loadRecipes(): Promise<Recipe[]> {
  const body = await getJson<{ recipes?: RawRecipe[] }>('/api/recipes');
  return (body.recipes ?? []).map(recipeFrom);
}

export async function createRecipeFromRun(runId: string): Promise<Recipe> {
  const response = await fetch(`/api/recipes/from-run/${encodeURIComponent(runId)}`, {
    method: 'POST',
    credentials: 'same-origin',
  });
  if (!response.ok) {
    throw new ApiError(await responseReason(response, '/api/recipes/from-run'), response.status);
  }
  const body = (await response.json()) as { recipe?: RawRecipe };
  return recipeFrom(body.recipe ?? {});
}
