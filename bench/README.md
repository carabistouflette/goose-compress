# Benchmark harness

Ten rows measure valid payloads under the contracts below. Two additional
Goose-only rows measure rejection of a truncated stream and a bad gzip
checksum. Valid rows must match the fixture output length and FNV-1a digest;
rejection rows must return the specified error and keep the same produced
length and digest across samples.

```
bench/
  bench.goose                    goose-compress: oneshot / stream / fresh / tail / invalid
  compare/zlib_bounded.c         zlib, streaming, under the same contract
  compare/zlib_oneshot.c         zlib, one-shot, no bounds
  compare/libdeflate_oneshot.c   libdeflate 1.26, one-shot, bounded buffer
  compare/goose_deflate_bench.goose  Macho0x/goose-deflate, whole buffer, no cap
  compare/goose_probe.goose      the library's status for one case, for parity
  compare/contract_parity.py     proves the reference row matches the library
  runner.py                      the serialised measurement runner
  measure.py                     the older single-median runner
```

## Contracts, and which rows may be ranked

| Row | Contract | Buffer | Comparable |
| --- | --- | --- | --- |
| goose-compress oneshot | bounded: output budget, input budget, 1 member, trailing rejected | reused | yes |
| goose-compress stream | bounded, adaptive steps up to 64 KiB | reused | yes |
| goose-compress fresh | bounded, adaptive steps up to 64 KiB | allocated per call | yes |
| goose-compress tail | bounded: `finish` + `close` after the last byte | reused | yes |
| zlib bounded | bounded, same framing policy, zlib 1.3.2 | reused | yes |
| zlib-ng bounded | bounded, same framing policy, zlib-ng 2.3.3 | reused | yes |
| zlib 1.4 bounded | bounded, same framing policy, zlib 1.4 built static | reused | yes |
| goose-compress baseline c99b049 oneshot | the published baseline binary, unchanged | reused | yes |
| goose-compress baseline c99b049 stream | the published baseline binary, unchanged | reused | yes |
| libdeflate oneshot | bounded one-shot: `out_nbytes_avail` is the budget, trailing rejected via `actual_in_nbytes` | reused | yes, against `oneshot` only |
| zlib oneshot | unbounded: no budgets, no trailing check | reused | **no** |
| goose-deflate whole buffer | unbounded: no output cap, no budgets, bool result | allocated per call | **no** |

The two invalid-input rows are deliberately non-comparable. They measure the
bounded library's rejection path; the one-shot references do not provide the
same resource-limit contract.

`zlib bounded` is the same zlib under the same framing policy, so a difference
there is the cost of the bounded incremental state machine, not a speed
ranking. The zlib-ng and zlib 1.4 rows answer "is this zlib's fault or ours?".
`libdeflate oneshot` is bounded per call but has no cumulative budget and no
streaming, so it ranks `oneshot` and nothing else. `goose-deflate` has no cap
at all and is measured, never ranked. No row here supports a state-of-the-art
claim.

## Build

```sh
GOOSE=/path/to/pinned/goose make bench
```

Compiles the four binaries into `.build/` and writes eight valid fixtures plus
the truncated-stream and bad-checksum inputs to `.build/bench-inputs/`. It
measures nothing.

## Plan, without timing

```sh
GOOSE=/path/to/pinned/goose make bench-plan
```

Prints every command, the input set, the invocation count, valid payload volume
and rejection call count. Nothing is timed.

## Run

```sh
GOOSE=/path/to/pinned/goose make bench-run
```

or directly:

```sh
python3 bench/runner.py --goose /path/to/pinned/goose --samples 20 \
  --output .build/benchmark.json
```

The runner keeps every raw sample, alternates the harness order each round so
slow drift lands on all rows, pins the child processes to one allowed CPU,
discards two warm-up rounds, and reports min/max/median/mean/stdev/MAD/p90/p99/
IQR plus peak RSS.

Two things about the output are worth knowing before reading it.

`--budget-mode exact|headroom` decides `limits.output_bytes`: `exact` sets it to
the payload, which is the tightest case, and `headroom` sets it to
`--headroom-bytes` (16 MiB by default), which is what a caller that does not
know the size does. They are different workloads and a change can help one and
hurt the other, so they are separate runs and the mode is recorded in
`method.budget_mode`.

`rows[].per_call_ns.samples` is sorted, which is right for a distribution and
wrong for pairing. Every observation is therefore also written to
`<output>.observations.jsonl` in acquisition order with fixture, round, position
in the round, implementation, iteration count and the unrounded `total_ns`, and
the `paired` section divides two rows round by round and reports the median
ratio with a bootstrap 95% interval. A ratio whose interval contains 1.0 is no
difference, not a small win. It records the Goose and C compiler paths, versions and
binary hashes; measured source and benchmark binary hashes; resolved shared
library paths and hashes; compiler flags; git state; CPU model/governor and
load average before and after. Load average is recorded but excluded from the
static-identity comparison because the benchmark itself changes it. It rejects
a run if compiler, source, binary, dependency or other static identity changes.

