# AGENTS.md

Agent that notices a travel disruption, traces its **blast radius** across a whole itinerary, and
proposes a replan that is actually workable. Simulated world only — nothing is ever booked or paid.

## Stack

Python 3 + `google-genai` + `python-dotenv`; `requirements.txt` is the whole list. Manual function
calling in `agent.py`: no framework, no automatic tool execution, no new dependencies. `unittest`
only, no linter or CI to invent.

## Rules — don't break these

- **A tool is registered in three places**: the `FunctionDeclaration` list, `TOOLS`, and the dict
  `build_tools` returns (`TOOL_IMPLS`). `check_tool_sync()` fails at startup on drift — keep it so.
- **Tools never raise.** Failures return `"error: ..."` text the model can read and react to; `raise` kills the run.
- **The model quotes times exactly as the tools printed them, and never does time arithmetic.** On the
  first live run it invented `BA 0431`'s times (00:15 EDT / 13:15 CEST; world: 22:40 / 11:40) — for a flight it wasn't asked to rebook. Prompted not to, it stopped. That is not enforcement.
- **The model is never the authority on whether a flight exists.** `commit_replan` must reject any leg
  whose flight number, times or cost don't appear verbatim in that leg's `find_alternatives` output. In code, not the prompt. Not optional.
- **Datetimes are aware UTC**, converted only for display. Derive times, never write the same time down twice.
- **`main.py` and the tests must keep working unchanged.** `app.py` is optional and may not touch them;
  the page's own logic lives in `uicore.py`, which has no streamlit import, so tests can drive it without
  running a page. Never call the model at import, on a rerun, or from anywhere but a button.
- **Keep `python main.py --no-llm` working** — zero requests, deterministic scripted replan. If the plumbing is wrong, it shows.
- **Offline tests pass before any live run**: `.venv/bin/python -m unittest discover -s tests -t .`

## Gemini SDK gotchas

Echo `call.id` on every `FunctionResponse` — `Part.from_function_response()` takes no `id`. Append model
turns verbatim or `thought_signature` is lost; the response turn is `role="user"`; `function_calls` is `None`, not `[]`.

## Quota is the design constraint

~20 requests per model per day. `poll_disruptions()` runs in ordinary Python and only a non-empty
result spends one, so an 8-tick demo costs a single agent call. `LLM_REQUEST_BUDGET` (default 8) caps
the damage and prints the counter. **Tests never make live calls.** A burst of 503s/429s once burned 10 requests on one disruption.

## Status

**M0–M2 and M4-core done.** Five tools, `analyse_impact` walking `Trip.legs` deterministically, and
`commit_replan` refusing any flight or time that `find_alternatives` did not return. `app.py` wraps all
of it as a Streamlit page; approval there raises `AwaitingApproval` instead of blocking (uicore.py). 79
offline tests, stdlib only. **Next is M3**: have the model choose between two valid options and justify
it, rather than proposing two for a human to pick.
