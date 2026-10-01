"""The dispatch loop: one agent call per disruption, manual function calling, no framework.

Carried over from agent-starter/AGENTS.md on purpose:
  - the retry/fallback ladder (429/503 back off 2s/5s/10s, then fail over) rather than a
    reinvented one;
  - echo `call.id` on every FunctionResponse -- `Part.from_function_response()` has no
    `id` parameter, so build the Part yourself;
  - append the model turn verbatim, or `thought_signature` is lost and Gemini 3 rejects
    the next request;
  - the function-response turn is `role="user"`, not `role="tool"`;
  - `response.function_calls` is `None`, not `[]`, when there are none;
  - tool errors go back to the model as text instead of raising.

Quota: the caller only invokes this when a disruption is actually announced, and
LLM_REQUEST_BUDGET caps the damage if that ever stops being true.

Everything this module reports goes through `_say`. With no `emit` callback it prints,
which is the CLI's behaviour and is covered by a stub-client test asserting the exact
lines; a UI passes `emit` instead and gets the same events as data to render.
"""
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from tools import PENDING_PREFIX

load_dotenv()

MODEL = os.getenv("MODEL", "gemini-3.6-flash")
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip()]
MODELS = list(dict.fromkeys([MODEL] + FALLBACK_MODELS))
RETRY_DELAYS = (2, 5, 10)
RETRY_CODES = (429, 503)
MAX_TURNS = 6                       # hard cap on requests spent on one disruption
REQUEST_BUDGET = int(os.getenv("LLM_REQUEST_BUDGET", "8"))
REQUESTS_USED = 0
BUDGET_SPENT = False


def budget_left() -> int:
    return REQUEST_BUDGET - REQUESTS_USED


def budget_message(reason: str) -> str:
    return (f"  !! request budget of {REQUEST_BUDGET} spent ({REQUESTS_USED} used): {reason}. "
            f"Raise LLM_REQUEST_BUDGET to allow more, or accept the scripted plan instead.")


def _say(emit, kind: str, text: str) -> None:
    """Report one thing. `emit` is None on the CLI, where that means print.

    Two deliberately different shapes: events the model produced (a tool call, its result,
    the final message) are split by kind so a UI can label them, while the operator-facing
    lines (request counter, retries, budget) are passed through whole. Both default to the
    exact text the CLI printed before, with no added or removed characters.
    """
    if emit is None:
        print(text)
    else:
        emit(kind, text)


SYSTEM_PROMPT = """You are a travel disruption agent. A traveller's booked itinerary has just been disrupted \
and you must get them to their destination.

Do this:
1. Call get_itinerary to see the plan, the constraints and the preferences.
2. Call analyse_impact for the disrupted leg, BEFORE any call to find_alternatives. It tells you which \
later legs break, by how much, and how much non-refundable money is at stake.
3. Call find_alternatives for the disrupted leg, and for any leg analyse_impact showed is at risk.
4. For each alternative, judge the whole trip, not the fare. Does the onward connection survive? Is the \
hotel still usable? Is the timed activity still reachable? Add up the new fare plus whatever \
non-refundable booking the option burns. An option that is cheap on the fare and ruins the trip is the \
expensive one.
5. Propose exactly two candidate replans, then call commit_replan once with the one you would actually \
do. It refuses anything that breaks a hard constraint, and it asks the traveller before changing \
anything.

Rules:
- The disruption is already described in the message below. Do not call poll_disruptions; there is \
nothing new to learn from it.
- Quote times only exactly as they appear in tool output. Never add, subtract, or convert times. If \
you need a time you were not given, call a tool.
- Only ever propose a flight that find_alternatives returned. If nothing works, say so plainly instead \
of inventing an option. Never write a flight number or a time you have not seen in tool output.
- Hard constraints (connections, check-in, booked activity times) must hold. If you must break one, name it.
- Non-refundable bookings are already paid for. Weigh the wasted cost, not just the new fare.
- The cheapest fare is often not the cheapest outcome. Check what each option actually costs.
- Be brief. Two candidates, a few sentences each."""


def generate(client, config, history, model, emit=None):
    """One request to one model, retrying transient errors. None if it is unusable."""
    global REQUESTS_USED, BUDGET_SPENT
    for attempt in range(len(RETRY_DELAYS) + 1):
        if BUDGET_SPENT:
            return None
        try:
            REQUESTS_USED += 1
            _say(emit, "note", f"  [llm {REQUESTS_USED}/{REQUEST_BUDGET}] {model}")
            if REQUESTS_USED >= REQUEST_BUDGET:
                BUDGET_SPENT = True
                _say(emit, "note", budget_message(f"stopping after {model}"))
            return client.models.generate_content(model=model, contents=history, config=config)
        except errors.APIError as exc:
            if exc.code not in RETRY_CODES:
                _say(emit, "note", f"  !! {model}: {exc}")
                return None
            if attempt == len(RETRY_DELAYS):
                break
            _say(emit, "note", f"  .. {model} error {exc.code}; retry {attempt + 1}/{len(RETRY_DELAYS)} in {RETRY_DELAYS[attempt]}s")
            time.sleep(RETRY_DELAYS[attempt])
        except Exception as exc:
            _say(emit, "note", f"  !! {model}: {type(exc).__name__}: {exc}")
            return None
    return None


def _unreachable(emit) -> None:
    """Say why there is no answer. One message, one reason, no further retries."""
    if BUDGET_SPENT:
        _say(emit, "note", "\nAgent: (out of request budget - the disruption is still on the "
                           "board, nothing was committed)")
    else:
        _say(emit, "note", "Agent: (model unreachable - the disruption is still on the board)")


def handle_disruption(client, config, impls, world, events, emit=None) -> None:
    """Spend one agent call on a freshly announced disruption."""
    brief = "\n".join(f"- {e.summary} (leg {e.leg_id}, +{e.minutes} min)" for e in events)
    question = (f"New disruption just announced at {world.now:%Y-%m-%d %H:%M} UTC:\n{brief}\n\n"
                f"Call analyse_impact for the affected leg first, then find_alternatives, then "
                f"commit_replan with the option you would actually choose.")
    history = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]

    for _ in range(MAX_TURNS):
        for model in MODELS:
            if BUDGET_SPENT:
                break
            response = generate(client, config, history, model, emit)
            if response is not None:
                break
        else:
            history.pop()  # keep turns alternating, or the next call is rejected
            _unreachable(emit)
            return
        if BUDGET_SPENT:
            history.pop()
            _unreachable(emit)
            return
        history.append(response.candidates[0].content)  # verbatim: keeps thought signatures
        calls = response.function_calls or []
        if not calls:
            _say(emit, "agent", f"\nAgent: {response.text}\n")
            return
        parts, pending = [], False
        for call in calls:
            _say(emit, "call", f"  -> {call.name}({call.args})")
            try:
                result = impls[call.name](**(call.args or {}))
            except Exception as exc:
                result = f"error: {exc}"  # the model sees it and can retry
            for line in str(result).splitlines():
                _say(emit, "result", f"  <- {line}")
            if str(result).startswith(PENDING_PREFIX):
                pending = True
            parts.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response={"result": result})))
        history.append(types.Content(role="user", parts=parts))
        # A UI is waiting on a human decision. Stop rather than let the model read
        # "not decided yet" as a failure and spend more requests proposing alternatives.
        if pending:
            return
    _say(emit, "note", f"Agent: (no answer after {MAX_TURNS} tool turns)")


def make_client_and_config(tools) -> tuple:
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY missing - see .env.example")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        tools=tools, system_instruction=SYSTEM_PROMPT,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    return client, config