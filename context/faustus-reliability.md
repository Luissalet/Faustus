# Faustus reliability

## Retrieve repository reasons as claims

**Id:** da9b9098-eb7e-42e8-a8d4-e92ddc23fd8f
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** https://github.com/oliver-zehentleitner/keep-the-why
**Verification:** tests/test_repository_rationale.py and isolated Q8 conversation receipts in D:/LocalAI/qa/why-web-20261005
**Revisit when:** a demonstrated retrieval need exceeds bounded lexical ranking

Keep the Why stores reasons and rejected alternatives in repository Markdown.
The Context Engine retrieves relevant context/*.md for code and review tasks,
up to three files, with content hashes and workspace containment checks.
Status and Evidence remain distinct claims in the original text; retrieval
does not establish their truth. Existing file tools maintain notes when asked.
**Reason:** preserving the reason can prevent reopening an already tested
alternative, while a traceable file lets a reviewer check the evidence.
**Rejected alternative:** inject these notes as mandatory binding decisions
or project instructions; repository text may contain stale or malicious claims.
**Rejected alternative:** a separate decision database or automatic human-trust
promotion; this would duplicate existing stores and attribute unsupported authority.

## Passive HTTP facts without an invented security verdict

**Id:** db6cbb37-6538-4ab5-aac9-b71e59b60d8f
**Type:** decision
**Status:** active
**Evidence:** confirmed
**Source:** https://github.com/lissy93/web-check
**Verification:** tests/test_web_passive_profile.py and existing outbound-fetch guard tests
**Revisit when:** a valuable inspection needs a check not covered by Cassandra or this profile

web_fetch inspect=true reuses the existing guarded fetch and its cache. It
reports recorded status, final URL, capture time and selected HTTP headers;
absent metadata stays unknown, cookie values are excluded, unperformed checks
are listed and output remains bounded, including JSON escaping.
**Reason:** an ad hoc HTTP observation is useful beyond Cassandra's existing
monitoring, without another service, scans or a new Hoard.
**Rejected alternative:** install the complete Web-Check stack or create an
OSINT Hoard; monitoring coverage already exists and a wider need was not shown.
**Rejected alternative:** turn missing headers into vulnerability findings,
or date an old cache observation as if it had just been captured.

## MCP-only scope must survive workspace tool heuristics

**Id:** 458b8e6c-3cf9-4ad3-ac56-f03852ca86c5
**Type:** incident
**Status:** active
**Evidence:** confirmed
**Source:** isolated Ledger reliability checkpoint, 2026-10-05
**Verification:** tests/test_tool_policy.py and ledger-original-summary.json in the QA evidence directory
**Revisit when:** the tool authority changes its alias or semantic-key model

The initial Ledger read-only request attempted Python and shell fallbacks.
Explicit MCP-only scope now filters native schemas, prevents workspace-floor
restoration and blocks forced calls at execution. Discovery and planning helpers
remain available under all registered aliases. Existing approval gates remain.
**Reason:** a user restriction is an authorization boundary, not a prompt hint.
**Rejected alternative:** strengthen only the prompt; a model can still emit a
prohibited call. The first live fix also blocked helper aliases; the final
policy treats each helper's registered spellings as the same scope decision.

## Unknown prices do not establish a strict increase

**Id:** 0d532c19-4b4c-4028-8c64-447147ac4e35
**Type:** incident
**Status:** active
**Evidence:** confirmed
**Source:** isolated CookHoard Q8 conversations, 2026-10-05
**Verification:** cook-verified-summary.json and state-final.json in D:/LocalAI/qa/why-web-20261005
**Revisit when:** price-completeness semantics change

CookHoard returned complete=false and a missing tomato price. Qwen scaled
quantities and known costs correctly, but repeatedly said the full total
would be strictly greater. Faustus's native and fenced-tool base rules now
explicitly distinguish known subtotal from missing values and allow the
missing cost to be zero, such as a gift.
**Reason:** a missing observation does not establish a positive price.
**Rejected alternative:** infer a full total or a strict increase from the
presence of an unpriced ingredient.

## A catalogue is an observed subset, not proof of API absence

**Id:** d026edaa-8b3d-4631-9282-f34a1ab51ed1
**Type:** incident
**Status:** active
**Evidence:** confirmed
**Source:** isolated rationale Q8 conversations, 2026-10-05
**Verification:** why-read-summary.json, why-final-summary.json and actual context packet receipts
**Revisit when:** capability discovery provides an authoritative complete API inventory

The QA bridge deliberately exposes only thirteen scoped tools. Qwen initially
inferred that Ledger's full current API lacked idempotency from that subset
and a historical note. Both base prompt modes now separate observed catalogue
scope from unverified API capabilities. The repeated original question used
repository rationale and scoped code/context searches without making the same
global absence claim.
**Reason:** failure to observe a capability is not proof that it does not exist.
**Rejected alternative:** describe an old rationale card or a restricted tool
catalogue as exhaustive current verification.
