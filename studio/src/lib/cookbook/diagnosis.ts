/**
 * What a failed launch's output means and what to do about it. Ported from
 * the previous interface's cookbook-diagnosis.js; the fixes are data (the
 * screen decides how to run them), so this stays pure and testable.
 */

export type Fix =
  | { label: string; kind: 'retry-replace'; flag: string; value: string; autofix?: boolean }
  | { label: string; kind: 'retry-prepend'; value: string }
  | { label: string; kind: 'retry-add'; flag: string; autofix?: boolean }
  | { label: string; kind: 'retry-remove'; flag: string }
  | { label: string; kind: 'env-fix'; value: string; autofix?: boolean }
  | { label: string; kind: 'field'; field: string; value: string | boolean }
  | { label: string; kind: 'copy'; text: string }
  | { label: string; kind: 'deps'; pkg: string }
  | { label: string; kind: 'edit'; overrides?: Record<string, string> }
  | { label: string; kind: 'cpu-edit' }
  | { label: string; kind: 'quick-cmd'; cmd: string }
  | { label: string; kind: 'pip-task'; name: string; args: string }
  | { label: string; kind: 'open-url'; url: (text: string) => string }
  | { label: string; kind: 'clear-gpus' }
  | { label: string; kind: 'clear-gpu-selection' }
  | { label: string; kind: 'focus'; field: string }
  | { label: string; kind: 'copy-output' };

export interface Diagnosis {
  message: string;
  suggestion?: string;
  fixes: Fix[];
}

interface Pattern extends Diagnosis {
  pattern?: RegExp;
  match?: (text: string) => boolean;
}

const HEALTHY = /Application startup complete|"(?:GET|POST)\s+\/v1\/[^"]+ HTTP\/[\d.]+"\s*2\d\d|Uvicorn running on|server is listening on https?:\/\//i;

const hfRepoFrom = (text: string) => {
  const m = text.match(/Access to model\s+(\S+)\s+is restricted/i) || text.match(/huggingface\.co\/([^\s/]+\/[^\s/]+)/i);
  return m ? `https://huggingface.co/${m[1]}` : 'https://huggingface.co/settings/gated-repos';
};

export const GPU_CLEANUP_COMMAND = `set -u
echo "[odysseus] Clearing GPU compute processes..."
if command -v nvidia-smi >/dev/null 2>&1; then
  pids="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null | tr -d " " | grep -E "^[0-9]+$" | sort -u)"
  if [ -z "$pids" ]; then
    echo "[odysseus] No NVIDIA compute processes found."
    exit 0
  fi
  echo "[odysseus] GPU PIDs: $pids"
  ps -fp $pids 2>/dev/null || true
  echo "[odysseus] Sending TERM..."
  kill -TERM $pids || true
  sleep 3
  alive=""
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then alive="$alive $pid"; fi
  done
  if [ -n "$alive" ]; then
    echo "[odysseus] Force killing remaining GPU PIDs:$alive"
    kill -KILL $alive || true
  fi
  sleep 1
  remaining="$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null | sed "/^$/d" || true)"
  if [ -n "$remaining" ]; then
    echo "[odysseus] GPU processes still remain:"
    echo "$remaining"
    exit 2
  fi
  echo "[odysseus] GPU cleanup complete. No NVIDIA compute processes remain."
else
  echo "[odysseus] nvidia-smi not found; falling back to common model-server process cleanup."
  pkill -TERM -f "sglang.launch_server|vllm|llama-server|text-generation-launcher|aphrodite" || true
  sleep 3
  pkill -KILL -f "sglang.launch_server|vllm|llama-server|text-generation-launcher|aphrodite" || true
  echo "[odysseus] Fallback cleanup complete."
fi`;

