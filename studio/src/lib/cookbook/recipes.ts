/**
 * Per-engine install recipes for the Dependencies tab (ported data). Each
 * recipe carries a pip variant (into the configured venv) and, where one
 * exists, a docker variant (the official image).
 */

export type Variant = 'pip' | 'docker';

export interface Recipe {
  backend: string;
  label: string;
  match: (modelId: string) => boolean;
  variants: Partial<Record<Variant, { commands: string[] }>>;
}

const any = () => true;

const MLX_METALLIB = `MLX_METALLIB="$(python - <<'PY'\nimport pathlib, sys\ntry:\n    import mlx\nexcept Exception as exc:\n    raise SystemExit(f"mlx Python package is required for mlx.metallib: {exc}")\nroot = pathlib.Path(mlx.__file__).resolve().parent\nfor name in ("lib/mlx.metallib", "mlx.metallib", "lib/default.metallib", "default.metallib"):\n    path = root / name\n    if path.exists():\n        print(path)\n        break\nelse:\n    raise SystemExit(f"No MLX metallib found under {root}")\nPY\n)"; mkdir -p "$HOME/.local/bin" && cp "$MLX_METALLIB" "$HOME/.local/bin/mlx.metallib" && cp "$MLX_METALLIB" "$HOME/.local/bin/default.metallib"`;
const BRIDGE = 'BRIDGE_DIR="${ODYSSEUS_ROOT:-$PWD}/swift/odysseus-mlx-image-bridge"';
const BRIDGE_CHECK = `${BRIDGE}; test -d "$BRIDGE_DIR" || { echo "Run this from an Faustus checkout that includes swift/odysseus-mlx-image-bridge, or set ODYSSEUS_ROOT=/path/to/odysseus."; exit 1; }`;

export const RECIPES: Recipe[] = [
  { backend: 'vllm', label: 'MiniMax M2 / M2.7', match: (m) => /minimax[-_]?m\s?2(\.7)?/i.test(m || ''), variants: { pip: { commands: ['uv pip install -U vllm --torch-backend auto'] }, docker: { commands: ['docker pull vllm/vllm-openai:latest'] } } },
  { backend: 'vllm', label: 'Any vLLM model', match: any, variants: { pip: { commands: ['uv pip install -U vllm --torch-backend auto'] }, docker: { commands: ['docker pull vllm/vllm-openai:latest'] } } },
  { backend: 'sglang', label: 'Any SGLang model', match: any, variants: { pip: { commands: ['uv pip install -U "sglang[all]" --torch-backend auto'] }, docker: { commands: ['docker pull lmsysorg/sglang:latest'] } } },
  { backend: 'mlx_lm', label: 'Any MLX model', match: any, variants: { pip: { commands: ['python -m pip install -U mlx-lm'] } } },
  { backend: 'mflux', label: 'mflux-compatible MLX image models', match: any, variants: { pip: { commands: ['python -m pip install -U mflux fastapi uvicorn python-multipart'] } } },
  { backend: 'boogu_image_mlx', label: 'MLX image models (Boogu)', match: any, variants: { pip: { commands: ['python -m pip install -U git+https://github.com/xocialize/boogu-image-mlx.git fastapi uvicorn python-multipart pillow'] } } },
  { backend: 'mlx_vlm', label: 'MLX image models (HiDream)', match: any, variants: { pip: { commands: ['python -m pip install -U fastapi uvicorn python-multipart mlx mlx-vlm "transformers>=4.57.0,<6.0" huggingface_hub safetensors numpy pillow tqdm sentencepiece hf_transfer'] } } },
  {
    backend: 'mlx_lama_swift',
    label: 'MLX image editing (LaMa / MI-GAN)',
    match: any,
    variants: { pip: { commands: ['python -m pip install -U fastapi uvicorn python-multipart pillow huggingface_hub', BRIDGE_CHECK, `${BRIDGE}; cd "$BRIDGE_DIR" && swift build -c release --product odysseus-mlx-inpaint`, `${BRIDGE}; mkdir -p "$HOME/.local/bin" && cp "$BRIDGE_DIR/.build/release/odysseus-mlx-inpaint" "$HOME/.local/bin/odysseus-mlx-inpaint"`, MLX_METALLIB] } },
  },
  {
    backend: 'mlx_ddcolor_swift',
    label: 'MLX image editing (DDColor)',
    match: any,
    variants: { pip: { commands: ['python -m pip install -U fastapi uvicorn python-multipart pillow huggingface_hub', BRIDGE_CHECK, `${BRIDGE}; cd "$BRIDGE_DIR" && swift build -c release --product odysseus-mlx-colorize`, `${BRIDGE}; mkdir -p "$HOME/.local/bin" && cp "$BRIDGE_DIR/.build/release/odysseus-mlx-colorize" "$HOME/.local/bin/odysseus-mlx-colorize"`, MLX_METALLIB] } },
  },
  { backend: 'diffusers', label: 'Any Diffusers image model', match: any, variants: { pip: { commands: ['python -m pip install -U "diffusers[torch]" torchvision accelerate scipy python-multipart'] } } },
  { backend: 'krea_diffusers', label: 'Latest Diffusers from Git', match: any, variants: { pip: { commands: ['python -m pip install -U git+https://github.com/huggingface/diffusers.git torchvision accelerate scipy python-multipart'] } } },
  { backend: 'sam_mask', label: 'SAM object mask tools', match: any, variants: { pip: { commands: ['python -m pip install -U torch torchvision transformers accelerate pillow'] } } },
  { backend: 'llama_cpp', label: 'Any GGUF model', match: any, variants: { pip: { commands: ['CMAKE_ARGS="-DGGML_CUDA=on" uv pip install -U "llama-cpp-python[server]"'] }, docker: { commands: ['docker pull ghcr.io/ggml-org/llama.cpp:server-cuda'] } } },
];

export const RECIPE_BACKENDS = new Set(RECIPES.map((r) => r.backend));

export function recipesForBackend(backend: string): Recipe[] {
  return RECIPES.filter((r) => r.backend === backend);
}

export function pickRecipe(backend: string, modelId: string): Recipe | null {
  const candidates = recipesForBackend(backend);
  if (!candidates.length) return null;
  for (const r of candidates) {
    try {
      if (r.match(modelId)) return r;
    } catch {
      /* a recipe matcher must never break the list */
    }
  }
  return candidates[candidates.length - 1] ?? null;
}

export function recipeCommands(recipe: Recipe | null, variant: Variant): string[] {
  if (!recipe) return [];
  const v = recipe.variants[variant] ?? recipe.variants.pip;
  return v?.commands ?? [];
}
