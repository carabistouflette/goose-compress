# Benchmark harness

Four harnesses, one contract: one gzip stream in, the whole payload out, and a
reported output length plus FNV-1a digest that must match across all of them.

```
bench/
  bench.goose                 goose-compress, modes oneshot / stream / invalid
  compare/zlib_oneshot.c      the same zlib, called one-shot with no bounds
  compare/libdeflate_oneshot.c  libdeflate 1.26, one-shot with no bounds
  measure.py                  generates inputs, runs the harnesses, records provenance
```

## Build

```sh
make bench
```

This compiles the three binaries into `.build/` and writes the deterministic
inputs to `.build/bench-inputs/`. It measures nothing.

## Run

```sh
make bench-run
```

or directly:

```sh
python3 bench/measure.py --output .build/benchmark.json --repetitions 7
```

Run it alone. Concurrent runs on one CPU make every row meaningless.

## Single harness invocation

Each binary takes the same arguments, so a row can be reproduced by hand:

```sh
.build/bench            oneshot input.gz 200 16777216
.build/bench            stream  input.gz 200 16777216
.build/bench            invalid input.gz 200 16777216
.build/zlib-oneshot              input.gz 200 16777216
.build/libdeflate-oneshot        input.gz 200 16777216
```

The third argument is the iteration count, the fourth is the output budget in
bytes. Each prints one line:

```
mode=oneshot input=44 output=1024 iterations=200 total_ns=... per_call_ns=... fnv1a=13576685282854019625 error=none
```

`output` and `fnv1a` are the correctness check. `per_call_ns` is the only
timing figure, and it is meaningless from a single run.

## Prepare inputs only

```sh
python3 bench/measure.py --build-only
```

Writes `.build/bench-inputs/` and `inputs.json` without running anything. This
is what `make bench` relies on for the input set.

## What is deliberately absent

- No cross-library ranking. The rows differ in bounds and allocation.
- No timing thresholds in CI. CI builds the harness and checks that it runs;
  it does not assert a speed.
- No `hyperfine`, no `perf`, no CPU pinning. Those would be a separate,
  documented methodology.
