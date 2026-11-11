---
name: cluster-model-serving
description: Choose which machine of a small GPU cluster serves which model, load it, check memory and health, move files between machines and stop cleanly. Use when language models must be served or moved across a few GPU machines without running out of memory or leaving jobs behind.
version: 1.0.0
category: engineering
tags: [cluster, gpu, model-serving, inference, vllm, memory, ssh, files, cluster-gpu, servidores, modelos, memoria]
apps: [prometheus]
status: published
source: imported
---

## When to Use

You manage a handful of GPU machines and must decide where each model runs, start it, confirm it works, move model files, and shut things down. This procedure covers placement, loading, monitoring and shutdown. It does not cover training and does not cover changing network or firmware settings on the machines.

## Procedure

1. Read the app's tool list. Take stock before deciding: for each machine the GPU memory total and free, disk space, what is already running, who is using it and whether it is reachable. A machine that is asleep may need to be woken first.
2. Estimate each model's need: weights at the chosen precision plus the cache that grows with context length and concurrent requests, plus working overhead. Weights alone are a lower bound; a model that fits only without a cache will fail on the first long request.
3. Place models. Put the model with the strictest memory need on the machine with most free memory, keep latency-sensitive models away from heavy batch ones, and avoid splitting one model across machines unless it cannot fit otherwise, since the link becomes the limit. Write the plan with expected memory per machine and leave headroom of at least 10 percent.
4. Move files if the model is not where it will run. Check disk space first, transfer, then compare size or checksum at the destination. An interrupted copy often looks complete.
5. Load with a recipe that records the model, precision, context length, memory fraction and port. Watch the startup log for out-of-memory messages and read memory after it is ready, not only before.
6. Check health with more than "the port answers": send a short prompt and a long one, read tokens per second, queue length, cache usage and latency from the serving statistics. Compare with the benchmark for that model if one exists. Watch the GPU error log too.
7. Keep a record of what runs where, with the endpoint address for clients, so others do not collide on a busy machine. When a request needs a different model, free the old one cleanly before loading the new one.
8. Stop cleanly: stop the serving processes, confirm that GPU memory is freed and ports are closed, remove temporary files you created, and power machines down only if nobody else needs them.

## Pitfalls

- Sizing from weights alone and running out of memory on a long context.
- Starting a second model on a GPU that still holds the first model's leftover memory.
- Copying a model onto a nearly full disk.
- Calling a server healthy because it responds to a ping.
- Powering off a machine that serves someone else.
- Leaving a half-loaded model after a failed start.

## Verification

- A written placement plan with expected memory per machine and headroom.
- Each served model answered a short and a long prompt, with throughput recorded.
- Moved files match in size or checksum.
- After shutdown, GPU memory shows free and no process remains on the ports used.
