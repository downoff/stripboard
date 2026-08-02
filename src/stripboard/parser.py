"""Screenplay → scenes. Deterministic, no model involved.

Scene detection and page anchoring are mechanical problems with exact answers, so
they get exact code. Sending them to a model would add cost, latency and a failure
mode for zero benefit — and it would put the page numbers (which every downstream
citation depends on) at the mercy of a generation.

Page maths follows the industry convention: a correctly formatted screenplay page
holds ~55 lines, and 1 page ≈ 1 minute of screen time. `page` is fractional so a
scene starting halfway down page 12 reads 12.5, which is how a 1st AD writes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LINES_PER_PAGE = 55

# INT. / EXT. / INT./EXT. / I/E. — optionally numbered, ending in a time-of-day
_SLUG = re.compile(
    r"^\s*(?:\d+[A-Z]?\s+)?"
    r"(?P<int_ext>INT\.?/EXT\.?|EXT\.?/INT\.?|I/E\.?|INT\.?|EXT\.?)\s+"
    r"(?P<rest>.+?)\s*$"
)

_TIME_TOKENS = (
    "CONTINUOUS", "MOMENTS LATER", "LATER", "SAME",
    "DAWN", "DUSK", "MORNING", "AFTERNOON", "EVENING", "NIGHT", "DAY",
)


@dataclass
class Scene:
    number: str
    int_ext: str
    location: str
    day_night: str
    page: float
    slug: str
    body: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.slug}\n{self.body}".strip()


def _normalise_int_ext(raw: str) -> str:
    r = raw.upper().rstrip(".")
    if "/" in r:
        return "INT/EXT"
    return "INT" if r.startswith("INT") else "EXT"


def _split_location_and_time(rest: str) -> tuple[str, str]:
    """`DINER PARKING LOT - NIGHT` → ('DINER PARKING LOT', 'NIGHT').

    Falls back to UNSPECIFIED rather than guessing, because a wrong day/night on a
    stripboard is a wasted shoot day and a silent wrong answer is worse than a gap.
    """
    upper = rest.upper()
    for token in _TIME_TOKENS:  # longest-first ordering matters: MOMENTS LATER before LATER
        for sep in (" - ", " -- ", " — ", " – "):
            suffix = f"{sep}{token}"
            if upper.endswith(suffix):
                return rest[: -len(suffix)].strip(), token
    # time-of-day present but no dash separator
    for token in _TIME_TOKENS:
        if upper.endswith(f" {token}"):
            return rest[: -(len(token) + 1)].strip(), token
    return rest.strip(), "UNSPECIFIED"


def parse(text: str) -> list[Scene]:
    """Parse screenplay text into scenes with page anchors."""
    # drop a Fountain title page / metadata block if present
    body = text
    if "\n====" in body:
        body = body.split("\n====", 1)[1]

    scenes: list[Scene] = []
    current: Scene | None = None
    line_no = 0

    for raw in body.splitlines():
        line_no += 1
        stripped = raw.strip()

        m = _SLUG.match(stripped) if stripped else None
        # a slug line is upper-case by convention; guard against matching dialogue
        # that happens to start with "Interior..."
        if m and stripped == stripped.upper():
            location, day_night = _split_location_and_time(m.group("rest"))
            # 1-indexed: page 1 starts at 1.0, so 12.5 reads "halfway down page 12"
            page = round(1 + line_no / LINES_PER_PAGE, 3)
            current = Scene(
                number=str(len(scenes) + 1),
                int_ext=_normalise_int_ext(m.group("int_ext")),
                location=location,
                day_night=day_night,
                page=page,
                slug=stripped,
            )
            scenes.append(current)
            continue

        if current is not None:
            current.lines.append(raw)

    for s in scenes:
        s.body = "\n".join(s.lines).strip()

    return scenes


def summarise(scenes: list[Scene]) -> str:
    """One line per scene — what a 1st AD would scan."""
    return "\n".join(
        f"  {s.number:>3}  p{s.page:<7} {s.int_ext:<7} {s.day_night:<12} {s.location}"
        for s in scenes
    )
