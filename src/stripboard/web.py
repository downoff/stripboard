"""Hosted surface for Stripboard.

One page. It shows the three things a 1st AD actually needs — the board, the
continuity report, and the citation behind every single element — and it lets you
run the agent on your own pages.

The strip colours are not decoration. A physical production board uses white for
INT/DAY, yellow for EXT/DAY, blue for INT/NIGHT and green for EXT/NIGHT, and a
scheduler reads the board by colour before reading a word of it. Reproducing that
convention is the whole point: the output has to look like the document it replaces.
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import threading
import time

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

from stripboard import pipeline

app = FastAPI(title="Stripboard")

def _find_fixture() -> pathlib.Path:
    """Locate the demo screenplay from the repo OR from an installed package.

    parents[2] is the repo root when running from src/, but site-packages when
    pip-installed, which is how it runs in the container. Check both.
    """
    name = "the_long_way_down.fountain"
    for base in (
        pathlib.Path(os.environ.get("STRIPBOARD_FIXTURES", "/app/fixtures")),
        pathlib.Path(__file__).resolve().parents[2] / "fixtures",
        pathlib.Path.cwd() / "fixtures",
    ):
        if (base / name).is_file():
            return base / name
    raise FileNotFoundError(f"{name} not found; set STRIPBOARD_FIXTURES")


FIXTURE = _find_fixture()
_CACHE: dict = {}
_LOCK = threading.Lock()

# Physical production-board stock. INT/DAY is white, EXT/DAY yellow, INT/NIGHT
# blue, EXT/NIGHT green. Anything else (CONTINUOUS, DUSK) gets board pink, which
# is what a real board uses for the odd ones out.
STRIP = {
    ("INT", "DAY"): ("#f4f1ea", "#1b1917"),
    ("EXT", "DAY"): ("#f2d857", "#241f05"),
    ("INT", "NIGHT"): ("#7fa8d4", "#0c1a2a"),
    ("EXT", "NIGHT"): ("#8fc4a0", "#0b1f14"),
}
PINK = ("#eba9bd", "#2b0c16")


def _strip_colour(int_ext: str, day_night: str) -> tuple[str, str]:
    return STRIP.get((int_ext.upper(), day_night.upper()), PINK)


# ~120k chars is comfortably a feature screenplay. The cap exists because the
# hosted demo runs on a real billed Gemini key.
MAX_CHARS = int(os.environ.get("STRIPBOARD_MAX_CHARS", 120_000))
RUNS_PER_WINDOW = int(os.environ.get("STRIPBOARD_RUNS_PER_WINDOW", 4))
WINDOW_SECONDS = 60.0
_runs: list[float] = []


def _throttle() -> bool:
    """Allow a live run, or refuse. Process-wide, not per-IP.

    Per-IP would be the usual choice, but the thing being protected here is a
    shared API budget rather than any one visitor's fair share, and a global
    counter is the one that actually bounds spend.
    """
    now = time.time()
    with _LOCK:
        _runs[:] = [t for t in _runs if now - t < WINDOW_SECONDS]
        if len(_runs) >= RUNS_PER_WINDOW:
            return False
        _runs.append(now)
        return True


def _run(text: str, script_id: str) -> dict:
    """Run the pipeline on a screenplay body. Writes a temp file because the
    pipeline's unit of work is a script on disk, same as the CLI."""
    tmp = pathlib.Path("/tmp") / f"{script_id}.fountain"
    tmp.write_text(text)
    return pipeline.run(str(tmp))


def _demo() -> dict:
    """The fixture run, computed once on first request and held in memory.

    Judges should not wait 45 seconds to see the thing work, and a cold Cloud Run
    instance would otherwise do the whole pipeline before painting a pixel.
    """
    with _LOCK:
        if "demo" not in _CACHE:
            t0 = time.time()
            res = _run(FIXTURE.read_text(), "the_long_way_down")
            res["elapsed"] = time.time() - t0
            _CACHE["demo"] = res
    return _CACHE["demo"]