const tp = (label: string, n: string): Fix => ({ label, kind: 'retry-replace', flag: '--tensor-parallel-size', value: n });
const edit: Fix = { label: 'Edit serve', kind: 'edit' };
const deps = (pkg: string, label = 'Open Dependencies'): Fix => ({ label, kind: 'deps', pkg });
const copy = (label: string, text: string): Fix => ({ label, kind: 'copy', text });

export const ERROR_PATTERNS: Pattern[] = [
  {
    pattern: /tmux is required|tmux.*not found|tmux:\s*command not found|command not found:\s*tmux|No such file or directory:\s*['"]?tmux/i,
    message: 'tmux is missing on this server.',
    suggestion: 'Open Dependencies and install tmux on the selected server.',
    fixes: [deps('tmux', 'Open tmux dependency'), copy('Copy apt install', 'sudo apt install -y tmux'), copy('Copy pacman install', 'sudo pacman -S --needed tmux')],
  },
  {
    pattern: /Port \d+ is already serving|port is occupied by a different model|choose another port before launching/i,
    message: 'Serve port is already occupied by another model.',
    suggestion: 'Stop the old server or choose a different port before relaunching.',
    fixes: [edit, copy('Copy check command', 'curl http://127.0.0.1:PORT/v1/models')],
  },
  {
    pattern: /No available memory for the cache blocks|Available KV cache memory:.*-/i,
    message: 'No GPU memory left for KV cache after loading model.',
    fixes: [
      { label: 'Retry with GPU mem 0.95', kind: 'retry-replace', flag: '--gpu-memory-utilization', value: '0.95' },
      { label: 'Retry with context 2048', kind: 'retry-replace', flag: '--max-model-len', value: '2048' },
      tp('Retry with more GPUs (TP=8)', '8'),
    ],
  },
  {
    pattern: /warming up sampler|max_num_seqs.*gpu_memory_utilization/i,
    message: 'OOM during warmup. Lower GPU memory or max sequences.',
    fixes: [
      { label: 'Retry with GPU mem 0.80', kind: 'retry-replace', flag: '--gpu-memory-utilization', value: '0.80' },
      { label: 'Retry with --max-num-seqs 64', kind: 'retry-add', flag: '--max-num-seqs 64' },
      { label: 'Retry with --max-num-seqs 32', kind: 'retry-add', flag: '--max-num-seqs 32' },
    ],
  },
  {
    pattern: /Loaded weights leave no GPU memory for the KV cache under --mem-fraction-static|Raise --mem-fraction-static above/i,
    message: 'SGLang static memory fraction is too low for the loaded weights.',
    suggestion: 'Retry with --mem-fraction-static 0.80 so weights fit and KV cache can still allocate.',
    fixes: [
      { label: 'Retry mem 0.80', kind: 'retry-replace', flag: '--mem-fraction-static', value: '0.80' },
      { label: 'Retry mem 0.82', kind: 'retry-replace', flag: '--mem-fraction-static', value: '0.82' },
      edit,
    ],
  },
  {
    pattern: /get_paged_mqa_logits_metadata|deepseek_v4_backend\.py|paged_mqa_metadata\.cuh:113.*CUDA error:\s*invalid argument/i,
    message: 'SGLang DeepSeek-V4 attention metadata kernel failed on this GPU/runtime.',
    suggestion: 'Stop retrying graph/memory tweaks for this exact FP8 command. SGLang’s RTX PRO 6000 recipe uses the original deepseek-ai/DeepSeek-V4-Flash checkpoint with --moe-runner-backend marlin, not the converted sgl-project FP8 checkpoint. Try that recipe/checkpoint, official SGLang container/nightly, or supported Hopper/Blackwell hardware.',
    fixes: [edit, { label: 'Copy error', kind: 'copy-output' }],
  },
  {
    pattern: /Capture cuda graph failed|cuda graph failed|paged_mqa_metadata|cuda-graph-backend-decode|cuda-graph-max-bs-decode|CUDA error:\s*invalid argument/i,
    message: 'SGLang failed while capturing decode CUDA graphs.',
    suggestion: 'Disable SGLang decode CUDA graph for this launch. DeepSeek-V4 is reaching graph capture, but this kernel is failing on the target hardware.',
    fixes: [
      { label: 'Disable decode graph', kind: 'retry-replace', flag: '--cuda-graph-backend-decode', value: 'disabled' },
      { label: 'Retry mem 0.80', kind: 'retry-replace', flag: '--mem-fraction-static', value: '0.80' },
      edit,
    ],
  },
  {
    pattern: /CUDA out of memory|torch\.cuda\.OutOfMemoryError|CUDA error: out of memory/i,
    message: 'GPU ran out of memory. Try more GPUs (higher TP) or lower context.',
    fixes: [
      tp('Retry with TP=2', '2'),
      tp('Retry with TP=4', '4'),
      { label: 'Retry with GPU mem 0.80', kind: 'retry-replace', flag: '--gpu-memory-utilization', value: '0.80' },
      { label: 'Retry with context 4096', kind: 'retry-replace', flag: '--max-model-len', value: '4096' },
      { label: 'Retry with --enforce-eager', kind: 'retry-add', flag: '--enforce-eager' },
    ],
  },
  {
    pattern: /not divisible by weight quantization|quantization block/i,
    message: 'FP8 MoE quantization is incompatible with this tensor-parallel split.',
    suggestion: 'Retry with a lower tensor-parallel size, such as TP=4 or TP=2. If it still fails, use a non-FP8/GGUF version of the model.',
    fixes: [tp('Retry with TP=4', '4'), tp('Retry with TP=2', '2'), edit],
  },
  {
    pattern: /There is no module or parameter named ['"]lm_head\.input_scale['"]|lm_head\.input_scale|weight_scale_2/i,
    message: 'vLLM cannot load this ModelOpt LM-head quantized checkpoint with the current runtime.',
    suggestion: 'Upgrade vLLM through the environment that provides this CLI (package manager, venv, Docker image, or source checkout), or choose a compatible checkpoint.',
    fixes: [deps('vllm'), copy('Copy upgrade hint', 'Upgrade the vLLM environment that provides the selected vllm CLI, or use a compatible checkpoint. Do not assume Faustus owns PATH/system/source/Docker installs.')],
  },
  {
    pattern: /not divisib|must be divisible|attention heads.*divisible/i,
    message: 'Tensor parallel size incompatible with model dimensions.',
    fixes: [tp('Retry with TP=1', '1'), tp('Retry with TP=2', '2'), tp('Retry with TP=4', '4')],
  },
  {
    pattern: /Too large swap space|swap space.*total CPU memory/i,
    message: 'Swap space too large for available CPU memory.',
    fixes: [
      { label: 'Retry without swap', kind: 'retry-remove', flag: '--swap-space' },
      { label: 'Retry with swap 1', kind: 'retry-replace', flag: '--swap-space', value: '1' },
    ],
  },
  {
    pattern: /swap space|not enough.*memory.*cpu|Cannot allocate memory/i,
    message: 'Not enough CPU RAM or swap space.',
    fixes: [
      { label: 'Retry without swap', kind: 'retry-remove', flag: '--swap-space' },
      { label: 'Lower max context to 4096', kind: 'field', field: 'ctx', value: '4096' },
    ],
  },
  {
    pattern: /unrecognized arguments:\s*--swap-space/i,
    message: '--swap-space was removed in newer vLLM versions. Remove it from the command.',
    fixes: [{ label: 'Retry without swap', kind: 'retry-remove', flag: '--swap-space' }],
  },
  {
    pattern: /Address already in use|bind.*address.*in use/i,
    message: 'Port is already in use. Another server may be running.',
    fixes: [
      { label: 'Kill existing vLLM', kind: 'quick-cmd', cmd: 'pkill -f vllm' },
      { label: 'Use port 8001', kind: 'field', field: 'port', value: '8001' },
    ],
  },
  {
    pattern: /No CUDA GPUs are available|no GPU.*found|CUDA_VISIBLE_DEVICES.*invalid/i,
    message: 'No GPUs visible. Check your GPU selection or driver.',
    fixes: [{ label: 'Clear GPU selection (use all)', kind: 'clear-gpu-selection' }],
  },
  {
    pattern: /403 Forbidden|401 Unauthorized|Access to model.*is restricted|gated repo|not in the authorized list|awaiting a review/i,
    message: 'Gated model. Your HF token IS being sent — but its account must be granted access first: open the model page, accept the license, and wait for approval (Meta models can take a while).',
    fixes: [
      { label: 'Request access on HF', kind: 'open-url', url: hfRepoFrom },
      { label: 'Check HF Token', kind: 'focus', field: 'hf_token' },
    ],
  },
  {
    pattern: /Weights for this component appear to be missing|load the component before passing/i,
    message: 'Single-file checkpoint needs a base model for missing components (text encoder, VAE). The base model may be gated — accept the license and set your HF token.',
    fixes: [
      {
        label: 'Request access to base model',
        kind: 'open-url',
        url: (text) => {
          const gated = text.match(/Access to model\s+(\S+)\s+is restricted/i);
          const base = text.match(/config=([^\s,)]+)/i);
          const model = text.match(/load model from\s+(\S+)/i);
          const repo = (gated && gated[1]) || (base && base[1]) || (model && model[1].replace(/[.]$/, ''));
          return repo ? `https://huggingface.co/${repo}` : 'https://huggingface.co/settings/gated-repos';
        },
      },
      { label: 'Check HF Token', kind: 'focus', field: 'hf_token' },
    ],
  },
  {
    pattern: /OmniGen2Pipeline|module diffusers has no attribute .*Pipeline|custom_pipeline=.*failed/i,
    message: 'This image model uses a custom Diffusers pipeline that your launch environment does not know yet.',
    fixes: [deps('diffusers', 'Update image dependencies'), { label: 'Copy diagnosis', kind: 'copy-output' }],
  },
  {
    pattern: /Entry Not Found.*model_index\.json|Could not load model.*Check diffusers/i,
    message: 'Single-file model may need an explicit base config. Add --single-file-config <repo_or_path> if the checkpoint is missing components.',
    fixes: [
      { label: 'Request access to base model', kind: 'open-url', url: hfRepoFrom },
      { label: 'Check HF Token', kind: 'focus', field: 'hf_token' },
    ],
  },
  {
    pattern: /does not appear to have a file named|not a valid model|No such file or directory.*model/i,
    message: 'Model path or ID not found.',
    fixes: [{ label: 'Check model name', kind: 'focus', field: 'model' }],
  },
  {
    pattern: /NCCL error|ncclSystemError|ncclInternalError/i,
    message: 'Multi-GPU communication (NCCL) failed.',
    fixes: [
      { label: 'Set TP to 1 (single GPU)', kind: 'field', field: 'tp', value: '1' },
      { label: 'Enable enforce eager', kind: 'field', field: 'enforce_eager', value: true },
    ],
  },
  {
    pattern: /memory capacity is unbalanced|Some GPUs may be occupied by other processes|pre_model_load_memory=.*local_gpu_memory/i,
    message: 'SGLang refused to start because free GPU memory is uneven across the selected tensor-parallel GPUs.',
    suggestion: 'Run Clear GPUs, then relaunch. If it still fails, choose only equally free GPUs or lower TP/context.',
    fixes: [
      { label: 'Clear GPUs', kind: 'clear-gpus' },
      copy('Copy clear command', GPU_CLEANUP_COMMAND),
      edit,
      { label: 'Set TP to 1', kind: 'field', field: 'tp', value: '1' },
      { label: 'Lower context', kind: 'field', field: 'ctx', value: '32768' },
    ],
  },
  {
    pattern: /KV cache.*too (small|large)|max_model_len.*exceeds|maximum.*context/i,
    message: 'Context length too large for available GPU memory.',
    fixes: [
      { label: 'Lower to 8192', kind: 'field', field: 'ctx', value: '8192' },
      { label: 'Lower to 4096', kind: 'field', field: 'ctx', value: '4096' },
      { label: 'Lower to 2048', kind: 'field', field: 'ctx', value: '2048' },
    ],
  },
  {
    pattern: /vllm.*command not found|No module named vllm/i,
    message: 'vLLM is not installed or not in PATH.',
    fixes: [deps('vllm'), { label: 'Check environment is set', kind: 'focus', field: 'env_type' }],
  },
  {
    pattern: /sgl_kernel[\s\S]*(Python\.h|libnuma\.so\.1|common_ops|libnvrtc\.so)|(?:Python\.h|libnuma\.so\.1|common_ops|libnvrtc\.so)[\s\S]*sgl_kernel|Could not load any common_ops library|Please ensure sgl_kernel is properly installed/i,
    message: 'SGLang native kernel/runtime is missing or mismatched on this server.',
    suggestion: 'Relaunch with Faustus’ venv CUDA library path fix. If the venv does not contain the matching NVIDIA runtime libs, run Repair sglang-kernel.',
    fixes: [
      { label: 'Edit / relaunch serve', kind: 'edit' },
      { label: 'Repair sglang-kernel', kind: 'pip-task', name: 'repair-sglang-kernel', args: 'install -U --force-reinstall --no-cache-dir sglang-kernel' },
      copy('Copy OS package command', 'sudo apt-get install -y libnuma-dev python3.12-dev build-essential'),
      deps('sglang'),
    ],
  },
  {
    pattern: /sglang.*command not found|No module named sglang|SGLang is not installed/i,
    message: 'SGLang is not installed or not in PATH.',
    fixes: [deps('sglang'), copy('Copy install command', 'python3 -m pip install "sglang[all]"')],
  },
  {
    pattern: /No module named ['"]?mlx_lm|mlx_lm.*command not found|MLX is not installed|MLX LM is not installed/i,
    message: 'MLX LM is not installed on this server.',
    suggestion: 'Install mlx-lm in the selected Python environment. MLX serving is intended for Apple Silicon Macs.',
    fixes: [{ label: 'Install MLX LM', kind: 'pip-task', name: 'install-mlx-lm', args: 'install -U mlx-lm' }, deps('mlx_lm'), copy('Copy install command', 'python3 -m pip install -U mlx-lm')],
  },
  {
    pattern: /mflux-generate-qwen.*not found|mflux-generate.*not found|MLX image serving requires mflux|No module named ['"]?mflux/i,
    message: 'MLX image serving requires mflux on this Apple Silicon server.',
    suggestion: 'Install mflux in the selected Python environment. This is for MLX image generation, not text MLX-LM.',
    fixes: [deps('mflux'), copy('Copy install command', 'python3 -m pip install -U mflux fastapi uvicorn')],
  },
  {
    pattern: /Unable to quantize model of type <class ['"]mlx_lm\.models\.switch_layers\.QuantizedSwitchLinear['"]>|QuantizedSwitchLinear/i,
    message: 'MLX-LM tried to quantize an already-quantized DeepSeek switch layer.',
    suggestion: 'Relaunch from the cached local snapshot path. Faustus rewrites MLX repo-id launches to the newest local Hugging Face snapshot when it exists on the selected Mac.',
    fixes: [{ label: 'Edit / relaunch serve', kind: 'edit' }, deps('mlx_lm'), { label: 'Copy error', kind: 'copy-output' }],
  },
  {
    pattern: /No accelerator \(CUDA, XPU, HPU, NPU, MUSA, MPS\) is available|Triton is not supported on current platform/i,
    message: 'SGLang needs a visible GPU/accelerator on this server.',
    suggestion: 'Switch this serve config to llama.cpp for CPU/local serving, or choose a GPU server.',
    fixes: [{ label: 'Switch to llama.cpp', kind: 'cpu-edit' }, { label: 'Choose GPU server', kind: 'edit' }],
  },
  {
    pattern: /flashinfer.*version.*does not match|flashinfer-cubin version/i,
    message: 'FlashInfer version mismatch.',
    fixes: [{ label: 'Auto-fix: bypass version check', kind: 'env-fix', value: 'FLASHINFER_DISABLE_VERSION_CHECK=1', autofix: true }],
  },
  {
    pattern: /torch\.cuda\.is_available\(\).*False|No CUDA runtime/i,
    message: 'vLLM needs a visible CUDA/ROCm GPU.',
    suggestion: 'Switch this serve config to llama.cpp for CPU/local serving, or choose a GPU server.',
    fixes: [{ label: 'Switch to llama.cpp', kind: 'cpu-edit' }, { label: 'Choose GPU server', kind: 'edit' }],
  },
  {
    pattern: /Engine core initialization failed/i,
    message: 'vLLM engine failed to start. Check the error above.',
    fixes: [
      { label: 'Retry with --enforce-eager', kind: 'retry-add', flag: '--enforce-eager', autofix: true },
      { label: 'Retry with context 4096', kind: 'retry-add', flag: '--max-model-len 4096', autofix: true },
      { label: 'Lower context to 4096', kind: 'field', field: 'ctx', value: '4096' },
      { label: 'Lower GPU mem to 0.80', kind: 'field', field: 'gpu_mem', value: '0.80' },
    ],
  },
  {
    pattern: /weight_loader.*unexpected keyword|Unexpected key.*state_dict/i,
    message: 'Model format incompatible with this vLLM version.',
    fixes: [{ label: 'Try trust remote code', kind: 'field', field: 'trust_remote', value: true }],
  },
  {
    pattern: /enable-auto-tool-choice requires --tool-call-parser/i,
    message: 'Auto tool choice needs a tool call parser.',
    fixes: [{ label: 'Retry with --tool-call-parser hermes', kind: 'retry-add', flag: '--tool-call-parser hermes', autofix: true }],
  },
  {
    pattern: /Please pass.*trust.remote.code=True|contains custom code which must be executed to correctly load/i,
    message: 'Model requires custom code. Enable --trust-remote-code.',
    fixes: [{ label: 'Retry with --trust-remote-code', kind: 'retry-add', flag: '--trust-remote-code', autofix: true }],
  },
  {
    pattern: /does not recognize this architecture|model type.*but Transformers does not/i,
    message: 'Model architecture too new for installed vLLM/transformers.',
    fixes: [
      { label: 'Try --trust-remote-code', kind: 'retry-add', flag: '--trust-remote-code', autofix: true },
      { label: 'Update vLLM on server', kind: 'pip-task', name: 'update-vllm', args: 'install -U vllm transformers' },
    ],
  },
  {
    pattern: /Either a revision or a version must be specified|transformers\.integrations\.hub_kernels|kernels\/layer/i,
    message: 'Transformers/kernels package mismatch.',
    fixes: [{ label: 'Repair kernel package', kind: 'pip-task', name: 'repair-kernels', args: 'install --user --break-system-packages "kernels<0.15"' }, deps('sglang')],
  },
  {
    pattern: /ollama.*command not found/i,
    message: 'Ollama is not installed on this server. Run: curl -fsSL https://ollama.com/install.sh | sh',
    fixes: [copy('Copy install command', 'curl -fsSL https://ollama.com/install.sh | sh')],
  },
  {
    pattern: /cmake: command not found|cmake.*not found.*Could not/i,
    message: 'cmake is required to compile llama.cpp from source, but it is not installed on this server.',
    suggestion: 'Install cmake via the OS package manager — apt: cmake build-essential / pacman: cmake base-devel / dnf: cmake gcc-c++ make / brew: cmake. Cookbook can do this automatically on the next launch if your user has passwordless sudo for apt/pacman/dnf.',
    fixes: [deps('llama_cpp'), copy('Copy apt install', 'sudo apt install -y cmake build-essential git'), copy('Copy pacman install', 'sudo pacman -Sy --needed cmake base-devel git'), copy('Copy dnf install', 'sudo dnf install -y cmake gcc gcc-c++ make git')],
  },
  {
    pattern: /^(make|g\+\+|gcc): command not found|Could not find C\+\+ compiler/im,
    message: 'A C/C++ compiler (build-essential / base-devel) is required to compile llama.cpp.',
    fixes: [deps('llama_cpp'), copy('Copy apt install', 'sudo apt install -y build-essential')],
  },
  {
    pattern: /^git: command not found/im,
    message: 'git is required to clone the llama.cpp source tree.',
    fixes: [deps('llama_cpp'), copy('Copy apt install', 'sudo apt install -y git')],
  },
  {
    pattern: /llama-server.*command not found|llama\.cpp.*not found|No module named.*llama_cpp|No module named 'starlette_context'/i,
    message: 'llama-cpp-python server is not installed. Run: pip install "llama-cpp-python[server]"',
    fixes: [deps('llama_cpp'), copy('Copy install command', 'pip install "llama-cpp-python[server]"')],
  },
  {
    pattern: /Windows Error 0xc000001d|Illegal instruction|0xc000001d/i,
    message: 'AVX2 Instruction Set Mismatch: the precompiled llama-cpp-python wheel requires CPU features (AVX2/FMA) that your processor or virtual machine lacks.',
    suggestion: 'Switch this serve config to Ollama (highly recommended, has dynamic CPU fallbacks), or choose a remote Linux GPU server.',
    fixes: [{ label: 'Switch to Ollama', kind: 'edit', overrides: { backend: 'ollama' } }, { label: 'Choose remote server', kind: 'edit' }],
  },
  {
    pattern: /CUDA Toolkit not found|Unable to find cudart library|missing:\s*CUDA_CUDART/i,
    message: 'llama.cpp found nvcc, but the CUDA runtime library is missing.',
    suggestion: 'Relaunch with the updated runner so llama.cpp builds CPU-only, or install a complete CUDA toolkit/runtime on this server for GPU llama.cpp.',
    fixes: [edit, deps('llama_cpp')],
  },
  {
    pattern: /No module named ['"]?torch|No module named ['"]?torchvision|No module named ['"]?diffusers|No module named ['"]?scipy|install scipy if you want to use beta sigmas|requires the Torchvision library|diffusers.*command not found/i,
    message: 'Diffusion serving needs PyTorch, Torchvision, Diffusers, Accelerate, and SciPy. Install Diffusers image deps from Cookbook → Dependencies.',
    fixes: [deps('diffusers'), copy('Copy install command', 'python3 -m pip install "diffusers[torch]" torchvision accelerate scipy python-multipart')],
  },
  {
    pattern: /Triton kernels.*Failed to import|cannot import name '\w+' from 'triton_kernels/i,
    message: 'Triton kernels version mismatch. Non-fatal warning — model will still run, just without optimized MoE kernels.',
    fixes: [{ label: 'Update triton on server', kind: 'pip-task', name: 'update-triton', args: 'install -U triton triton-kernels' }],
  },
  {
    pattern: /No space left on device|Disk quota exceeded|ENOSPC/i,
    message: 'Disk full on the server. Free up space before retrying.',
    fixes: [{ label: 'Check HF cache size', kind: 'quick-cmd', cmd: 'du -sh ~/.cache/huggingface 2>/dev/null' }],
  },
  {
    pattern: /Connection refused|Could not connect|Connection reset by peer/i,
    message: 'Network connection failed. Server may be unreachable or HuggingFace is down.',
    fixes: [{ label: 'Test HF connectivity', kind: 'quick-cmd', cmd: 'curl -sI https://huggingface.co 2>&1 | head -3' }],
  },
  {
    pattern: /attention_sink|sliding.window.*not supported|sliding_window.*incompatible/i,
    message: 'Model uses attention features unsupported in this vLLM version.',
    fixes: [{ label: 'Update vLLM on server', kind: 'pip-task', name: 'update-vllm', args: 'install -U vllm' }],
  },
  {
    pattern: /nvcc fatal\s+:\s+Unsupported gpu architecture 'compute_\d+'/i,
    message: 'FlashInfer is JIT-compiling sampling kernels with an nvcc too old for this GPU (no sm_89 / sm_90 support — pre-CUDA 11.8). Changing the attention backend does not help — flashinfer JITs the SAMPLER too. The clean fix is to set VLLM_USE_FLASHINFER_SAMPLER=0 so vLLM uses its native sampler instead.',
    suggestion: 'Relaunch with VLLM_USE_FLASHINFER_SAMPLER=0 prepended.',
    fixes: [
      { label: 'Retry with VLLM_USE_FLASHINFER_SAMPLER=0', kind: 'retry-prepend', value: 'VLLM_USE_FLASHINFER_SAMPLER=0 ' },
      { label: 'Uninstall flashinfer-python', kind: 'pip-task', name: 'uninstall-flashinfer', args: 'uninstall flashinfer-python -y' },
      edit,
    ],
  },
  {
    pattern: /ImportError: cannot import name '[^']+' from 'torch(\.\w+)+'/i,
    message: 'vLLM was built against a newer torch than what is installed. Reinstall vLLM so pip pulls a compatible torch (or upgrade torch directly).',
    fixes: [
      { label: 'Reinstall vLLM (pulls matching torch)', kind: 'pip-task', name: 'reinstall-vllm', args: 'install --force-reinstall vllm' },
      { label: 'Upgrade torch only', kind: 'pip-task', name: 'upgrade-torch', args: 'install -U torch' },
    ],
  },
  {
    match: (text) => {
      const tail = text.slice(-6000);
      if (HEALTHY.test(tail)) return false;
      return /Failed to build\b|subprocess-exited-with-error|Could not build wheels|metadata-generation-failed/i.test(tail);
    },
    message: 'A dependency failed to build during install — usually an older package whose build breaks on this Python version, not a server problem. The install did not finish.',
    suggestion: 'Check the captured output for the package that failed to build; it may need a newer release or a patch to install on this Python version.',
    fixes: [],
  },
  {
    match: (text) => {
      const tail = text.slice(-4096);
      if (!/Traceback \(most recent call last\)/i.test(tail)) return false;
      if (HEALTHY.test(tail)) return false;
      return /vllm/i.test(tail);
    },
    message: 'A vLLM process hit a Python traceback and may be wedged.',
    fixes: [{ label: 'Kill vLLM processes', kind: 'quick-cmd', cmd: 'pkill -f vllm' }],
  },
  {
    match: (text) => {
      const tail = text.slice(-4096);
      if (!/Traceback \(most recent call last\)/i.test(tail)) return false;
      return !HEALTHY.test(tail);
    },
    message: 'Python traceback detected — check the captured output for the underlying error.',
    suggestion: 'Read the captured output for the failing step; copy the troubleshooting bundle if you need help.',
    fixes: [],
  },
];

/** First matching diagnosis for a task's output, or null. */
export function diagnose(text: string): Diagnosis | null {
  if (!text) return null;
  for (const entry of ERROR_PATTERNS) {
    const hit = entry.match ? entry.match(text) : entry.pattern!.test(text);
    if (hit) return { message: entry.message, suggestion: entry.suggestion, fixes: entry.fixes };
  }
  return null;
}
