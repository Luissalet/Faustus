# Long chat messages

Normal chat accepts text through JSON (`/api/chat` and `/api/chat_stream`),
URL-encoded or multipart form data (`/api/chat_stream`), Studio and the native
MCP `hoard_turn_start` tool. These entry points do not impose a character limit
independent of the selected model's context budget. They do not truncate the
submitted message to make transport validation pass.

`/api/chat_stream` reads its form with Starlette's `max_part_size=inf` option.
This applies to text parts of this route, including the budget decorator and
FastAPI dependency, rather than changing the framework's global parser. File
and field counts, upload validation and attachment handling retain their
existing limits. Studio uploads attachments separately and sends their IDs;
an arbitrary file part on the chat route is not a new attachment-upload API.

Empty messages remain invalid except for the existing attachment-only path.
The per-turn `initial_response_timeout_s` retains its finite range of 5 to 900
seconds, inherited project defaults and cancellation behaviour. Its dependency
reads the same cached JSON/form body as the request-scoped budget decorator;
FastAPI no longer pre-parses a default-size form before that reader runs.

Transport acceptance is separate from context selection and model success.
The existing agent AUTO budget can still reduce context (its configurable
hard maximum defaults to 200,000 tokens). Setting `agent_input_token_budget=0`
is an existing operator control; this change does not set it or alter any
explicit token budget. A context error returned by the model is still an
error, not a successful answer. Stop and persistent run handling are unchanged.

`tests/test_long_chat_message.py` covers Unicode above 50K, text parts above 1 MiB,
the real route through FastAPI with JSON and multipart, multipart with a file,
timeout rejection and retained upload validation. Private CPU integration
additionally checks the frozen 3,192,509-character QA prompt at the synthetic
backend by its original hash, separately from context metadata Faustus adds.
That test uses a 1M backend window and explicitly disables the existing soft
input budget in private settings to isolate transport. It does not prove model
quality, actual tokenization or that a particular live configuration sends 1M.