# ---------------------------------------------------------------- rendering


def _e(s: object) -> str:
    return html.escape(str(s))


def _board(elements: list[dict]) -> str:
    """Group by location + lighting setup. Script order is not shooting order —
    you shoot everything at one location on one setup together, and that
    regrouping is the single thing a stripboard exists to do."""
    groups: dict[tuple, dict] = {}
    for el in elements:
        key = (el["location"], el["int_ext"], el["day_night"])
        g = groups.setdefault(key, {"scenes": set(), "n": 0, "pages": []})
        g["scenes"].add(el["scene_number"])
        g["pages"].append(float(el["page"]))
        g["n"] += 1

    rows = []
    for (loc, ie, dn), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][2])):
        bg, fg = _strip_colour(ie, dn)
        scenes = ", ".join(sorted(g["scenes"], key=lambda s: int(s) if s.isdigit() else 0))
        rows.append(
            f'<div class="strip" style="--bg:{bg};--fg:{fg}">'
            f'<span class="sc">{_e(scenes)}</span>'
            f'<span class="loc">{_e(loc)}</span>'
            f'<span class="ie">{_e(ie)}</span>'
            f'<span class="dn">{_e(dn)}</span>'
            f'<span class="ct">{g["n"]} element{"" if g["n"] == 1 else "s"}</span>'
            f"</div>"
        )
    return "\n".join(rows)


def _continuity(issues: list[dict]) -> str:
    if not issues:
        return '<p class="none">No continuity conflicts found.</p>'
    out = []
    for iss in issues:
        sev = iss.get("severity", "LOW")
        ev = "".join(f"<li>{_e(q)}</li>" for q in iss.get("evidence", []))
        out.append(
            f'<article class="issue sev-{_e(sev).lower()}">'
            f'<header><span class="sev">{_e(sev)}</span>'
            f'<span class="kind">{_e(iss.get("type", ""))}</span>'
            f'<span class="scenes">sc {_e(", ".join(iss.get("scenes", [])))}</span></header>'
            f"<p>{_e(iss.get('summary',''))}</p>"
            f"<ul class=\"ev\">{ev}</ul>"
            f"</article>"
        )
    return "\n".join(out)


def _elements(elements: list[dict]) -> str:
    rows = []
    for el in sorted(elements, key=lambda e: float(e["page"])):
        rows.append(
            "<tr>"
            f'<td class="num">{_e(el["scene_number"])}</td>'
            # one decimal: the stored value is a fractional page POSITION, and three
            # decimals reads as false precision on a document measured in eighths
            f'<td class="num">{float(el["page"]):.1f}</td>'
            f'<td><span class="cat">{_e(el["category"])}</span></td>'
            f"<td>{_e(el['element'])}</td>"
            f'<td class="q">{_e(el["quote"])}</td>'
            "</tr>"
        )
    return "\n".join(rows)


