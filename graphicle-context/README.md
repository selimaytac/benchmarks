# graphicle-context

How long does [Graphicle](https://github.com/Graphicle-AI/graphicle)'s `graphicle context` take on a large repository, and does its Claude Code prompt hook return a packet within its 10 s budget? Measured on 0.1.3 and on 0.1.5, which added a passage index for this ([issue #8](https://github.com/Graphicle-AI/graphicle/issues/8)).

## Result

kubernetes at ca8a2a9e4 (30,315 files, shallow history since 2026-06-01), the 22 questions in [questions.json](questions.json), no answer model configured, no model calls:

| | 0.1.3 | 0.1.5 |
|---|---|---|
| `graphicle context`, median | 153.5 s | 9.5 s |
| 90th percentile (nearest rank) | 331 s | 10.0 s |
| slowest | 999 s | 10.4 s (single rounds 8.8–11.1 s) |
| context under 10 s | 0 of 22 | 19 of 22 |
| prompt hook returned a packet (one pass) | not run | 18 of 22 |
| the question's file name appears in the packet (`graphicle context`) | 17 of 22 | 20 of 22 |
| same, in the hook's packet (one pass) | not run | 15 of 22 |
| full sync (code graph + passage index) | not measured | 393 s |

## What this does and doesn't tell you

- On this machine each 0.1.5 call costs about 9 s almost regardless of the question: an unrelated prompt took 8.9 s, and `--max-tokens` from 300 to 3000 all took 9.2–9.6 s. That sits right under the hook's 10 s budget, so near the edge it comes down to timing. In the one pass, L14 and L16 ran past it; on three more runs each, L12, L14 and L16 all got a packet that named the file (8.7–9.5 s). `GRAPHICLE_PROMPT_CONTEXT_SECONDS` sets the budget.
- The maintainer reported a 3.1 s median on their own server for the same questions, so this machine is about 3x slower; expect your numbers to differ.
- The 0.1.3 column is one round per question from a run the day before, possibly with other work on the machine; 0.1.5 is 3 rounds per question. Treat the ratio as rough.
- "File name appears in the packet" means a pattern of the question's first key group matches anywhere in the packet text (cut at 8,400 characters for both versions). It is not "ranked first". For L questions the file is one the fix commit changed; C01–C04 are caller questions, where it is one of the callers. By group: L 15/18 → 16/18, C 2/4 → 4/4.
- L11 and L13 are skipped by the hook by design, as non-code questions.
- One repository, 22 questions, one machine. It says nothing about answer quality with a model, or about other agents.

## Run it

```sh
git clone --shallow-since=2026-06-01 https://github.com/kubernetes/kubernetes && git -C kubernetes checkout ca8a2a9e4
mkdir data
REPO=$PWD/kubernetes GRAPHICLE_DATA_DIR=$PWD/data ROUNDS=3 python3 run.py
```

`run.py` syncs the clone into the empty data folder, then for each question times `graphicle context` (ROUNDS times) and runs the prompt hook once the way `graphicle setup --prompt-context` installs it (`graphicle hook claude-code prompt-context`). Rows go to `results-0.1.5.json`. `results-0.1.3.json` holds the earlier version's timings, and `results-0.1.5-hook-repeats.json` the three extra hook runs.
