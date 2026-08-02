"""Score a run against fixtures/ground_truth.json.

The fixture has four deliberately planted continuity errors, so continuity quality
is a number, not an impression. Two numbers matter and they pull against each other:

  recall     — of the planted errors, how many were found
  precision  — of what was reported, how much was real

Precision is the one that decides whether a 1st AD keeps using this. A tool that
cries wolf gets closed after the second false alarm, so a run that finds 2/4 with
zero noise is worth more than one that finds 4/4 buried in six inventions.

    python -m stripboard.eval
"""

from __future__ import annotations

import json
import pathlib
import sys

from stripboard.pipeline import run

FIXTURE = "fixtures/the_long_way_down.fountain"
TRUTH = "fixtures/ground_truth.json"


def _matches(issue: dict, planted: dict) -> bool:
    """A planted error counts as found when the report mentions every token that
    makes it identifiable. Deliberately literal — a fuzzy match would let a vague
    report take credit for a specific error."""
    blob = " ".join(
        [issue.get("summary", ""), issue.get("type", "")] + list(issue.get("evidence", []))
    ).lower()
    return all(tok.lower() in blob for tok in planted["must_mention"])


def main() -> int:
    truth = json.loads(pathlib.Path(TRUTH).read_text())
    result = run(FIXTURE)

    scenes, elements, issues = result["scenes"], result["elements"], result["issues"]

    print("=" * 66)
    print("\033[1mEVAL\033[0m vs ground truth\n")

    ok = True

    # --- structure -------------------------------------------------------
    want = truth["scene_count"]
    got = len(scenes)
    mark = "PASS" if got == want else "FAIL"
    ok &= got == want
    print(f"  [{mark}] scene count            {got}/{want}")

    # --- grounding: the promise the whole product rests on ----------------
    unquoted = [e for e in elements if not e.get("quote")]
    mark = "PASS" if not unquoted else "FAIL"
    ok &= not unquoted
    print(f"  [{mark}] every element cited    {len(elements)-len(unquoted)}/{len(elements)}")

    # --- element floors ---------------------------------------------------
    by_cat: dict[str, int] = {}
    for e in elements:
        by_cat[e["category"]] = by_cat.get(e["category"], 0) + 1
    for cat, floor in truth["expected_elements_min"].items():
        n = by_cat.get(cat, 0)
        mark = "PASS" if n >= floor else "FAIL"
        ok &= n >= floor
        print(f"  [{mark}] {cat:<20} {n} (min {floor})")

    # --- continuity: recall + precision -----------------------------------
    print()
    planted = truth["planted_errors"]
    found, missed = [], []
    claimed = set()
    for p in planted:
        hit = next((i for n, i in enumerate(issues) if _matches(i, p) and n not in claimed), None)
        if hit is not None:
            claimed.add(issues.index(hit))
            found.append(p)
        else:
            missed.append(p)

    recall = len(found) / len(planted)
    precision = len(claimed) / len(issues) if issues else 1.0

    print(f"  continuity recall     \033[1m{len(found)}/{len(planted)}\033[0m  ({recall:.0%})")
    print(f"  continuity precision  \033[1m{len(claimed)}/{len(issues)}\033[0m  ({precision:.0%})")
    for p in found:
        print(f"    \033[32m✓\033[0m {p['id']}  {p['summary'][:72]}")
    for p in missed:
        print(f"    \033[31m✗\033[0m {p['id']}  {p['summary'][:72]}")
    if len(claimed) < len(issues):
        print(f"    \033[33m!\033[0m {len(issues)-len(claimed)} reported issue(s) not in ground truth "
              "— either a false alarm or a real error I did not plant. Read them.")

    print()
    print("=" * 66)
    # Precision is the hard gate. Recall is tracked and improved, not gated on,
    # because the honest current number is what makes progress visible.
    gate = ok and precision == 1.0
    print(f"  \033[1m{'GREEN' if gate else 'RED'}\033[0m  "
          f"(structure+grounding ok={ok}, precision={precision:.0%}, recall={recall:.0%})\n")
    return 0 if gate else 1


if __name__ == "__main__":
    sys.exit(main())
