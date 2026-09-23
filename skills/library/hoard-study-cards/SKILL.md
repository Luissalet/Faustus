---
name: hoard-study-cards
description: Flashcards from what the user has read, and a quiz on them, with the Hoard plugins: library passages (Borges) or saved pages (Links) become cards in Hypatia; "examíname" runs a spaced-repetition session in the chat. Use when the user says "hazme tarjetas de", "quiero memorizar", "prepárame el examen de", "examíname", "pregúntame", "repasemos".
version: 1.0.0
tags: [hoards, study, flashcards, hypatia, borges, links, quiz]
category: research
status: published
source: imported
---

## When to Use

Two moments: the user wants cards made from material they own, or wants to be quizzed on what is due. Skip it for a general knowledge question that is not about their material.

## Procedure

Plugin tools are exposed as `mcp__<connector>__<name>`; one `lookup_tools`
call naming all of them (`library_search`, `library_read`, `read_link`,
`cards_search`, `cards_add`, `cards_due`, `card_review`, `cards_stats`)
loads their schemas.

**Making cards**

1. **Get the material from the user's own sources**: `library_search`
   (Borges) with the topic and, for the best hits, `library_read` of the
   passage; or `read_link` (Links) for a saved page; or the text the user
   pasted. Never write a card from general knowledge alone: every card
   cites a source the tool returned (file and page or section, or the
   URL).
2. **Check what already exists**: `cards_search` with the topic; do not
   duplicate a front that is already there (the app dedupes exact fronts,
   not rephrasings).
3. **Write the cards**: one fact per card; the front is a question with
   ONE unambiguous answer; the back is short (a phrase, a number, a
   name); tags from the topic; `source` on every card. Between four and
   twelve cards per request unless the user says how many. Put them in
   the deck the user named, or a deck named after the topic.
4. **Add them** with a single `cards_add`, then tell the user how many
   were added and how many already existed, and offer to quiz them now.

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
- Revealing the back before the user answered, or grading an answer the
  user did not give.
- Grading on the ordinal ("tarjeta 1") instead of the front you showed.
- Asking several cards in one message.
- Reading the apps' data folders or databases with the shell.

## Verification

- Every added card's `source` names something a tool returned this turn.
- Every `card_review` follows an answer the user wrote in this
  conversation, and its front is the one shown in the previous turn.
- The count reported ("añadidas / ya existían") matches `cards_add`'s
  result.
