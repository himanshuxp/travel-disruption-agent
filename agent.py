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
"""
import os
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

MODEL = os.getenv("MODEL", "gemini-3.6-flash")
FALLBACK_MODELS = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip()]
MODELS = list(dict.fromkeys([MODEL] + FALLBACK_MODELS))
RETRY_DELAYS = (2, 5, 10)
RETRY_CODES = (429, 503)
MAX_TURNS = 6                       # hard cap on requests spent on one disruption
REQUEST_BUDGET = int(os.getenv("LLM_REQUEST_BUDGET", "20"))
REQUESTS_USED = 0

SYSTEM_PROMPT = """You are a travel disruption agent. A traveller's booked itinerary has just been disrupted \
and you must get them to their destination.

Do this:
1. Call get_itinerary to see the plan, the constraints and the preferences.
2. Call find_alternatives for the disrupted leg, and for any later leg you would have to rebook.
3. Propose exactly two candidate replans. For each: the flights, the total fare delta, and what it \
costs in time.

Rules:
- The disruption is already described in the message below. Do not call poll_disruptions; there is \
nothing new to learn from it.
- Only ever propose a flight that find_alternatives returned. If nothing works, say so plainly instead \
of inventing an option.
- Copy flight numbers and departure/arrival times exactly as the tools printed them. Never recompute, \
convert or restate a time from memory -- you will get it wrong.
- Hard constraints (connections, check-in, booked activity times) must hold. If you must break one, name it.
- Non-refundable bookings are already paid for. Weigh the wasted cost, not just the new fare.
- The cheapest fare is often not the cheapest outcome. Check what each option actually costs.
- Be brief. Two candidates, a few sentences each."""


def budget_left() -> int:
    return REQUEST_BUDGET - REQUESTS_USED


def generate(client, config, history, model):
    """One request to one model, retrying transient errors. None if it is unusable."""
    global REQUESTS_USED
    for attempt in range(len(RETRY_DELAYS) + 1):
        if not budget_left():
            print(f"  !! request budget of {REQUEST_BUDGET} exhausted; stopping")
            return None
        try:
            REQUESTS_USED += 1
            print(f"  [llm {REQUESTS_USED}/{REQUEST_BUDGET}] {model}")
            return client.models.generate_content(model=model, contents=history, config=config)
        except errors.APIError as exc:
            if exc.code not in RETRY_CODES:
                print(f"  !! {model}: {exc}")
                return None
            if attempt == len(RETRY_DELAYS):
                break
            print(f"  .. {model} error {exc.code}; retry {attempt + 1}/{len(RETRY_DELAYS)} in {RETRY_DELAYS[attempt]}s")
            time.sleep(RETRY_DELAYS[attempt])
        except Exception as exc:
            print(f"  !! {model}: {type(exc).__name__}: {exc}")
            return None
    return None


def handle_disruption(client, config, impls, world, events) -> None:
    """Spend one agent call on a freshly announced disruption."""
    brief = "\n".join(f"- {e.summary} (leg {e.leg_id}, +{e.minutes} min)" for e in events)
    question = (f"New disruption just announced at {world.now:%Y-%m-%d %H:%M} UTC:\n{brief}\n\n"
                f"Work out what it breaks and propose two replans.")
    history = [types.Content(role="user", parts=[types.Part.from_text(text=question)])]

    for _ in range(MAX_TURNS):
        for model in MODELS:
            response = generate(client, config, history, model)
            if response is not None:
                break
        else:
            history.pop()  # keep turns alternating, or the next call is rejected
            print("Agent: (model unreachable - the disruption is still on the board)")
            return
        history.append(response.candidates[0].content)  # verbatim: keeps thought signatures
        calls = response.function_calls or []
        if not calls:
            print(f"\nAgent: {response.text}\n")
            return
        parts = []
        for call in calls:
            print(f"  -> {call.name}({call.args})")
            try:
                result = impls[call.name](**(call.args or {}))
            except Exception as exc:
                result = f"error: {exc}"  # the model sees it and can retry
            for line in str(result).splitlines():
                print(f"  <- {line}")
            parts.append(types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response={"result": result})))
        history.append(types.Content(role="user", parts=parts))
    print(f"Agent: (no answer after {MAX_TURNS} tool turns)")


def make_client_and_config(tools) -> tuple:
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY missing - see .env.example")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.GenerateContentConfig(
        tools=tools, system_instruction=SYSTEM_PROMPT,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    return client, config