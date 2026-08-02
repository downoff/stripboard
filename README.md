# Stripboard

**An agent that reads a screenplay and hands back a production breakdown you can defend.**

Every element it finds — cast, props, wardrobe, vehicles, stunts, VFX, locations — carries the
scene, the page, and the **verbatim line it came from**. Then it queries its own output to find
the things a human script supervisor is paid to catch: the same vehicle described two ways
eighty pages apart, a scene marked DAY that continuity says must be NIGHT, a prop that appears
before it is established.

Built for the [Agentic Cinema Hackathon](https://agentic-cinema.devpost.com) — ClickHouse track.

## Why this and not another script generator

Breaking down a locked script is days of manual work for a 1st AD and a line producer, and it is
the input to everything downstream: the stripboard, the schedule, the budget, the call sheets.
It is also, structurally, a **grounded extraction problem** — which is a very different thing
from asking a model to write.

Generation is where every model is roughly equal. Grounding is where they are not. So Stripboard
is judged on a harder bar than "did it produce something": **every row must trace to a line of
the script, or it does not ship.**

## How it works

```
screenplay.pdf
    │
    ├─ ingest agent      → scenes, slug lines, page anchors
    ├─ breakdown agent   → elements per scene, each with a verbatim quote + page
    ├─ continuity agent  → cross-scene contradictions (the part humans miss)
    └─ schedule agent    → stripboard ordering, day/night + location banding
    │
    ▼
ClickHouse ──(official mcp-clickhouse MCP server)──► the agent queries its own breakdown
```

The element table is the point. A feature breakdown is thousands of rows across hundreds of
scenes, and the useful questions are analytical: *how many shoot days does the Mustang need,
which locations repeat, what is every scene the dog appears in, where does continuity break.*
That is a columnar-store question, so ClickHouse holds the rows and the agent reaches them
through the official MCP server at runtime.

## Stack

- **Gemini** + **Google Cloud Agent Builder / ADK** — the agent layer
- **ClickHouse** (self-hosted or Cloud) via the official **`mcp-clickhouse`** MCP server
- No other AI models, agent frameworks, or AI APIs — Google Cloud AI only, per contest rules

## Run it

```bash
docker compose up -d          # ClickHouse on :8123
uv venv .venv && uv pip install --python .venv/bin/python -e .
.venv/bin/python -m stripboard.verify_stack   # proves the MCP path end to end
```

`verify_stack` is deliberately the first thing you can run: it stands the schema up, writes a
handful of element rows with a planted continuity error, and pulls the contradiction back out
**through the MCP tools** rather than a direct driver call. If that passes, the integration the
contest requires is real on your machine.

## The hosted page

`src/stripboard/web.py` is the one-page surface: the board, the continuity report, the
cited breakdown, and a box to paste your own pages. `Dockerfile` puts a real
clickhouse-server and the app in one image, so the hosted copy runs the same stack as
your machine rather than a recording of it.

Locally:

```bash
docker build -t stripboard .
docker run -p 8080:8080 -e GOOGLE_API_KEY=... stripboard   # http://localhost:8080
```

To Cloud Run (one paste; substitute your own project):

```bash
gcloud run deploy stripboard --source . \
  --project YOUR_PROJECT --region europe-west1 \
  --allow-unauthenticated --memory 4Gi --cpu 2 \
  --min-instances 0 --max-instances 2 --timeout 600 --concurrency 20 \
  --set-env-vars "GOOGLE_API_KEY=YOUR_KEY"
```

`--min-instances 0` matters: the container idles at no cost and only bills while a run is
in flight. The paste box is capped at 120k characters and 4 live runs a minute, because
one run is roughly 15 Gemini calls against a real billed key.

## Licence

MIT. See [LICENSE](LICENSE).

## Status

Early. The stack proof is green; the agents are being built. Nothing here is lifted from any
prior project — this is original work for the contest period.
