# GLM implementation and Qwen review

Use an opt-in persistent working group in Agora. The existing resident GLM
remains the default; a separate authenticated OpenAI-compatible endpoint serves
Qwen on the fourth Spark. This is API collaboration between independent models.

## Participant execution

`POST /api/groups` and `PATCH /api/groups/{id}` accept the existing participant
routes/personas, plus two optional participant fields:

| Field | Meaning |
| --- | --- |
| `autonomyPreset` | Existing native `supervised`, `bounded_autonomous` or `read_only` preference. Omission retains project/global behaviour. |
| `inputTokenBudget` | Existing per-turn input budget, integer 4096–200000. This is a soft input budget, not the model's physical context window. |

Policies are saved with the composition and snapshotted for each queued turn.
Question continuations retain the participant's policy. Invalid policy values
are rejected before sessions are created. Native read-only enforcement excludes
write, shell execution, administrative and unknown-effect tools even if a model
attempts to call them; a persona alone does not provide that restriction.

Steps record the selected `model`, `endpointId` and redacted `endpointUrl` in
`detail.route`. Canonical parent messages retain this as `metadata.group_route`.
Headers and API keys are excluded. The native reply's model/endpoint metadata and
the serving API must also be checked when verifying actual routing.

Normal chat sessions can retain provider authentication headers as well as the
encrypted endpoint record. New nonempty session-header writes now use the same
existing `secret_storage` key, while preserving the JSON column and dictionary
API. Legacy header objects remain readable and are not rewritten when unrelated
session metadata changes. There is no new credential store or mass migration.

The built-in `groups` MCP exposes these fields in `group_create` and
`group_update`; `group_get` returns the persisted composition and step evidence.

## Composition

Use IDs returned by the real endpoint/model APIs. The following is a template;
replace `QWEN_ENDPOINT_ID` after registering and testing the authenticated
endpoint through `/api/model-endpoints`. No credential belongs in a group.

```json
{
  "name": "GLM + Qwen Code Review",
  "mode": "round-robin",
  "executionMode": "agent",
  "participants": [
    {
      "personaName": "Implementador GLM",
      "route": {"endpointId": "sparks-2ba9eff4", "model": "glm-5.3-flash-nvfp4"},
      "personaPrompt": "Inspect the actual task and source. Implement the smallest correct change and run relevant tests. Give the reviewer the actual diff, acceptance criteria and exact tool outputs. Correct only substantiated findings. Do not claim unexecuted tests."
    },
    {
      "personaName": "Revisor Qwen (solo lectura)",
      "route": {"endpointId": "QWEN_ENDPOINT_ID", "model": "qwen3.8-flash-next"},
      "autonomyPreset": "read_only",
      "inputTokenBudget": 180000,
      "personaPrompt": "Independently inspect the actual diff, requirements, source and native test outputs. Do not edit files. Report verdict pass, changes_requested or inconclusive, findings with verified file, location, severity, concrete evidence and a reproducing test, and unverified areas. A peer summary alone is not evidence."
    }
  ]
}
```

The 180000-token soft budget leaves room below Qwen's expected 262144-token
window for system/tool context and output. Verify the actual loaded window; do
not substitute GLM's 1048576-token setting. Large reviews should be partitioned
by file/module with an explicit record of excluded material. Native context
trimming is not proof that the reviewer saw every source file.

The model metadata table distinguishes Flash-Next's native 262144-token window
from the generic Qwen3 family fallback. This architectural fallback is not a
measurement of a particular deployment. A smaller reported local serving window
still takes precedence; verify the authenticated catalog after service changes.

Send the implementation/review task once with a stable `request_id`. Later
participants receive completed native tool outputs. Require `git diff` output
and test commands/results in that evidence. If Qwen reports reproducible
findings, send a new directed turn with `mentions` containing the implementer's
session ID; then direct a fresh review to Qwen. Default to at most two correction
cycles per task, and report unresolved findings. The ordinary two-member group
does not automatically schedule those correction turns or parse a verdict as
permission to integrate. A structured verdict must be validated before use.

An unavailable reviewer remains an error/pending review. Do not replace its
route with GLM or replay completed writes. FIFO, request receipts, group Stop,
ownership and native recovery retain their existing semantics. Keep GLM-only
conversations available independently of this group.

## Fourth Spark installation

On 16 October 2026 the fourth machine was `gx10-c94d.local` (NVIDIA Sync alias
`Spark4Asus`), Ethernet DHCP `192.168.0.7`, GB10, 121 GiB RAM and 822 GiB free
NVMe before installation. SSH succeeds through Sync; Windows name resolution
outside SSH did not resolve mDNS, so test both hostname and current DHCP address.
Faustus runs as a native Windows backend on `127.0.0.1:7000`.

