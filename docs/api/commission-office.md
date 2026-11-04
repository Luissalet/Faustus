# Commission office (Hangar E29, first cut)

`src/commission_office.py` keeps one record per client commission and follows
its cycle: opportunity → proposal → agreed → in progress → delivered → closed
(or lost). The first cut covers **scope → milestone → delivery**, with
contacts, files, decisions and payments, and points at the owners of each
thing instead of copying them.

- **Scope versions**: first request, agreement and change requests. Each new
  version is compared with the current agreement (or, for the first
  agreement, with the first request) by `scope_compare` (E30); the
  comparison id, counts and questions are kept with the version. Adding the
  first agreement moves an opportunity or proposal to *agreed*.
- **Milestones**: title, due date, amount and deliverables. A deliverable
  can point at a delivery package (E33); its *ready* state is read from the
  package every time, never copied.
- **Contacts** (name, role, e-mail, optional People's Hoard id), **files**
  (path and role), **decisions** (date, text) and **payments** (label,
  expected amount, due date, optional Ledger reference). Nothing is read from
  or written to People or Ledger here.
- **Status** (computed): next milestone, overdue milestones, deliverables
  without a ready package, open questions of the last change request,
  milestones done, expected billing, budget.
- **Timeline**: every change is an event.

Records: `DATA_DIR/commissions/<owner>/<id>.json`.

| Surface | Name |
|---|---|
| Chat tool | `commission_office` (create, update, list, show/status, add_scope, add_milestone, update_milestone, link_package, add_contact, add_file, add_decision, add_payment) |
| HTTP (admin) | `GET/POST /api/commissions`, `GET /api/commissions/{id}`, `POST /api/commissions/{id}/{action}` |
| MCP | built-in server `delivery`, tool `commission_office` |
| Studio | Delivery → **Commissions**: list with stage, next due date and overdue count; new commission; stage selector; scope versions with their comparison and questions (link to the comparison); milestones with due date, amount, deliverables and the live ready state of their packages, "Link" a package, "Mark done", add milestone; contacts, files and decisions; timeline |

Not in this cut: proposals (E31), invoices synced with Ledger, People
look-ups, a separate Hoard.

## People and Ledger, read live (follow-up)

- `people_lookup {name}` / `GET /api/commissions/people-lookup?name=`: People's Hoard `find_people` through the Hub; `link_person {id, name, people_id}` stores only the id.
- `reconcile_payments {id}`: Ledger's `list_entries` (read-only) around the expected payments; incoming movements of exactly the amount, scored by date and by client or label in the counterparty or note. Besides exact amounts it offers two or three incoming movements that add up to what is still owed (`kind: combined`) and smaller movements whose counterparty or note names the client or the label (`kind: partial`); movements already recorded for a payment are left out. `confirm_payment {id, label, tx_id | tx_ids, amount?}` adds the movements to `receipts`, keeps `received` and confirms the payment only when they cover it (`status.payments_partly_paid` lists the rest); a movement cannot pay two payments. `link_review {id, milestone, review_id}` files a review desk under a milestone; `status.reviews` gives each desk's milestone, latest and delivered version and open comments, and delivering in the desk adds a `delivery` event to the commission. 
- Studio: Delivery → Commissions → **People and payments**.

## Meetings to requirements and open questions (Hangar E27)

- `list_meetings` / `GET /api/commissions/meetings`: recent Faustus meetings of the owner.
- `import_meeting {id, meeting_id | text, source?, date?}` / `POST /api/commissions/{id}/import_meeting`: each statement is sorted into `decision`, `promise`, `requirement`, `proposal` or `question`, with speaker, timestamp (`[hh:mm:ss] Name: text`) and line. Conditional wording (podríamos, se podría, quizá, could, maybe, what if) is always a proposal. Stored as `meetings[]` (R1, R2…); nothing enters the scope.
- `promote_meeting_item {id, meeting, n, as: decision|requirement|milestone, force?}`: a decision keeps its speaker; a requirement becomes a change version (current text plus the statement, compared with the agreement); a promise can become a milestone. The origin (`R1#4 Luis 00:04:02 · source`) is kept. Proposals and questions answer `office.not_firm` unless `force`.
- Status adds `meeting_items_pending` and `meeting_open_questions`.
- Studio: Delivery → Commissions → **Meetings**.