CSS = """
*{box-sizing:border-box}
body{margin:0;background:#12100e;color:#e8e3d9;
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px 96px}
header.top{padding:64px 0 40px;border-bottom:1px solid #2c2823}
h1{font-size:clamp(34px,6vw,58px);line-height:.98;margin:0 0 14px;letter-spacing:-.03em;font-weight:680}
h1 em{font-style:normal;color:#f2d857}
.lede{max-width:56ch;color:#a8a096;margin:0 0 8px}
.meta{font:12px/1.5 ui-monospace,Menlo,monospace;color:#6d665d;margin-top:20px;
  display:flex;gap:20px;flex-wrap:wrap}
.meta b{color:#a8a096;font-weight:500}
h2{font-size:13px;letter-spacing:.13em;text-transform:uppercase;color:#8a8279;
  margin:64px 0 6px;font-weight:600}
h2 + .sub{color:#6d665d;font-size:14px;margin:0 0 22px;max-width:62ch}

/* the board — real strips, read by colour first */
.board{border:1px solid #2c2823;border-radius:3px;overflow:hidden}
.strip{display:grid;grid-template-columns:76px 1fr 52px 108px 104px;gap:12px;align-items:center;
  background:var(--bg);color:var(--fg);padding:9px 14px;
  font:12.5px/1.3 ui-monospace,Menlo,monospace;
  border-bottom:1px solid rgba(0,0,0,.22)}
.strip:last-child{border-bottom:0}
.strip .loc{font-weight:700;letter-spacing:.01em}
.strip .sc{opacity:.72}
.strip .ct{text-align:right;opacity:.62}
.strip .ie,.strip .dn{opacity:.78}

/* continuity */
.issue{border:1px solid #2c2823;border-left:3px solid #4a443c;border-radius:3px;
  padding:16px 18px;margin-bottom:10px;background:#171512}
.issue.sev-high{border-left-color:#d4553f}
.issue.sev-medium{border-left-color:#d9a441}
.issue header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:9px;
  font:11px/1 ui-monospace,Menlo,monospace;letter-spacing:.07em}
.sev{color:#d4553f;font-weight:700}
.sev-medium .sev{color:#d9a441}.sev-low .sev{color:#8a8279}
.kind{color:#8a8279}.scenes{color:#6d665d;margin-left:auto}
.issue p{margin:0 0 10px}
.ev{margin:0;padding:0;list-style:none;border-top:1px solid #24211d;padding-top:9px}
.ev li{font:12.5px/1.6 ui-monospace,Menlo,monospace;color:#8a8279;padding-left:14px;
  position:relative;margin-bottom:3px}
.ev li:before{content:"";position:absolute;left:0;top:.62em;width:6px;height:1px;background:#4a443c}
.none{color:#6d665d}

/* elements + citation */
.tbl{width:100%;border-collapse:collapse;font-size:13.5px}
.tbl th{text-align:left;font:11px/1 ui-monospace,Menlo,monospace;letter-spacing:.09em;
  color:#6d665d;padding:0 10px 10px;border-bottom:1px solid #2c2823;font-weight:500}
.tbl td{padding:8px 10px;border-bottom:1px solid #1e1b18;vertical-align:top}
.tbl .num{font:12px ui-monospace,Menlo,monospace;color:#6d665d;white-space:nowrap}
.cat{font:10.5px ui-monospace,Menlo,monospace;letter-spacing:.06em;color:#a8a096;
  border:1px solid #2c2823;border-radius:2px;padding:2px 6px;white-space:nowrap}
.q{color:#8a8279;font:12.5px/1.5 ui-monospace,Menlo,monospace}
.scroll{overflow-x:auto}

/* run your own */
form{margin-top:18px}
textarea{width:100%;min-height:190px;background:#171512;color:#e8e3d9;
  border:1px solid #2c2823;border-radius:3px;padding:14px;
  font:13px/1.6 ui-monospace,Menlo,monospace;resize:vertical}
textarea:focus{outline:none;border-color:#f2d857}
button{margin-top:12px;background:#f2d857;color:#12100e;border:0;border-radius:3px;
  padding:11px 22px;font-size:14px;font-weight:640;cursor:pointer}
button:hover{background:#fbe378}
button[disabled]{opacity:.5;cursor:progress}
.note{color:#6d665d;font-size:13px;margin-top:10px}
footer{margin-top:80px;padding-top:24px;border-top:1px solid #2c2823;
  color:#6d665d;font-size:13px}
a{color:#f2d857}
@media (max-width:720px){
  .strip{grid-template-columns:56px 1fr;gap:4px 10px}
  .strip .ie,.strip .dn,.strip .ct{grid-column:2;text-align:left;font-size:11.5px}
}
"""


