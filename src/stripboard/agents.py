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
import re

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


def build_candidates(elements: list[dict]) -> str:
    """Compute the pairs worth checking, instead of hoping the model sweeps for them.

    First version of the continuity pass found the two loudest errors and stopped —
    2/4 recall. The miss was structural, not stylistic: an open-ended "find
    contradictions" prompt has no obligation to be exhaustive, so it satisfices.

    The element table already knows where to look. Any category that carries more
    than one distinct name across scenes is a candidate for the-same-thing-described-
    two-ways, and any recurring item is a candidate for used-before-established. So
    enumerate those here and hand the model a checklist it has to answer, rather than
    an invitation to browse.
    """
    by_cat: dict[str, list[dict]] = {}
    for e in elements:
        by_cat.setdefault(e["category"], []).append(e)

    lines: list[str] = []

    # (a) same category, more than one distinct name → possible mismatch
    for cat, els in sorted(by_cat.items()):
        names = sorted({e["element"] for e in els})
        if len(names) < 2:
            continue
        detail = "; ".join(
            f"{n} (sc{','.join(sorted({x['scene_number'] for x in els if x['element'] == n}))})"
            for n in names
        )
        lines.append(f"  [{cat}] do any of these name the SAME physical thing? {detail}")

    # (b) anything appearing in 2+ scenes → possible establishment-order problem
    seen: dict[tuple[str, str], list[dict]] = {}
    for e in elements:
        seen.setdefault((e["category"], e["element"]), []).append(e)
    for (cat, name), els in sorted(seen.items()):
        if len(els) < 2:
            continue
        order = sorted(els, key=lambda x: x["page"])
        trail = " -> ".join(f"sc{x['scene_number']}(p{x['page']})" for x in order)
        lines.append(
            f"  [{cat}] {name} recurs: {trail}. Is its FIRST appearance an establishing "
            "one, or is it used/known before the audience is shown where it came from?"
        )

    return "\n".join(lines) if lines else "  (no recurring elements)"


_ESTABLISHING = (
    "hands", "handed", "presses", "gives", "gave", "passes", "produces",
    "first time", "never seen", "finds", "found", "discovers", "reveals",
    "pulls out", "takes out", "arrives with", "brings",
)
_USING = (
    "unlocks", "unlocked", "opens", "opened", "uses", "used", "turns",
    "picks up", "holds", "carries", "inserts", "starts",
)


def _mentions(scene_text: str, element: str) -> list[str]:
    """Every sentence in the scene that talks about this element.

    Matched on the full name and on its head noun, because prose refers back to a
    BRASS KEY as "the key". Word-boundary only, so KEY does not match MONKEY.
    """
    name = element.strip()
    head = name.split()[-1] if name.split() else name
    pat = re.compile(rf"\b({re.escape(name)}|{re.escape(head)})\b", re.I)

    # Screenplay action is hard-wrapped at ~65 columns, so one sentence spans
    # several lines. Rejoin each blank-line-delimited paragraph BEFORE splitting on
    # sentence punctuation — otherwise "Rena unlocks the metal" and "FOOTLOCKER at
    # the end of the bed with the BRASS KEY." read as unrelated fragments and the
    # verb never travels with the noun. That was the 2026-08-02 miss.
    out = []
    for para in re.split(r"\n\s*\n", scene_text):
        flat = " ".join(para.split())
        if not flat:
            continue
        for s in re.split(r"(?<=[.!?])\s+", flat):
            if s and pat.search(s):
                out.append(s)
    return out


