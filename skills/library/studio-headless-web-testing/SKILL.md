---
name: studio-headless-web-testing
description: Test a served web page end to end in a headless browser at several sizes and colour schemes, including keyboard focus, form errors, a saved submission checked in the database, a simulated server failure and horizontal overflow, and look at the screenshots. Use when a production order delivers a web page or app and it must be proven to work without opening a visible browser.
version: 1.0.0
category: testing
tags: [studio, playwright, headless, browser, web, testing, e2e, forms, overflow, accessibility, pruebas, navegador-sin-ventana, formulario, movil]
accepts: [web-page]
produces: [responsive-captures]
status: published
source: imported
---

## When to Use

The test station of a web order, after the code and before or alongside `studio-vitruvius-design-critique`. For test structure in larger suites see `e2e-testing`.

- **Receives:** the page served locally (URL and how to start and stop it), the acceptance criteria of the brief, and where submissions are stored.
- **Delivers:** a repeatable test script, a JSON result per run, screenshots of each flow that were looked at, and the list of criteria passed or failed.

## Procedure

1. **Serve** the page on loopback from a script that writes its PID and log; record both. Use a copy of the database made for the test, never the real one.
2. **Browser.** Playwright with headless Chromium (or Brave in headless mode). Never a visible window and never desktop control: the person may be using the screen.
3. **Flows.** One flow per environment: 1440 px light, 390 px light, 390 px dark, all with `reduced_motion="reduce"`. Each flow: the main heading, navigation, every filter or tab (count the items shown), keyboard focus visible on the controls, the form with missing fields (errors shown, nothing sent), the form filled (success message with a reference), and a screenshot at each state.
4. **Selectors by role** (`get_by_role("combobox", name="...")`, `get_by_role("button", name=..., exact=True)`). Text search for a select matched the wrong element.
5. **Persistence.** After a successful submission, read the row from the test database and compare every field with what was typed.
6. **Failure path.** `page.route("**/api/<endpoint>", lambda r: r.fulfill(status=503, ...))`, submit, and check the fields keep their values and the button is enabled again.
7. **Overflow.** At 320, 390, 768, 1024 and 1440 px, `document.documentElement.scrollWidth` must equal `clientWidth`.
8. **Console and network.** Collect console errors and failed requests per flow; a valid flow has none.
9. **Look** at the screenshots of every flow before reporting; a passing script can still show a broken layout.
10. Stop the server you started, unless the order asks to keep it for review (then write its URL, PID and how to stop it).

## Pitfalls

- Testing only the desktop size; small screens break first.
- Checking that a request was sent instead of what was stored.
- Flaky waits: wait for a visible state (`expect(...).to_contain_text`), not a fixed time.
- A 400 for invalid data must be tested too; it proves the server validates, not only the page.

## Verification

- JSON result with every flow passed, timings, and zero console or network errors in valid flows.
- The stored row matches the typed values field by field.
- Screenshots opened for each flow; anything that looks wrong goes back to the code station with size, scheme and state.