def _page(res: dict, *, title: str, elapsed: float | None) -> str:
    scenes, elements, issues = res["scenes"], res["elements"], res["issues"]
    took = f"{elapsed:.0f}s" if elapsed else "cached"
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stripboard — {_e(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<header class="top">
<h1>Every element on this board<br>points at <em>a line in the script</em>.</h1>
<p class="lede">Stripboard reads a screenplay and returns the paperwork a first AD
would otherwise build by hand: a shooting-order board, a continuity report, and a
breakdown where nothing is listed unless a verbatim quote backs it.</p>
<div class="meta">
<span><b>{len(scenes)}</b> scenes</span>
<span><b>{len(elements)}</b> elements, all cited</span>
<span><b>{len(issues)}</b> continuity conflicts</span>
<span><b>{took}</b> end to end</span>
<span>store: <b>ClickHouse</b> via mcp-clickhouse</span>
</div>
</header>

<h2>Stripboard</h2>
<p class="sub">Grouped by location and lighting setup, not script order. Strip colour
follows the physical board: white INT/DAY, yellow EXT/DAY, blue INT/NIGHT, green
EXT/NIGHT.</p>
<div class="board">{_board(elements)}</div>

<h2>Continuity</h2>
<p class="sub">Found by reading the whole script at once, then a second pass that
only looks for things used before the beat that introduces them. Each conflict has
to be provable from two quotes or it is not reported.</p>
{_continuity(issues)}

<h2>Breakdown</h2>
<p class="sub">The citation column is the product. An element with no quote behind it
never reaches this table.</p>
<div class="scroll"><table class="tbl">
<thead><tr><th>SC</th><th>PAGE</th><th>CATEGORY</th><th>ELEMENT</th><th>CITED FROM</th></tr></thead>
<tbody>{_elements(elements)}</tbody></table></div>

<h2>Run it on your own pages</h2>
<p class="sub">Paste a screenplay. Scene headings in standard form (INT./EXT. LOCATION - DAY).
A short scene takes a few seconds; a feature takes about a minute.</p>
<form method="post" action="/run" onsubmit="this.q.disabled=true;this.q.textContent='Reading…'">
<textarea name="script" placeholder="INT. ROADSIDE DINER - DAY&#10;&#10;Rain hammers the window..."></textarea>
<button name="q" type="submit">Break it down</button>
</form>
<p class="note">Nothing is stored between runs.</p>

<footer>Built for Agentic Cinema. Source:
<a href="https://github.com/downoff/stripboard">github.com/downoff/stripboard</a>.
Gemini 3.6 Flash for judgement, ClickHouse for the element store, everything else
deterministic.</footer>
</div></body></html>"""


def _notice(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stripboard</title><style>{CSS}</style></head><body><div class="wrap">
<header class="top"><h1>{_e(title)}.</h1><p class="lede">{_e(body)}</p></header>
<p class="note" style="margin-top:28px"><a href="/">Back to the demo</a></p>
</div></body></html>"""


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    res = _demo()
    return _page(res, title="The Long Way Down", elapsed=res.get("elapsed"))


@app.post("/run", response_class=HTMLResponse)
def run(script: str = Form("")) -> str:
    """Run the pipeline on pasted pages.

    Guarded on both sides. One run is ~15 Gemini calls against a real billed key,
    so an open endpoint on a public URL is a way to spend someone else's budget:
    MAX_CHARS bounds a single request and _throttle bounds the rate.
    """
    text = (script or "").strip()
    if len(text) < 40:
        res = _demo()
        return _page(res, title="The Long Way Down", elapsed=res.get("elapsed"))
    if len(text) > MAX_CHARS:
        return _notice(
            "That is longer than this demo accepts",
            f"The paste was {len(text):,} characters; the hosted demo caps at "
            f"{MAX_CHARS:,} (roughly a feature). Run the CLI locally for anything "
            f"bigger — there is no cap there.",
        )
    if not _throttle():
        return _notice(
            "Too many runs just now",
            "This demo allows a few live runs a minute so one visitor cannot spend "
            "the whole API budget. Wait a moment and try again, or clone the repo "
            "and run it with your own key.",
        )
    t0 = time.time()
    res = _run(text, f"user_{int(time.time())}")
    return _page(res, title="your pages", elapsed=time.time() - t0)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
