# Shared copilot attachments

Hoard panels use the same upload, draft and turn services as Studio. The shared
composer lets the user choose files, remove a pending attachment, retry a failed
upload and send files with or without message text. Selection is bound to the
original conversation before asynchronous context or network work begins.

The TypeScript SDK exposes `uploads.create(files, sessionId?)`, returning native
upload metadata. `copilot.saveDraft(sid, text, attachmentIds?)` preserves existing
IDs when omitted and detaches files when given an empty list. The native PATCH
endpoint preserves either omitted field; PUT still replaces the draft. An MCP
update of attachment IDs alone never clears the message. The component keeps
pending uploads and ready IDs separately for each conversation. Uploaded IDs
survive panel recreation through the canonical draft. Unuploaded files remain
available for retry while that panel instance exists; raw File objects are not
persisted across a full page reload. Restored unsent IDs have generic localized
labels because the draft stores IDs rather than upload metadata. Native history
keeps the uploaded filenames and the attachment references.

MCP provides `hoard_attachment_upload`, `hoard_draft_get` and `hoard_draft_save`.
`hoard_turn_start` accepts uploaded attachment IDs through the same native
multipart contract. The Python and Node family relays admit the exact upload
route and preserve multipart bytes. No agent, upload store or model lives in
the Hoard relay.

In no-login mode uploads, chunked uploads and attachment reads use the existing
canonical storage identity, matching chat preprocessing. Authenticated browser
and API-token owners keep their existing identities. Older ownerless uploads
remain readable as local files in no-login mode without reassigning their owners,
IDs or content. Authenticated mode keeps its existing owner checks.

Verification covers original-chat ownership during context capture and network
completion, upload failure/retry, draft recreation, selective removal, stable
retry IDs, attachment-only turns, authenticated owner attribution, byte-exact
native multipart relay and actual HTTP/MCP upload-to-turn behavior. The private
CPU provider receives the uploaded CSV. This is interface and data-flow evidence,
not a GLM quality or completed family acceptance claim.

Conversation changes detach the local observer without cancelling the original
remote turn. A send fixes its session, mode, context and attachment IDs before
asynchronous preparation. An older completion cannot replace the newer
conversation's draft or transcript. Pending retries are kept per conversation,
and an older initialization cannot select itself over a newer session.
