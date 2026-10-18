# Composer draft recovery

Saved conversations keep their unsent text and attachment IDs on the server,
scoped to the owner and conversation. Draft storage no longer imposes a separate
64 KiB character limit or an 80,000-byte JSON request limit. Saving a draft does
not submit it to a model; model context handling still applies when sending.

`GET /api/sessions/{sid}/draft` reads the draft, `PUT` replaces it, and `PATCH`
preserves omitted fields. The existing `hoard_draft_get` and `hoard_draft_save`
MCP tools use these same endpoints. Empty or whitespace-only text with no
attachments clears the draft. Writes remain atomic and last-write-wins.
Attachment validation remains 64 IDs at most, each a nonempty string of at most
300 characters.

Studio reads the server even when a saved conversation is opened directly.
Its one-second save debounce waits for that initial read. A newer remote draft
replaces the local copy; typing or attaching a file while the read is pending
keeps those local edits. Responses from a conversation that was left, or from
before Nobody mode was enabled, cannot replace the current draft. Conversation
switches do not write the previous conversation's text into the new slot.

Server attachment IDs are restored with any existing local metadata. If another
browser has no filename or media metadata, the attachment ID is shown as its
name, using the existing attachment fallback. Nobody mode and conversations
that have not been saved do not sync drafts to the server. Browser storage can
still run out of space; server draft recovery does not require a previous local
copy.

Validation covers Unicode drafts of 80,003 characters and an exact 3,192,509
character fixture through storage, HTTP PUT/PATCH/GET, native MCP, and Studio.
The Studio effect check runs the actual draft effects with React and delayed
server responses, including session switches, local edits, attachment recovery,
and Nobody mode. Run the focused suite with:

```text
python -m pytest tests/test_session_draft.py tests/test_l70_a14_session_draft_route.py tests/test_l70_a14_studio_draft_sync_js.py
```
