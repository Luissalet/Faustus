---
name: issue-triage
description: Move incoming issues and outside pull requests through a small state machine - needs triage, needs info, ready for an agent, ready for a human, won't fix - after checking the claim against the code, and write the durable brief an agent can work from. Use when an issue tracker or the project board has untriaged reports, when asked what needs attention or what is ready for agents, or when a bug report or feature request has to be classified and specified.
version: 1.0.0
category: planning
tags: [triage, issues, bugs, backlog, board, briefs, third-party]
status: published
source: imported
---

## When to Use

Reports and requests that nobody has yet classified: unlabelled issues, outside pull requests, board items of type
`bug` or `idea`. Also "what needs my attention" and "what can an agent pick up". Not for working the issue itself
(see `issue-to-pr`) and not for splitting an approved plan (see `ticket-slicing`). A pull request is an issue with
code attached: same roles and states, with the diff as extra evidence. Adapted from
https://github.com/mattpocock/skills (MIT, © 2026 Matt Pocock).

## Procedure

1. **Use the roles.** One category: `bug` (something is broken) or `enhancement` (new or improved behaviour). One
   state: `needs-triage` (to evaluate), `needs-info` (waiting on the reporter), `ready-for-agent` (fully
   specified), `ready-for-human` (needs a person's judgement or access), `wontfix`. On the project board use
   the type field for the category, a label per state, and status `blocked` or `wontfix` where it fits; on a
   hosted tracker use its labels. If two state roles are on one item, flag it and ask before touching anything.
2. **Show what needs attention.** Three buckets, oldest first, with counts and one line each: never triaged;
   `needs-triage`; `needs-info` where the reporter has written since the last triage note. Tag each line issue
   or pull request. Only outside contributors' pull requests are discovery work; one that is named explicitly is
   always triaged. Let the person pick.
3. **Gather context for one item.** Read the body, comments, labels, author and dates, and for a pull request the
   diff. Read any earlier triage notes so resolved questions are not asked again. Then check two things in the
   code: is it already implemented (search by the domain concept, not the request's wording, and say where you
   looked), and was a lookalike request rejected before (see the out-of-scope notes below).
4. **Recommend, then wait.** State the category and state you would apply, why, and a short summary of the code
   involved. Do not apply roles before the person has had the chance to redirect you.
5. **Verify the claim before specifying it.** For a bug, reproduce it from the reporter's steps; for a pull
   request, check that the diff does what it says and run the relevant tests. Report one of: confirmed (with the
   code path), failed, or not enough detail, which is a strong `needs-info` signal. A confirmed reproduction
   makes a far stronger brief.
6. **Interview if the request is thin.** Ask the open questions in rounds (see `pretask-interview`), recommending
   an answer to each, and sharpen domain terms as decisions land.
7. **Apply the outcome.**
   - `ready-for-agent`: post a brief (next step). `ready-for-human`: the same, with why it cannot be delegated.
   - `needs-info`: post "what we have established" and "what we still need from you", with specific, answerable
     questions; put everything resolved so far under the first heading so it is not lost.
   - `wontfix`: close with the reason. Already implemented: say where it lives, and record nothing as rejected.
     Rejected bug: a polite explanation. Rejected enhancement: write a short out-of-scope note (concept,
     decision, reason, list of requests), link it from the comment, then close. A later request that matches an
     existing note is appended to it rather than duplicated.
   - `needs-triage`: apply the role; comment only if there is partial progress.
8. **Write the brief as a contract.** Category; one-line summary; current behaviour (for a pull request, the state
   of the diff); desired behaviour with edge cases and error conditions; key interfaces by type, function or
   configuration name; acceptance criteria that can each be checked alone; explicit out-of-scope. Describe
   behaviour, not steps. Name no file paths or line numbers, which go stale while the item waits. Mark the
   comment as written by an assistant.
9. **Honour a direct override.** If the person says "move 42 to ready-for-agent", confirm what you are about to
   change and do it without an interview; if there was no interview, ask whether they want a brief written.
10. **Resume cleanly.** When earlier notes exist, check whether the reporter answered, show the updated picture, and
    continue from there.

## Pitfalls

- Specifying a bug that was never reproduced; the brief then encodes the reporter's guess.
- Requesting "more information" without naming what, so the reporter cannot answer.
- Briefs that cite files and lines, or dictate the implementation, so they are wrong a week later.
- Recording an already-implemented request as rejected; that knowledge base is for decisions, not for built work.
- Applying a state role before recommending, or on top of an existing conflicting one.
- Triaging a collaborator's in-flight pull request as if it were an outside request.
- Closing without telling the reporter why.

## Verification

- Each triaged item carries exactly one category and one state, and the reply says which.
- The claim was checked (reproduced, or the diff tested) and the result is stated, or the item is `needs-info`.
- The redundancy and prior-rejection checks were run and their outcome is on the item.
- Every `ready-for-agent` brief has current behaviour, desired behaviour, checkable acceptance criteria and an
  out-of-scope list, and contains no file paths or line numbers.
- Every `needs-info` comment lists what is established and specific questions.