### Contention: two protocols

`--protocol absolute` is the original gate: refuse to start when the 1-minute
load average is above `--max-load` (default 0.5). It is kept because it is what
the earlier campaigns were measured under, and because it is easy to explain.

It is also weak on a multicore machine. A load average of 0.4 on eight cores
says nothing about whether the one core this run is pinned to is busy, and a
load average of 0.0 says nothing about a job that started after the check. So
`--protocol effective` (the default) gates on the CPU that is actually used:

1. Pick the least busy allowed CPU from `/proc/stat`, and record its class
   (fast or slow core) and its maximum frequency.
2. Measure that CPU's foreign busy fraction over `--sample-window` seconds and
   refuse above `--max-foreign-cpu` (default 0.05).
3. Run a fixed calibration loop on that CPU and refuse if the p90 of
   wall-time / process-CPU-time exceeds `--max-jitter` (default 1.15). That
   ratio is what a preempted run looks like; a quiet run sits near 1.0.
4. Refuse if CPU pressure `some-avg10` exceeds `--max-psi` (default 5.0).
5. Require at least `--min-calibration-batches` (default 10) calibration
   batches, so a deadline cannot leave too few observations for a quantile.
6. Re-measure after the run and record the result as
   `contention.endpoint_checks_passed`.

The endpoint checks bracket the run; they do not prove the machine was quiet in
between. What covers the middle is `contention.sample_phase`, and each field
there says exactly what it measures:

- `no_large_round_outliers` is true when no sample round took more than 1.5x
  the median round. That is a coarse signal: it catches a transient big enough
  to move a whole round, and it says nothing about a short disturbance that
  landed inside one implementation's slice. It is not a claim that the run was
  undisturbed.
- `children_nonvoluntary_switches_per_round` is the involuntary context-switch
  count of the timed binaries, taken as a difference of
  `getrusage(RUSAGE_CHILDREN)`. The sample loop runs one child at a time and
  waits for each, so a round's delta belongs to that round's children. The
  absolute value includes the switches inherent in spawning and reaping a
  process, so the signal is the variation across rounds, not the level: a round
  far above its neighbours is a round that was descheduled.
- `runner_nonvoluntary_switches_per_round` is the same counter for the Python
  driver, read from `/proc/self/status`. It describes the driver, not the
  binaries, and is kept only because it is cheap and occasionally useful.

A refused run is written to `<output>.rejected.json` with the raw contention
observation, so a rejection is evidence and not a deleted attempt.

`--lock-file` takes an exclusive `flock` on a path for the whole run, and
`--lock-timeout` bounds the wait. `make bench-run` always passes
`$(BENCH_LOCK)`, default `/tmp/goose-compress-bench.lock`; set `BENCH_LOCK=` to
opt out, and set it to a shared path to serialise against other jobs on the
machine. A run that cannot take the lock within the timeout exits non-zero
instead of measuring anyway.

Run it alone. Concurrent work on the same CPU makes every row meaningless.

## Single harness invocation

Each binary takes the same arguments, so a row can be reproduced by hand:

```sh
.build/bench            oneshot input.gz 200 16777216
.build/bench            stream  input.gz 200 16777216
.build/bench            fresh   input.gz 200 16777216
.build/bench            tail    input.gz 200 16777216
.build/bench            invalid input.gz 200 16777216
.build/zlib-bounded             input.gz 200 16777216
.build/zlib-ng-bounded          input.gz 200 16777216
.build/zlib14-bounded           input.gz 200 16777216
.build/libdeflate-oneshot       input.gz 200 16777216
.build/goose-deflate-bench      input.gz 200 16777216
.build/baseline/archive/c99b049-v6/variants/release/bench oneshot input.gz 200 16777216
```

The third argument is the iteration count, the fourth is the output budget in
bytes. Each prints one line:

```
mode=oneshot input=44 output=1024 iterations=200 total_ns=... per_call_ns=... fnv1a=13576685282854019625 error=none peak_rss_kb=2588
```

`output` and `fnv1a` are the correctness check. `per_call_ns` is the only
timing figure, and it is meaningless from a single run.

## Prepare inputs only

```sh
python3 bench/runner.py --build-only
```

Writes `.build/bench-inputs/` and `inputs.json` without running anything.

## What is deliberately absent

- No cross-library ranking. Only rows marked comparable share a contract.
- No timing thresholds in CI. CI builds the harness and checks that it runs;
  it does not assert a speed.
- Local measurements are descriptive, not a CI performance promise.