def _ordering_timelines(scenes: list[Scene], elements: list[dict]) -> str:
    """Build page-ordered timelines for props that look established AFTER use.

    This is the shape the whole-script pass keeps missing. The other three checks
    are "two things contradict"; this one is "one thing in the wrong order", and it
    needs the page sequence in front of it, not buried in a 13-scene prompt.

    Reads the SOURCE SENTENCES, not the stored quote. The breakdown agent stores a
    minimal verbatim span ("with the BRASS KEY.") which is right for citation and
    useless here — the verb that separates establishing from using sits outside it.
    Scanning quotes found nothing on 2026-08-02; scanning prose is the fix.

    The keyword scan is a cheap pre-filter, not the verdict — it decides what is
    worth asking about. The model still has to prove it with two verbatim quotes,
    so a false hit here costs a question, not a false alarm.
    """
    text_by_scene = {s.number: s.text for s in scenes}

    seen: dict[tuple[str, str], list[dict]] = {}
    for e in elements:
        if e["category"] in ("PROP", "VEHICLE", "SPECIAL_EQUIPMENT"):
            seen.setdefault((e["category"], e["element"]), []).append(e)

    blocks: list[str] = []
    for (cat, name), els in sorted(seen.items()):
        if len(els) < 2:
            continue
        # one row per scene, page-ordered, carrying the real prose
        rows = []
        for e in sorted(els, key=lambda x: x["page"]):
            for line in _mentions(text_by_scene.get(e["scene_number"], ""), name):
                rows.append((e["page"], e["scene_number"], line))
        if not rows:
            continue

        low = lambda r: r[2].lower()
        first_use = next((r for r in rows if any(v in low(r) for v in _USING)), None)
        late_est = next(
            (r for r in reversed(rows) if any(v in low(r) for v in _ESTABLISHING)), None
        )
        # only worth asking when something that reads as introducing the object
        # sits LATER in the script than something that reads as using it
        if not (first_use and late_est and late_est[0] > first_use[0]):
            continue
        trail = "\n".join(f"      p{p:<8} sc{sc:<4} {line}" for p, sc, line in rows)
        blocks.append(
            f"  [{cat}] {name}\n{trail}\n"
            f"      ^ USED at p{first_use[0]} (sc{first_use[1]}), but what reads as its "
            f"INTRODUCTION is at p{late_est[0]} (sc{late_est[1]}). Real ordering error?"
        )

    return "\n\n".join(blocks) if blocks else ""


def find_ordering_issues(
    client: genai.Client, scenes: list[Scene], elements: list[dict]
) -> list[dict]:
    """Dedicated establishment-order pass. Skipped entirely when nothing qualifies."""
    timelines = _ordering_timelines(scenes, elements)
    if not timelines:
        return []

    system = (
        _GROUNDING
        + """

You are checking ONE thing: establishment order.

A script establishes an object before the audience sees it used. An error is when
an object is used, opened, unlocked or treated as already-owned EARLIER in the
script than the beat that introduces it — the moment it is handed over, found, or
described as held for the first time.

NOT an error: an object simply appearing in several scenes. NOT an error: an
object present from the start with no introduction beat at all — that is a normal
screenwriting choice. The error is specifically an INTRODUCTION that arrives after
a USE.

Report only what you can prove with two verbatim quotes: the earlier use, and the
later introduction. If the earlier line is not really a use, or the later line is
not really an introduction, report nothing."""
    )
    out = _call(
        client,
        f"PAGE-ORDERED TIMELINES:\n\n{timelines}\n\n"
        f"FULL SCRIPT:\n" + "\n\n".join(f"[SCENE {s.number} | p{s.page}]\n{s.text}" for s in scenes),
        _CONTINUITY_SCHEMA,
        system,
        max_tokens=16384,
    )
    return out.get("issues", [])


def find_continuity(client: genai.Client, scenes: list[Scene], elements: list[dict]) -> list[dict]:
    """Whole-script pass. This is the part a human script supervisor is paid for."""
    scene_block = "\n\n".join(f"[SCENE {s.number} | p{s.page}]\n{s.text}" for s in scenes)
    element_block = "\n".join(
        f"  sc{e['scene_number']} p{e['page']} {e['category']}: {e['element']}" for e in elements
    )
    candidate_block = build_candidates(elements)
    system = (
        _GROUNDING
        + """

You are now the SCRIPT SUPERVISOR reviewing the whole script for continuity.

Work through these four checks IN ORDER and do not stop early. Finding one error
does not excuse you from the rest — a real pass is exhaustive.

  1. PROPERTY_MISMATCH — the same physical thing described two different ways
     (a '67 vs a '68 of the same car, a colour that changes, a count that changes).
  2. TIME_OF_DAY_CONTRADICTION — a slug line whose own action text contradicts it.
  3. PROP_USED_BEFORE_ESTABLISHED — an object is used, opened, recognised or
     discussed as known before the audience is shown where it came from.
  4. WARDROBE_CONTINUITY — a character's clothing changes between scenes with no
     beat in between that would explain the change.

The CHECKLIST below was computed from the element table: every category carrying
more than one name, and every item that recurs across scenes. Answer each line —
either it is a real contradiction (report it) or it is not (stay silent on it).

Report ONLY what you can prove with two verbatim quotes. Do not report stylistic
choices, deliberate ambiguity, or anything you are unsure about — a false alarm
costs a production more than a missed nitpick, and precision is what gets this
tool kept or dropped."""
    )
    out = _call(
        client,
        f"CHECKLIST (computed — answer every line):\n{candidate_block}\n\n"
        f"ELEMENTS FOUND:\n{element_block}\n\nFULL SCRIPT:\n{scene_block}",
        _CONTINUITY_SCHEMA,
        system,
        # whole-script reasoning at thinking=high needs real headroom — see _call
        max_tokens=32768,
    )
    return out.get("issues", [])
