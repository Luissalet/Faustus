---
name: hoard-study-cards
description: Flashcards from what the user read, heard or discussed (Borges passages, Links pages, Scribe transcripts, past chats) into Hypatia, and a spaced-repetition quiz. Use when the user says "hazme tarjetas de", "quiero memorizar", "tarjetas de la reunión", "examíname", "pregúntame", "repasemos".
version: 1.1.0
tags: [hoards, study, flashcards, hypatia, borges, links, scribe, quiz]
category: research
status: published
source: imported
---

## When to Use

Two moments: the user wants cards made from material they own (read, heard in a recorded session, or discussed in a past chat), or wants to be quizzed on what is due. Skip it for general knowledge.

## Procedure

Plugin tools are exposed as `mcp__<connector>__<name>`; one `lookup_tools`
call naming all of them (`library_search`, `library_read`, `read_link`,
`scribe_sessions`, `cards_suggest`, `cards_suggest_accept`, `cards_search`,
`cards_add`, `cards_due`, `card_review`, `cards_stats`) loads their schemas.

**Making cards**

1. **Get the material from the user's own sources**:
   - read: `library_search` (Borges) with the topic and, for the best hits,
     `library_read` of the passage; or `read_link` (Links) for a saved page;
     or the text the user pasted.
   - heard: "de la reunión / de lo que hablamos ayer" → `scribe_sessions`
     (Scribe) to find the session, then `cards_suggest` with
     `source: {kind: "scribe", session_id}` (or `since`/`until`), which
     drafts cards from the transcript with a local model and returns them
     as `drafts` for the user to approve. If it returns `material` and no
     drafts (no model available), write the cards yourself from that text.
   - discussed: "de lo que aprendí hoy / de lo que decidimos" →
     `library_search` with `source: "faustus"` (Borges indexes the user's
     own chats) and `library_read` of the turns that matter.
   Never write a card from general knowledge alone: every card cites a
   source the tool returned (file and page or section, URL, session and
   time, or chat title and turn).
2. **Check what already exists**: `cards_search` with the topic; do not
   duplicate a front that is already there (the app dedupes exact fronts,
   not rephrasings).
3. **Write the cards**: one fact per card; the front is a question with
   ONE unambiguous answer; the back is short (a phrase, a number, a
   name); tags from the topic; `source` on every card. Between four and
   twelve cards per request unless the user says how many. Put them in
   the deck the user named, or a deck named after the topic. Drafts from
   `cards_suggest` are shown to the user first; add only the ones they
   accept, with `cards_suggest_accept` (or `cards_add`).
4. **Add them** with a single call, then tell the user how many were
   added and how many already existed, and offer to quiz them now.

**Quizzing**

1. `cards_due` for the deck (or all), `limit` 10. If nothing is due, say
   so with the next due date from `cards_stats` and offer to add cards.
2. Ask ONE front, in the user's language, and stop the turn: never show
   the back, never ask two at once.
3. When the user answers, compare with the back yourself and call
   `card_review` once, with the front you showed (and the id), grading
   strictly: again for wrong, blank, "no me acuerdo" or an answer about
   something else (a true sentence about another topic is still wrong);
   hard for partly right; good for right; easy only for instant and
   complete. Then say what the back says and ask the next front.
4. "Para", "déjalo", "siguiente sin responder" ends or skips: skipping is
   `card_review` with again only if the user says they did not know it;
   otherwise leave the card unreviewed.
5. Close the session with `cards_stats`: reviewed today, due left, streak.

## Pitfalls

- Cards from your own knowledge, or fronts with several right answers.
- Adding `cards_suggest` drafts without showing them to the user first.
- Revealing the back before the user answered, or grading an answer the
  user did not give.
- Grading on the ordinal ("tarjeta 1") instead of the front you showed.
- Asking several cards in one message.
- Reading the apps' data folders or databases with the shell.

## Verification

- Every added card's `source` names something a tool returned this turn.
- Every `card_review` follows an answer the user wrote in this
  conversation, and its front is the one shown in the previous turn.
- The count reported ("añadidas / ya existían") matches the add call's
  result.
