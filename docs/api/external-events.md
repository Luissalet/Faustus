# External events and native jobs

Incoming events reuse the native task scheduler, agent/action/research executors,
task history and sessions. An event receipt and its one-shot native jobs commit
together. The source's stable identity is scoped to its owner, source and event
type. Replaying it returns the existing jobs and does not increment thresholds.
Two different events queue separate jobs even while the first is running.

`POST /api/tasks/events` accepts the authenticated owner's envelope:

```json
{
  "source": "documents",
  "event_type": "document_created",
  "event_id": "source-document-42",
  "payload": {"document_id": "source-document-42"},
  "delivery": {"app": "receiver", "tool": "save_report", "result_field": "content"}
}
```

Active event subscriptions matching `event_type` supply the task definition.
Optional `task_id` targets one active task of the same owner. LLM/research jobs
receive the source envelope in their prompt. Action scripts/configuration remain
byte-for-byte unchanged; their event context remains available in the receipt.
Task settings and existing session output are retained. Jobs snapshot the
definition when received and appear under its Incoming events section; their
native Activity/history and Stop remain available through their task IDs.

`GET /api/tasks/events?task_id=<definition>` returns receipts, native job/run IDs,
execution status, full retained result, chat ID and separate delivery outcome.
The newest100 event receipts are listed; older native work and chats remain in
their normal histories. `POST /api/tasks/events/<job>/retry-delivery` sends only
the retained successful result. It never reruns work or resends a delivered job.

Hub delivery can call an existing app tool, with `arguments` plus the result and
stable job key (`result_field` defaults to `result`, `key_field` to `event_id`).
Alternatively `delivery: {"event_type": "faustus.job.done"}` emits metadata and
a result URL into the existing Hub event bus/rules; full text stays in Faustus.
Delivery states are none, pending, sending, delivered, failed and unknown.
Interrupted or unacknowledged sends become unknown and are not automatically
replayed. An explicit retry keeps the same job key. Receiver support determines
deduplication after an ambiguous manual retry; universal exactly-once external
effects are not promised. Execution success remains visible when delivery fails.

The existing token webhook `/api/tasks/<id>/webhook/<token>` still supports an
empty body as Run now. A JSON envelope queues durable per-event work. Gmail
PubSub uses `source: "gmail_pubsub"` and its `message.data` base64 JSON
`emailAddress`/`historyId`; the history identity deduplicates redelivery even if
PubSub changes its messageId. It is a notification to the normal task, not an
automatic Google Cloud subscription installer or a copied email body.

Existing document creation APIs/tools emit document IDs/title. Existing IMAP
observation emits account/folder/message identity before marking mail seen.
Its initial inbox remains a baseline; repeated polls/reconnections do not replay
mail. The existing central mail poller observes subscribed accounts every30
seconds through a single header-only pass, with persisted UID/UIDVALIDITY cursors.
It uses read-only INBOX and BODY.PEEK, without reading bodies or marking messages;
reset UID state establishes another baseline. Failed headers stay before the
cursor and are retried. The pass runs independently of scheduled SMTP delivery.
No per-Hoard poller or separate inference runtime is introduced. This is polling,
not IMAP IDLE, and does not install a mailbox or Google Cloud subscription.

Agent tool `manage_tasks` adds event, events and retry_delivery. Native Execution
MCP adds event_ingest, event_receipts and event_retry_delivery, sharing the same
database/handlers. The configured owner applies on every path. Text inference
continues through the configured global service; tests use private HTTP fixtures
and do not establish live model quality or autonomy.
