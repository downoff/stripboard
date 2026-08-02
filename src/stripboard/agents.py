"""The two agents that do actual judgement.

Everything mechanical lives in parser.py. These two exist because they need
reading comprehension:

  breakdown_scene  — what physically has to be on set for this scene, and the exact
                     line that proves it
  find_continuity  — what contradicts what across the whole script

Both run on Gemini. Neither passes `temperature` — Google deprecated and ignores it
from 3.6 Flash onward, so determinism is requested in the system instruction, which
is the supported route. (Learned the hard way: an SDK accepting a parameter says
nothing about the model honouring it.)
"""

from __future__ import annotations

import json
import os

from google import genai
from google.genai import types

from stripboard.parser import Scene

MODEL = os.environ.get("STRIPBOARD_MODEL", "gemini-3.6-flash")

CATEGORIES = [
    "CAST", "BACKGROUND", "PROP", "WARDROBE", "VEHICLE",
    "STUNT", "VFX", "SFX", "ANIMAL", "SET_DRESSING", "SPECIAL_EQUIPMENT",
]

_GROUNDING = """
You are a 1st Assistant Director breaking down a screenplay for production.

THE ONE RULE THAT MATTERS: every element you return must carry `quote`, copied
CHARACTER-FOR-CHARACTER from the scene text. One contiguous passage, at most 25
words. If you cannot quote it, you may not report it. Quotes are verified
programmatically against the source and any element whose quote does not appear
verbatim is DISCARDED — inventing one loses the element, it does not sneak it past.

Do not infer. "She drives" implies a vehicle only if a vehicle is written. An
unnamed extra is BACKGROUND, not CAST. Speaking or named characters are CAST.

DETERMINISM REQUIREMENTS:
- The same scene must produce the same breakdown every run.
- Do not paraphrase, reorder or re-word between runs.
- Take the most literal reading. Never embellish.
"""

_ELEMENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "elements": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "category": {"type": "STRING", "enum": CATEGORIES},
                    "element": {"type": "STRING", "description": "Canonical name, UPPER CASE"},
                    "quote": {"type": "STRING", "description": "Verbatim from the scene, <=25 words"},
                    "confidence": {"type": "NUMBER"},
                },
                "required": ["category", "element", "quote", "confidence"],
            },
        }
    },
    "required": ["elements"],
}

_CONTINUITY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "issues": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "type": {
                        "type": "STRING",
                        "enum": [
                            "PROPERTY_MISMATCH",
                            "TIME_OF_DAY_CONTRADICTION",
                            "PROP_USED_BEFORE_ESTABLISHED",
                            "WARDROBE_CONTINUITY",
                            "LOCATION_INCONSISTENCY",
                            "OTHER",
                        ],
                    },
                    "severity": {"type": "STRING", "enum": ["HIGH", "MEDIUM", "LOW"]},
                    "summary": {"type": "STRING"},
                    "scenes": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "evidence": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
                        "description": "Verbatim quotes, one per scene involved",
                    },
                },
                "required": ["type", "severity", "summary", "scenes", "evidence"],
            },
        }
    },
    "required": ["issues"],
}


def _client() -> genai.Client:
    if os.environ.get("VERTEX_API_KEY"):
        return genai.Client(vertexai=True, api_key=os.environ["VERTEX_API_KEY"])
    if os.environ.get("GOOGLE_API_KEY"):
        return genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    raise RuntimeError("Set GOOGLE_API_KEY or VERTEX_API_KEY")


def _call(
    client: genai.Client,
    prompt: str,
    schema: dict,
    system: str,
    *,
    max_tokens: int = 8192,
    thinking: str = "high",
) -> dict:
    """One structured call, with the Gemini 3 output-budget trap handled explicitly.

    ⚠️ Thinking tokens are charged against `max_output_tokens`. At thinking_level=high
    on a whole-script prompt, an 8k cap is spent thinking and the JSON comes back
    TRUNCATED — `json.loads` then dies on a partial object. Bit us on the first real
    run of the continuity pass. So: give reasoning passes real headroom, and when the
    budget is still hit, say so loudly instead of returning a silently empty result.
    """
    resp = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=thinking),
        ),
    )

    finish = None
    if resp.candidates:
        finish = getattr(resp.candidates[0], "finish_reason", None)

    if not resp.text:
        print(f"      ! empty response (finish_reason={finish}) — raise max_tokens")
        return {}

    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        print(
            f"      ! truncated JSON at {len(resp.text)} chars "
            f"(finish_reason={finish}) — raise max_tokens"
        )
        return {}


def _verify_quote(quote: str, source: str) -> bool:
    """Substring check with whitespace normalised — the promise the README makes."""
    norm = lambda s: " ".join(s.split()).lower()
    return norm(quote) in norm(source)


def breakdown_scene(client: genai.Client, scene: Scene) -> list[dict]:
    """One scene → verified elements. Unverifiable quotes are dropped, not kept."""
    out = _call(
        client,
        f"Break down this scene.\n\n{scene.text}",
        _ELEMENT_SCHEMA,
        _GROUNDING,
    )
    verified, discarded = [], 0
    for el in out.get("elements", []):
        if _verify_quote(el.get("quote", ""), scene.text):
            el["scene_number"] = scene.number
            el["page"] = scene.page
            el["int_ext"] = scene.int_ext
            el["day_night"] = scene.day_night
            el["location"] = scene.location
            verified.append(el)
        else:
            discarded += 1
    if discarded:
        print(f"      scene {scene.number}: dropped {discarded} element(s) — quote not verbatim")
    return verified


def find_continuity(client: genai.Client, scenes: list[Scene], elements: list[dict]) -> list[dict]:
    """Whole-script pass. This is the part a human script supervisor is paid for."""
    scene_block = "\n\n".join(f"[SCENE {s.number} | p{s.page}]\n{s.text}" for s in scenes)
    element_block = "\n".join(
        f"  sc{e['scene_number']} p{e['page']} {e['category']}: {e['element']}" for e in elements
    )
    system = (
        _GROUNDING
        + "\n\nYou are now reviewing the WHOLE script for continuity errors: the same "
        "thing described two different ways, a slug line that contradicts its own "
        "action, a prop used before it is established, wardrobe that changes with no "
        "beat explaining it.\n"
        "Report ONLY contradictions you can prove with two verbatim quotes. Do not "
        "report stylistic choices, deliberate ambiguity, or anything you are unsure "
        "about — a false alarm costs a production more than a missed nitpick."
    )
    out = _call(
        client,
        f"ELEMENTS FOUND:\n{element_block}\n\nFULL SCRIPT:\n{scene_block}",
        _CONTINUITY_SCHEMA,
        system,
        # whole-script reasoning at thinking=high needs real headroom — see _call
        max_tokens=32768,
    )
    return out.get("issues", [])
