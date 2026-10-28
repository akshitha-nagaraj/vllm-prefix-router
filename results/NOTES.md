# Results notes

One CSV per run, named `<experiment>-<policy>.csv`, with one row per request. See the README for the columns.

## Calibration (`calib-*`, `calib8-*`)

Round-robin only. These runs locate the load level where latency starts climbing, using 4 system prompts, 3 turns, and 2s think time.

- `calib-*`: 32-token replies. Total latency jumps between 0.4 and 0.8 sessions/s.
- `calib8-*`: 8-token replies, the setting used for the experiments. The jump moves to between 0.8 and 1.6 sessions/s.

Even at low load, round-robin got about 75% prefix-cache hits with 4 system prompts, because each replica caches all four after a few requests.

## Experiment 1: load sweep (`exp1-*`)

Sweeps `SESSION_RATE` over 0.4, 0.8, 1.2, and 1.6, with `N_SYSTEM_PROMPTS=4`, `TURNS=3`, `THINK_TIME=2`, `MAX_TOKENS=8`, and `DURATION=120`. There are 2 repeats, with `SEED` set to the repeat number. Policies within a repeat share the seed, so they see the same arrival pattern.

**Known problems:**

- `exp1-rate0.8-rep2-cache_aware` and `exp1-rate0.8-rep2-hybrid` are **contaminated**. The host Mac idle-slept and a Time Machine backup started during these runs. Exclude them.
- **Run order was fixed:** round-robin, then cache-aware, then hybrid, at every rate. Any drift over a block therefore lands on hybrid. `ordercheck-*` re-runs rate 1.2 in reverse order to test this.

**Observations:**

- Affinity works as intended: cache-aware and hybrid send 94–100% of follow-up turns to the same replica as the previous turn, against about 50% for round-robin.
- At 4 system prompts, the time-to-first-token differences are mostly within run-to-run noise. Round-robin already hits on the system prompt everywhere, so routing can only save the conversation-history part of each prompt (about 100–200 tokens).

## Run-order check (`ordercheck-*`)

This re-runs rate 1.2 with the repeat-1 seed in reverse order: hybrid, then cache-aware, then round-robin.

| Policy | Original order (TTFT p50 / p95) | Reversed order (TTFT p50 / p95) |
|---|---|---|
| round-robin | 4.43 / 7.65 s (ran 1st) | 5.61 / 9.59 s (ran 3rd) |
| cache-aware | 4.28 / 8.20 s (ran 2nd) | 4.04 / 7.63 s (ran 2nd) |
| hybrid | 7.20 / 17.08 s (ran 3rd) | 4.49 / 9.24 s (ran 1st) |

**Position in the block matters more than the policy.** Whichever policy ran last was slowest. Hybrid's apparent weakness at high load in `exp1` is mostly a run-order artifact, so the `exp1` comparisons at 1.2 and 1.6 are not reliable. The cause of the drift is not yet known; host thermal throttling under sustained load is one candidate. Later experiments rotate the policy order between blocks and pause between runs.
