---
name: hoard-study-cards
description: Study with Hypatia (Exam Coach): questions from what the user read, heard or discussed (Borges, Links, Scribe, past chats, the subject's own PDFs), quizzes and mock exams with honest grading. Use when the user says "hazme preguntas de", "quiero memorizar", "examíname", "pregúntame", "repasemos", "simulacro".
version: 2.0.0
tags: [hoards, study, flashcards, hypatia, exam-coach, borges, links, scribe, quiz, estudio, estudiar, tarjetas, repaso, repasar, preguntas, examen, examinar, memorizar, simulacro, examíname, pregúntame, tarjeta]
category: research
status: published
source: imported
---

## When to Use

The user wants study questions made from material they own, wants to be quizzed on what is due, or wants a mock exam on their weak topics. Skip it for general knowledge.

## Procedure

Plugin tools are exposed as `mcp__<connector>__<name>`; one `lookup_tools`
call naming all of them (`library_search`, `library_read`, `read_link`,
`scribe_sessions`, `subjects_list`, `topics_list`, `questions_search`,
`questions_suggest`, `questions_suggest_accept`, `questions_add`,
`notebook_search`, `cards_due`, `card_review`, `answer_grade`,
`weak_topics`, `exam_mock`, `study_stats`) loads their schemas.

In Hypatia a subject (asignatura) holds topics (temas) and questions; a
flashcard is a DESARROLLO question (prompt = front, modelAnswer = back).
`subjects_list` gives the exact subject names to pass.

**Making questions**

1. **Get the material from the user's own sources**:
   - the subject's PDFs: `notebook_search` (subject, query) returns cited
     passages with file and page.
   - read elsewhere: `library_search` (Borges) and `library_read` of the
     best hits; or `read_link` (Links) for a saved page; or pasted text.
   - heard: "de la reunión / de la clase de ayer" → `scribe_sessions`
     (Scribe) to find the session, then `questions_suggest` with
     `source: {kind: "scribe", session_id}` (or `since`/`until`).
   - discussed: `library_search` with `source: "faustus"` (Borges indexes
     the user's chats) and `library_read` of the turns that matter.
   `questions_suggest` also takes `{kind: "text"}` or `{kind: "sources",
   subject, topic?, query?}` and drafts with a local model; with no model
   it returns `material`: write the questions yourself from it.
   Never write a question from general knowledge alone: every one cites a
   source a tool returned (file and page, URL, session and time, or chat).
2. **Check what exists**: `questions_search` with the topic; do not add a
   rephrasing of a question that is already there.
3. **Write them**: one fact per question, a prompt with ONE unambiguous
   answer, a short modelAnswer, the source in `explanation`. Four to
   twelve per request unless the user says how many. Show drafts first;
   save only what the user accepts with `questions_suggest_accept` (or
   `questions_add`), in the subject and topic the user named.
4. Report how many were added and how many already existed, and offer a
   quiz.

**Quizzing**

1. `cards_due` (subject/topic, `limit` 10). Nothing due → say so with
   `study_stats` and offer `weak_topics` or new questions.
2. Ask ONE prompt, in the user's language, and stop the turn: never show
   the answer, never ask two at once. For TEST questions show the options.
3. When the user answers, call `answer_grade` (it scores TEST and fill-in
   exactly and gives a verdict for open answers), then `card_review` once
   with the prompt you showed (and the id): again for wrong, blank, "no me
   acuerdo" or an answer about something else; hard for partly right;
   good for right; easy only for instant and complete. Then say the model
   answer and ask the next one.
4. "Para", "déjalo" ends; a skip is `again` only if the user says they
   did not know it.
5. Close with `study_stats`: reviewed today, due left, streak.

**Mock exam**: `weak_topics` → `exam_mock` (subject, count, focus weak);
it saves a "Simulacro" the user can also open in the Exam Coach app.
Quiz it with the same rules, then summarise per topic.

## Pitfalls

- Questions from your own knowledge, or prompts with several right answers.
- Saving drafts without showing them to the user first.
- Revealing the answer before the user answered, or grading an answer the
  user did not give.
- Grading on the ordinal ("pregunta 1") instead of the prompt you showed.
- Asking several questions in one message.
- Reading the apps' data folders or databases with the shell.

## Verification

- Every saved question names a source some tool returned this turn.
- Every `card_review` follows an answer the user wrote in this
  conversation, and its prompt is the one shown in the previous turn.
- The count reported ("añadidas / ya existían") matches the save call.
