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

## Experiment 2: prompt-diversity sweep (`exp2-*`)

Sweeps `N_SYSTEM_PROMPTS` over 4, 16, 64, and 128 at `SESSION_RATE=0.8`. Everything else matches exp1: `TURNS=3`, `THINK_TIME=2`, `MAX_TOKENS=8`, `DURATION=120`, and 2 repeats. The policy order rotates between blocks, and each run is followed by a 60s pause. The host did not sleep, and no pod restarted.

| N | Policy | TTFT p50 rep1 / rep2 (s) | TTFT p95 rep1 / rep2 (s) | Mean TTFT, turn 0 / turns 1–2 (s) |
|---|---|---|---|---|
| 4 | round-robin | 2.46 / 1.58 | 14.59 / 2.70 | 2.21 / 3.51 |
| 4 | cache-aware | 10.78 / 1.85 | 24.34 / 3.54 | 4.99 / 7.59 |
| 4 | hybrid | 1.88 / 1.77 | 3.42 / 3.51 | 1.88 / 1.97 |
| 16 | round-robin | 2.71 / 2.14 | 4.88 / 4.90 | 2.71 / 2.63 |
| 16 | cache-aware | 2.59 / 2.99 | 4.54 / 5.18 | 2.87 / 2.96 |
| 16 | hybrid | 2.96 / 2.58 | 5.97 / 4.79 | 2.85 / 3.03 |
| 64 | round-robin | 4.56 / 3.98 | 7.97 / 8.93 | 4.89 / 4.31 |
| 64 | cache-aware | 2.66 / 3.00 | 5.09 / 6.26 | 3.19 / 2.96 |
| 64 | hybrid | 4.64 / 3.12 | 9.34 / 6.15 | 4.15 / 4.21 |
| 128 | round-robin | 5.40 / 5.08 | 12.70 / 12.01 | 6.30 / 5.90 |
| 128 | cache-aware | 4.94 / 3.89 | 10.84 / 8.21 | 5.39 / 4.71 |
| 128 | hybrid | 4.98 / 3.87 | 9.95 / 8.80 | 5.28 / 4.76 |

**Anomaly:** the `exp2-n4-rep1` round-robin and cache-aware runs were the first two runs of the sweep, after about 30 minutes idle. Their tail latency is several times that of every other N=4 run, including rep 2 of the same policies. The cause is unknown. Treat N=4 as rep 2 only.

**Observations:**

- **N=4 and N=16:** no meaningful difference between policies. All system prompts fit in each replica's cache, so round-robin hits on them too.
- **N=64:** cache-aware is clearly better, and consistently so in both repeats. p50 TTFT is 2.7–3.0s against 4.0–4.6s for round-robin, and p95 is 5.1–6.3s against 8.0–8.9s.
- **N=128:** cache-aware and hybrid both beat round-robin, but by less. p50 is about 15–25% lower and p95 about 15–30% lower.
- **Mechanism:** the gap opening at N=64 and narrowing at N=128 fits the cache-duplication argument. 64 prompts of ~750 tokens is about 48k tokens. That exceeds one replica's ~43k-token cache when round-robin copies every prompt onto both replicas, but fits when cache-aware routing splits the prompts between them. At 128, neither policy fits the working set. This is inferred from latency only; per-run cache hit and eviction counts were not recorded.
- **Hybrid** matched cache-aware in rep 2 at N=64 but matched round-robin in rep 1. With `MIN_GAP=4` it may be spilling often enough to break the partitioning. This is unconfirmed.
