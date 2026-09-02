# GPU placement — which card Ollama fills first

With two or more cards Faustus shows every card (usage panel: *Combined /
Separate*; Local models: one bar per card, where each loaded model sits) and
lets you choose how Ollama places models.

## What Ollama does on its own (measured, 0.33.2)

* A model that fits one card goes to the card with the **most free memory**.
* A model that fits no single card is **split** across all of them,
  proportionally to free memory. The ratio is Ollama's: `tensor_split` in the
  request is ignored.
* `main_gpu: N` pins a model to card N — and a pinned model that does not fit
  that card is **not** split: the rest runs on the CPU (27B on a 16 GB card:
  54/66 layers on the GPU, 10 tok/s instead of 19–24).

## The policy (Local models → *Placement*, or Settings → Agent & automation → GPU placement)

* **Auto** — Ollama's own choice (above).
* **Fill GPU N first** — every model whose weights fit card N *with room for
  its context* is pinned to it; bigger models stay Auto (split). So "fill the
  RTX 5060 Ti first" keeps the 4070 Ti free for what does not fit, and never
  sends layers to the CPU.
* A per-model pin (Options… → `main_gpu`) always wins over the policy. The
  form warns when the chosen card cannot hold the model.

The policy applies to every chat request (`llm_core`), to the Load button and
to dispatched workers — all on the local Ollama only.
