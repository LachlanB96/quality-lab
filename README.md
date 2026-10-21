# Quality Lab

A pytest suite that plays my game, SpireScape, over the network, plus evals for an AI email classifier, and the page that reports on both.

**The report:** [spirescape.lachlanb.com/pytest-suite](https://spirescape.lachlanb.com/pytest-suite/) · **The game:** [spirescape.lachlanb.com](https://spirescape.lachlanb.com/)

## What's here

SpireScape is a multiplayer card battler I'm building: a C# server that owns every rule, and a Unity WebGL client that only sends intents and draws what comes back. This suite never imports the server. It speaks the same binary protocol the browser does, from Python, so it tests what a player can actually reach.

It runs against two targets:

- **local**: a server built from the private SpireScape repo, one per pytest-xdist worker, on a 20 ms tick with a fixed seed. Everything runs here, including hostile input that wouldn't be fair on a public server.
- **live**: the public demo at its real 600 ms tick, shared with whoever else is playing. Fights share one worker because the map has two rats, and the helpers retry when someone else gets to a rock first.

Each test carries a layer and an area:

| Layer | What it means here |
|---|---|
| unit | The lab's own code: the protocol codec, the run recorder, the analytics, the AI guardrails, the eval scoring |
| contract | What the server promises every client: message order, shapes and sizes, strict decoding of every frame |
| integration | One feature through a real server: login, movement, gathering, crafting, combat, chat |
| e2e | The whole tutorial, from Old Bram to the boat |
| property | Hypothesis: generated messages, clicks and garbage bytes |
| robustness | Malformed frames, oversized frames, floods, lies about lengths |
| data | Invariants over the content catalog every client receives |
| perf | Tick cadence, round trips and login time, recorded as metrics |
| ui | The WebGL client in a real browser (Playwright) |

## Bugs it found

Seven, all real, all pinned by `xfail(strict=True)` tests carrying the bug's id. A fix makes the test pass, which fails the run until the marker comes off, so nothing gets fixed or broken quietly. The write-ups are in [`findings.json`](findings.json). The worst: losing a starter tool can leave the tutorial impossible to finish.

## The AI parts

- **Review** (`qualitylab/analyst.py`): after the runs, Claude reads the run records and the test code, then writes an overview and suggestions. A script checks every test it names and every number it quotes against the records before anything is published, and strikes what doesn't match. The guardrails have their own unit tests.
- **Suggestions** (`suggestions.json`): Claude proposes, I decide. Each one stays on the page with its status and my reason.
- **Evals** (`qualitylab/evals/email_triage.py`): my email dashboard sorts mail with Claude Haiku and puts interview times in a calendar. The eval runs its real classifier over a made-up, labelled inbox, compares variants, and sorts every miss into a prompt gap, a rubric gap or a product issue.

I built this with Claude Code: it drafted the tests and the tooling, and I review every test (`review.json` records which).

## Running it

```sh
uv sync
npm ci                                                              # esbuild and TypeScript 7, for the report page's script
uv run pytest tests/unit                                            # no server needed
uv run pytest tests/spirescape --target live -n 4 --dist loadgroup  # the public demo
SPIRESCAPE_REPO=<checkout> uv run pytest tests -n 4                 # local: needs the private server source

uv run ql run --target live      # a recorded run, into history/runs/
uv run ql analyse                # the AI review (needs the claude CLI, signed in)
uv run ql eval email             # the evals (needs the private classifier: EMAIL_DASHBOARD_DIR)
uv run ql build                  # the report page, into build/
```

[`docs/TOUR.md`](docs/TOUR.md) walks through where each pytest technique is used and why.
