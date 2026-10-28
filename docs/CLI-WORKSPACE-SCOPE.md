# Project task workspaces

A request to /api/chat_stream may supply workspace as multipart form data or JSON.
The native CLI already forwards --workspace. For a project chat, an explicit,
already-vetted directory equal to the primary project root or canonically
inside it becomes the execution and checkpoint workspace. With no selection,
the project root remains the default. Chats without a project retain their
existing workspace behavior.

This fixes a route assignment that replaced every explicit task folder with
the whole project root. A task inside a large project can now checkpoint its
own folder without first snapshotting the entire project. Actual latency must
be measured; this change does not promise a particular speed or token saving.

Invalid explicit project selections return HTTP400 before inference, rather
than silently falling back to the larger root. The existing authorization and
vetting still run first. Comparison uses realpath, normcase and commonpath;
a sibling sharing a name prefix, another drive or a resolved escape does not
qualify. No filesystem links are created by the implementation or its tests.

The project identity and attached tool-access roots remain unchanged, including
exact file attachments. This selects execution/checkpoint scope; it does not
claim narrower file-access authority. Attached roots do not become alternate
execution roots. The project trust setting is inherited only at the exact
project root, as before. When an approval resumes a sealed action, the sealed
workspace remains authoritative and the workspace-dependent trust option is
refreshed without dropping other options added for that request.

Verification before integration: an external13-case production-AST oracle
went from5PASS/8FAIL to13PASS; a separate3-case sealed-approval oracle went
from0PASS/3FAIL to3PASS, with frozen bytes unchanged. Ten permanent standard
library tests also pass on Windows, including drive and case semantics. These
are source-level results with explicit auth/project seams, not a live service,
model, bearer-authentication or performance claim. Live validation is recorded
separately after integration and a coordinated service restart.
