# Benchmark harness

Seven rows, one contract each, and a reported output length plus FNV-1a digest
that must match across every row that decodes the same payload. A row that did
less work is rejected rather than reported.

```
bench/
  bench.goose                    goose-compress: oneshot / stream / fresh / tail / invalid
  compare/zlib_bounded.c         the same zlib, streaming, under the same contract
  compare/zlib_oneshot.c         the same zlib, one-shot, no bounds
  compare/libdeflate_oneshot.c   libdeflate 1.26, one-shot, no bounds
  runner.py                      the serialised measurement runner
  measure.py                     the older single-median runner
```

## Contracts, and which rows may be ranked

| Row | Contract | Buffer | Comparable |
| --- | --- | --- | --- |
| goose-compress oneshot | bounded: output budget, input budget, 1 member, trailing rejected | reused | yes |
| goose-compress stream | bounded, 64 KiB steps | reused | yes |
| goose-compress fresh | bounded, 64 KiB steps | allocated per call | yes |
| goose-compress tail | bounded: `finish` + `close` after the last byte | reused | yes |
| zlib bounded | bounded, same framing policy | reused | yes |
| zlib oneshot | unbounded: no budgets, no trailing check | reused | **no** |
| libdeflate oneshot | unbounded: no budgets, no trailing check | reused | **no** |

`zlib bounded` is the same zlib under the same framing policy, so a difference
there is the cost of the bounded incremental state machine, not a speed
ranking. `libdeflate oneshot` is a throughput reference: it is one-shot and
unbounded, so it does not rank a streaming decoder. No row here supports a
state-of-the-art claim.

## Build

```sh
make bench
```

Compiles the four binaries into `.build/` and writes the deterministic inputs
to `.build/bench-inputs/`. It measures nothing.

## Plan, without timing

```sh
make bench-plan
```

Prints every command, the input set, the invocation count and the total decode
volume. Nothing is executed.

## Run

```sh
make bench-run
```

or directly:

```sh
python3 bench/runner.py --samples 20 --output .build/benchmark.json
```

The runner keeps every raw sample, alternates the harness order each round so
slow drift lands on all rows, discards two warm-up rounds, and reports
min/max/median/mean/stdev/MAD/p90/p99/IQR plus peak RSS. It records provenance
before and after the run and fails if the two differ. It refuses to start when
the 1-minute load average is above `--max-load` (default 0.5).

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
.build/zlib-oneshot             input.gz 200 16777216
.build/libdeflate-oneshot       input.gz 200 16777216
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
- No `hyperfine`, no `perf`, no CPU pinning. Those would be a separate,
  documented methodology.