The audited external recipe is
[`hasso5703/dgx-spark-qwen38`](https://github.com/hasso5703/dgx-spark-qwen38),
commit `08294c6e8f603abd736233a9ae040db962aa9b22` (MIT). Its Flash checkpoint is
`RadixArk/Qwen3.8-Flash-Next-NVFP4`, revision
`7b719225242aacd3dbd3f9407468c2ee9a9d2594`; the serving image is
`lmsysorg/sglang@sha256:3e79541d1e2a6818979c4aa9ca1fa63d6961d7bc50120867953ea6baaf1d8f62`.
Model licensing is separate from the installer licence; verify the checkpoint's
model card/licence for the intended use.

After checking GPU, disk, Docker access, ports and source, run as the normal
user on this machine only:

```bash
MODEL_CHOICE=flash FLASH_TIER=context ENGINE_BIND=127.0.0.1 PROXY_BIND=0.0.0.0 \
  ./install.sh --no-27b --no-opencode --no-cockpit --no-image --no-video \
  --no-kolibri --no-nemotron
```

The audited proxy accepts only loopback or all-IPv4 bind addresses. Restrict
port 30001 to loopback and the trusted LAN with the host firewall before
exposing it. The engine remains on loopback port 30000. Preserve SSH and other
firewall rules. No router forwarding is needed. Use the proxy, never direct
engine access for external clients. Retain authentication.

On this fresh machine Docker initially failed with `error creating buildkit
instance: invalid database`. The broken `/var/lib/docker/buildkit` cache was
renamed in place to `buildkit.invalid-668-20261016-160218`, preserving it for
recovery; containers, images and volumes were not moved. Docker then started.

## Validation, diagnosis and rollback

Check `qwen38-flash`, `qwen38-keepalive` and the installation journal. Initial
model and PLE preparation can take many minutes. Verify authenticated
`/v1/models` and actual `/v1/chat/completions` on the Spark and from Faustus,
plus a rejected request without credentials. Read the key from
`~/.config/qwen38/api-key` into memory, never print it. Save it through the
existing encrypted endpoint persistence; do not add a plaintext credential
file to Faustus.

Verify tools and structured output against the real service. A health response,
catalog entry or scripted provider is not an inference test. Preserve explicit
failures for 401, connection errors, overload, context overflow and startup.
Restart only the fourth Spark's service when testing recovery, and verify GLM
before/after. A service restart does not establish whole-machine reboot recovery.

Disable/archive the new group and disable only its Qwen endpoint to roll back
Faustus. Stop the fourth Spark's Qwen services if necessary. Keep the existing
GLM configuration, default and all three hosts untouched. Review an external
uninstaller's exact deletion list before considering it.

Benchmark at least 20 identical controlled tasks in GLM-only, GLM self-review
and GLM + Qwen arms before any automatic activation. Record test success,
regressions, substantiated findings, false positives, latency, usage and retries.
This integration alone establishes no quality improvement.

## Deployment log and credential handling

The tested engine printed its merged `api_key` in a startup `ServerArgs` line.
The initial key was rotated; an actual request using that expired credential
returned 401. Existing journals were retained. The two Faustus endpoint records
were updated in place using `PATCH /api/model-endpoints/{id}` and the normal
encrypted store; endpoint IDs and the global GLM route were preserved.

Existing sessions retain their saved headers. After this explicit key rotation,
the eight task-owned Qwen sessions were rebound through the normal
`PATCH /api/session/{id}` model picker operation, keeping IDs, names and history.
A request from an old session first returned 401; after rebinding, that same
conversation generated successfully with the new credential. A future manual
key rotation likewise needs existing conversations to reselect the endpoint.
Ordinary machine reboots keep the key and do not require this operation.

Spark 4 has a generated-launcher override, `docker run --log-driver=none`, plus
`/etc/systemd/system/qwen38-flash.service.d/668-log-secrets.conf` containing
`LogFilterPatterns=~api_key`. Normal engine diagnostics continue through the
system journal, while the credential-bearing configuration line is discarded
and Docker does not retain a second raw log. The final boot's actual journal
was checked in memory for the current credential, with zero matches. See
[systemd's filter contract](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml)
and [Docker's per-container logging configuration](https://docs.docker.com/engine/logging/configure/).

`~/protect-qwen-logs.sh` reapplies these overrides after an installer refresh.
It is idempotent when already applied; reapplying a lost launcher override does
not rotate the key again. Do not print the key or `engine-secrets.yaml`, or
publish unfiltered historical startup configuration. The installation bootstrap,
operational scripts, hashes and real verification results are retained with the
task's local evidence. These deployment overrides are outside the pinned
installer repository.
