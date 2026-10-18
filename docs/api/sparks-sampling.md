# Sampling on managed Sparks endpoints

An endpoint published by a managed Sparks recipe keeps its server sampling
defaults. Its saved endpoint ID must occur in `sparks_endpoints`; the rule does
not depend on a hostname, IP address, recipe name or model family. An ordinary
local endpoint retains the existing local sampler defaults.

Faustus does not inject `local_top_p_default`, `local_top_k_default`,
`local_min_p_default` or `local_repeat_penalty_default` into a managed request.
It also skips the implicit local temperature floor and coding temperature cap.
The temperature supplied by the caller still goes out, including the ordinary
generic default. Faustus does not impose a model-specific replacement value.

Explicit session, preset and per-call sampling values keep their precedence.
Existing saved model options also remain effective on the native Ollama path;
caller options override saved options. Explicit `top_p`, `top_k`, `min_p` and
`repeat_penalty` are forwarded through their existing provider mappings. The
`repeat_penalty` extension is not a promise that every OpenAI-compatible server
recognizes it as `repetition_penalty`; backend validation still applies.

Both plain chat and agent fallback factories retain the original caller
temperature. A managed candidate therefore does not inherit a local primary's
automatic floor or coding cap. An ordinary local fallback from a managed primary
receives its existing local floor, and its coding cap when appropriate. Chains
without managed Sparks retain their previous behavior.

Output budgets are independent of this rule. The existing fallback output caps
remain in place, and explicit zero or negative unlimited budgets remain omitted.
Reasoning selection, structured-output schemas, first-response budgets,
inactivity timeouts and cancellation keep their existing contracts. No global
settings are changed and consumers are not automatically switched to streaming.

`tests/test_sparks_sampling.py` exercises real private TCP payloads for synchronous,
asynchronous and streaming calls, both directions of mixed agent fallback,
plain-chat candidate preparation, saved native model options and output budgets.
Its backend is synthetic. These checks establish request construction and do not
certify model answer quality or demonstrate a semantic improvement in production.
