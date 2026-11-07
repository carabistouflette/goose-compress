# Benchmark harness

Seven rows measure valid payloads under the contracts below. Two additional
Goose-only rows measure rejection of a truncated stream and a bad gzip
checksum. Valid rows must match the fixture output length and FNV-1a digest;
rejection rows must return the specified error and keep the same produced
length and digest across samples.

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

The two invalid-input rows are deliberately non-comparable. They measure the
bounded library's rejection path; the one-shot references do not provide the
same resource-limit contract.

`zlib bounded` is the same zlib under the same framing policy, so a difference
there is the cost of the bounded incremental state machine, not a speed
ranking. `libdeflate oneshot` is a throughput reference: it is one-shot and
unbounded, so it does not rank a streaming decoder. No row here supports a
state-of-the-art claim.

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
IQR plus peak RSS. It records the Goose and C compiler paths, versions and
binary hashes; measured source and benchmark binary hashes; resolved shared
library paths and hashes; compiler flags; git state; CPU model/governor and
load average before and after. Load average is recorded but excluded from the
static-identity comparison because the benchmark itself changes it. The runner
refuses to start when the 1-minute load average is above `--max-load` (default
0.5), and rejects a run if compiler, source, binary, dependency or other static
identity changes.

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
- Local measurements are descriptive, not a CI performance promise.
