# Freshness battery — 2026-09-18

Automatic check that Faustus searches the web for time-sensitive questions instead of refusing or asking permission, and does NOT search for timeless control questions. See docs/evals/freshness.md. Run live on the owner's machine (local 27B model, agent mode, web toggle untouched) right after the "search first" change.

| # | Question | Control | Searched | Refused | Sources | Seconds | Verdict |
|---|----------|---------|----------|---------|---------|---------|---------|
| 1 | ¿Ganó el Real Madrid su último partido? | no | yes | no | 5 | 51.2 | PASS |
| 2 | Who won the last Champions League final? | no | yes | no | 5 | 76.9 | PASS |
| 3 | What's the weather like in Madrid right now? | no | yes | no | 5 | 66.4 | PASS |
| 4 | ¿Qué tiempo hace hoy en Buenos Aires? | no | yes | no | 5 | 73.1 | PASS |
| 5 | What is the current price of Bitcoin? | no | yes | no | 5 | 68.5 | PASS |
| 6 | ¿Cuál es el precio actual del bitcoin? | no | yes | no | 5 | 74.1 | PASS |
| 7 | What is the latest version of the Linux kernel? | no | yes | no | 5 | 75.7 | PASS |
| 8 | ¿Cuál es la última versión de Python? | no | no | no | 0 | 44.1 | FAIL (not searched) |
| 9 | Who is the current head coach of Manchester United? | no | yes | no | 5 | 58.0 | PASS |
| 10 | ¿Quién es el entrenador actual del FC Barcelona? | no | yes | no | 5 | 73.1 | PASS |
| 11 | What's in the news today about artificial intelligence? | no | no | no | 0 | 69.0 | FAIL (not searched) |
| 12 | Dame las noticias de hoy sobre inteligencia artificial | no | yes | no | 5 | 120.6 | PASS |
| 13 | How do I sort a list in Python? | yes | no | no | 0 | 64.5 | PASS |
| 14 | ¿Qué es una derivada en matemáticas? | yes | no | no | 0 | 54.5 | PASS |
| 15 | Explain how a hash map works. | yes | no | no | 0 | 62.0 | PASS |
| 16 | Escribe una función que sume dos números en Python. | yes | no | no | 0 | 60.1 | PASS |

**14/16 passed.**

Notes from the server log: the two failures are the model's, not the plumbing — for #8 it answered from memory ("3.13.x, if you want I can check") with the web tools available; for #11 it first wrote "I don't have live web access", then corrected itself ("I do have web access after all") and searched in round 2, after the battery's stream had already been judged. Three questions started with a round that was only the context separator; the new nudge turned each into a tool call on the next round. Every control question stayed offline.
