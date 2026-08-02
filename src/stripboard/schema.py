"""The element table — the artifact everything downstream reads.

One row per (scene, element). A feature breakdown is thousands of these across
hundreds of scenes, which is why this lives in a columnar store rather than
alongside the app: the useful questions are all analytical.

`quote` is not decoration. Nothing enters this table without the verbatim line it
came from, so any row on a call sheet can be traced back to the page it is on.
"""

from __future__ import annotations

DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS stripboard.elements (
        script_id     String,
        scene_number  String,
        page          Decimal(6,3),          -- 12.5 == halfway down page 12
        int_ext       LowCardinality(String), -- INT / EXT / INT-EXT
        day_night     LowCardinality(String), -- DAY / NIGHT / DAWN / DUSK / CONTINUOUS
        location      String,
        category      LowCardinality(String), -- CAST / PROP / WARDROBE / VEHICLE / STUNT / VFX / ANIMAL / SFX / SET_DRESSING
        element       String,
        quote         String,                 -- verbatim source line. never synthesised.
        confidence    Float32
    )
    ENGINE = MergeTree
    ORDER BY (script_id, scene_number, category)
    """,
]

SEED_COLUMNS = [
    "script_id", "scene_number", "page", "int_ext", "day_night",
    "location", "category", "element", "quote", "confidence",
]

# Deliberately contains a continuity error a human would have to catch by reading
# 58 pages apart: the same hero vehicle described as a '67 and later a '68.
SEED_ROWS = [
    ["demo", "42", 12.500, "INT", "DAY",   "DINER",        "CAST",    "SARAH",        "SARAH sits alone in the corner booth.", 0.98],
    ["demo", "42", 12.500, "INT", "DAY",   "DINER",        "PROP",    "COFFEE CUP",   "She turns the coffee cup a slow half-circle.", 0.91],
    ["demo", "43", 13.000, "EXT", "NIGHT", "PARKING LOT",  "VEHICLE", "1967 MUSTANG", "The '67 Mustang idles under a dead streetlight.", 0.95],
    ["demo", "88", 71.200, "EXT", "NIGHT", "PARKING LOT",  "VEHICLE", "1968 MUSTANG", "The '68 Mustang is gone. Only oil on the asphalt.", 0.94],
]
