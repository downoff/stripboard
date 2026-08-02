"""screenplay in → breakdown, stripboard and continuity report out.

    python -m stripboard.pipeline fixtures/the_long_way_down.fountain

Scenes break down concurrently — a feature is 100+ scenes and they are independent,
so wall-clock is one scene, not the sum. The continuity pass runs last because it
needs the whole element table.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import pathlib
import re
import sys
import time

from mcp_clickhouse import create_clickhouse_client, run_query

from stripboard import agents
from stripboard.parser import parse, summarise
from stripboard.schema import DDL

DB = "stripboard"
MAX_WORKERS = 8


def _store(script_id: str, elements: list[dict]) -> None:
    client = create_clickhouse_client()
    client.command(f"CREATE DATABASE IF NOT EXISTS {DB}")
    for stmt in DDL:
        client.command(stmt)
    client.command(f"ALTER TABLE {DB}.elements DELETE WHERE script_id = '{script_id}'")
    if not elements:
        return
    client.insert(
        f"{DB}.elements",
        [
            [
                script_id, e["scene_number"], e["page"], e["int_ext"], e["day_night"],
                e["location"], e["category"], e["element"], e["quote"],
                float(e.get("confidence", 0.0)),
            ]
            for e in elements
        ],
        column_names=[
            "script_id", "scene_number", "page", "int_ext", "day_night",
            "location", "category", "element", "quote", "confidence",
        ],
    )


def _scene_nums(issue: dict) -> set[int]:
    """Scene numbers, however the pass chose to label them ("5" or "Scene 5")."""
    return {int(n) for s in issue.get("scenes", []) for n in re.findall(r"\d+", s)}


def _merge(issues: list[dict], elements: list[dict]) -> list[dict]:
    """Collapse reports that are the same finding seen from a different anchor.

    Two passes looking at one defect will not agree on which scenes to cite. The
    brass key is *present* in sc3, *used* in sc5 and *handed over* in sc11, so one
    pass files it as (3, 11) and the other as (5, 11). Both are true, and filing
    both is exactly the second false alarm that gets a tool closed for good.

    Identity is therefore (same type) AND (shares a scene OR shares an object) —
    a script supervisor files one note per defect, not one per piece of evidence.
    The trade is that two genuinely distinct errors of the same type in the same
    scene would merge; that is rarer than the duplicate it prevents.
    """
    names = {e["element"].upper() for e in elements if len(e["element"]) > 3}

    def subjects(i: dict) -> set[str]:
        # summary only — evidence quotes drag in whatever else was in the line,
        # which would make the wardrobe note look like it is about the key
        blob = i.get("summary", "").upper()
        return {n for n in names if n in blob}

    kept: list[dict] = []
    for iss in issues:
        subj, scn = subjects(iss), _scene_nums(iss)
        for n, k in enumerate(kept):
            if k["type"] != iss["type"]:
                continue
            if (subj & subjects(k)) or (scn & _scene_nums(k)):
                # keep whichever cites more evidence
                if len(iss.get("evidence", [])) > len(k.get("evidence", [])):
                    kept[n] = iss
                break
        else:
            kept.append(iss)
    return kept


def _stripboard(script_id: str) -> str:
    """Shooting order. Group by location + day/night — the whole point of a
    stripboard is that you shoot everything at one location on one lighting setup
    together, not in script order."""
    res = json.loads(run_query(f"""
        SELECT location, day_night, int_ext,
               count() AS elements,
               groupUniqArray(scene_number) AS scenes
        FROM {DB}.elements
        WHERE script_id = '{script_id}'
        GROUP BY location, day_night, int_ext
        ORDER BY location, day_night
    """))
    lines = [f"  {'LOCATION':<24} {'I/E':<8} {'TIME':<12} {'SCENES':<16} ELEMENTS"]
    for loc, dn, ie, n, scenes in res["rows"]:
        lines.append(f"  {loc:<24} {ie:<8} {dn:<12} {','.join(sorted(scenes)):<16} {n}")
    return "\n".join(lines)


def run(path: str) -> dict:
    src = pathlib.Path(path)
    script_id = src.stem
    text = src.read_text()

    print(f"\n\033[1mStripboard\033[0m  {src.name}\n" + "=" * 66)

    t0 = time.time()
    scenes = parse(text)
    print(f"\n[1/4] parsed \033[1m{len(scenes)} scenes\033[0m ({time.time()-t0:.2f}s, no model)")
    print(summarise(scenes))

    client = agents._client()

    t1 = time.time()
    print(f"\n[2/4] breaking down {len(scenes)} scenes concurrently…")
    elements: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for res in pool.map(lambda s: agents.breakdown_scene(client, s), scenes):
            elements.extend(res)
    print(f"      \033[1m{len(elements)} verified elements\033[0m in {time.time()-t1:.1f}s")

    t2 = time.time()
    _store(script_id, elements)
    print(f"\n[3/4] stored in ClickHouse via mcp-clickhouse ({time.time()-t2:.2f}s)")
    by_cat = json.loads(run_query(
        f"SELECT category, count() FROM {DB}.elements WHERE script_id='{script_id}' "
        "GROUP BY category ORDER BY count() DESC"
    ))
    print("      " + "  ".join(f"{c}:{n}" for c, n in by_cat["rows"]))

    t3 = time.time()
    print(f"\n[4/4] continuity + establishment-order passes…")
    # Two passes, run together. The whole-script sweep is good at
    # two-things-contradict; establishment order is one-thing-in-the-wrong-place and
    # kept getting lost in the bigger prompt, so it gets its own focused call with
    # page-ordered timelines. The ordering pass self-skips when nothing qualifies.
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        f_general = pool.submit(agents.find_continuity, client, scenes, elements)
        f_order = pool.submit(agents.find_ordering_issues, client, scenes, elements)
        issues = f_general.result()
        ordering = f_order.result()

    raw = len(issues) + len(ordering)
    issues = _merge(issues + ordering, elements)
    merged = raw - len(issues)
    print(
        f"      \033[1m{len(issues)} issue(s)\033[0m  "
        f"(sweep {len(f_general.result())}, ordering {len(ordering)}, "
        f"{merged} merged as duplicate) in {time.time()-t3:.1f}s"
    )

    print("\n" + "=" * 66)
    print("\033[1mSTRIPBOARD\033[0m  (shooting order, grouped by location + lighting)\n")
    print(_stripboard(script_id))

    print("\n" + "=" * 66)
    print("\033[1mCONTINUITY\033[0m\n")
    if not issues:
        print("  none found")
    for i, iss in enumerate(issues, 1):
        sev = {"HIGH": "\033[31m", "MEDIUM": "\033[33m", "LOW": "\033[90m"}.get(iss["severity"], "")
        print(f"  {i}. {sev}[{iss['severity']}]\033[0m {iss['type']}  scenes {', '.join(iss['scenes'])}")
        print(f"     {iss['summary']}")
        for ev in iss["evidence"]:
            print(f"       \033[90m> {ev}\033[0m")
        print()

    print("=" * 66)
    print(f"total {time.time()-t0:.1f}s\n")
    return {"scenes": scenes, "elements": elements, "issues": issues, "script_id": script_id}


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "fixtures/the_long_way_down.fountain")
