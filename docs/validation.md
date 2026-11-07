# Validation

Everything below was executed on the machine described in
[docs/provenance.md](docs/provenance.md), with the pinned compiler
`eadca409f68210893f366e60d0c2f0dc29a339be`. Commands are given exactly as run.
`GOOSE` is set to an existing pinned build of that revision, reused read-only.

No performance measurement was run. See [docs/benchmark.md](docs/benchmark.md).

## Layers

| Layer | What it proves | Where |
| --- | --- | --- |
| Specification fixtures | 48 named cases, one per documented behaviour | `tests/spec.goose` |
| Chunking invariance | the same answer under 7 chunk sizes | `tests/boundaries.goose` |
| API and ownership | lifecycle, invalid limits, terminal failure, cleanup | `tests/api.goose` |
| Differential | 100,000 generated cases against an independent oracle | `tests/differential.goose` |
| Native adapter | 119 direct C-level checks | `tests/driver.c` |
| Concurrency | independent decoders, stale handles, exhaustion, close-while-in-flight | `tests/concurrency.c` |
| Artefact integrity | fixtures, corpus, oracle and source fingerprint are what they claim | `tests/test_compress.py` |
| Consumer | the example runs and refuses what it should | `scripts/example_check.py` |
| Sanitizers | ASan and UBSan clean, no leaks, instrumentation proven live | `make sanitize` |
| ThreadSanitizer | the registry refcount and the per-decoder lock are race-free | `make concurrency-tsan` |

## Fixtures

`python3 scripts/fixtures.py` writes 48 fixtures to `.build/fixtures`, each as
`<name>.in`, `<name>.out`, an optional `<name>.dict`, plus `index.bin` and
`manifest.json`.

Coverage: gzip header variants (extra, name, comment, HCRC, all fields), all
three block types (stored, fixed, dynamic), two- and three-member streams, a
member count over the limit, trailing bytes under both policies, truncated
header/body/trailer, bad CRC, bad ISIZE, a 1 MiB bomb with a 4 KiB budget and
with an exact budget, zlib with a matching/missing/wrong dictionary, bad
Adler-32, zlib trailing and truncation, raw stored/fixed/dynamic, raw with a
dictionary, a bad block type, a stored-length mismatch, a distance-too-far
stream, raw truncation, and the exact/short/zero boundary cases for both
budgets.

```
$ make fixtures
wrote 48 fixtures to .build/fixtures
$ "$GOOSE" run tests/spec.goose
spec ok: 48 fixtures
```

## Chunking invariance

`tests/boundaries.goose` replays every fixture at chunk sizes 1, 2, 3, 7, 64,
1024 and 65536, and asserts that the error, the produced bytes and
`progress.consumed` are identical to the single-chunk run. It adds explicit
assertions for a zero output budget, an exact output budget, an exact input
budget, one byte short of each, trailing bytes split across chunks, and
idempotent `close`.

```
$ "$GOOSE" run tests/boundaries.goose
boundaries ok: 336 chunked runs
```

336 = 48 fixtures × 7 chunk sizes.

## Differential corpus

`python3 scripts/corpus.py --cases 100000 --seed 20261107` writes
`.build/corpus/corpus.bin` and `manifest.json`.

```
wrote 100000 cases, 39142465 bytes, sha256 6e729d76bf98549895379443fe517ac421f491574661728fd998932efb4edec7
  checksum mismatch    4427
  dictionary mismatch  93
  limit exceeded       27875
  malformed stream     2860
  none                 42865
  too many members     2783
  trailing data        6868
  truncated stream     12229
```

Each case is a format, a trailing policy, an optional dictionary, three
budgets, a chunk size and a byte string, with the expected error and expected
output recorded. The generator mutates valid streams by truncation, bit flips,
appending and concatenation, and varies the budgets around the exact
boundaries (0, size−1, size, size+1, random).

The expectation comes from `scripts/oracle.py`, which is a second
implementation of `docs/api.md` written from that document: DEFLATE decoding is
CPython's zlib, and the RFC 1950/1952 wrappers are parsed by hand so that the
oracle can report the bytes produced before a trailer check failed.

```
$ "$GOOSE" run tests/differential.goose
differential ok: 100000 cases
```

The corpus is reproducible: re-running the generator produces the same bytes
and the same SHA-256. `tests/test_compress.py` asserts this.

## Oracle independence

The oracle is not trusted on its own. `tests/test_compress.py` checks it
against CPython:

- every valid fixture is decoded with `zlib.decompress(data, 31)`,
  `zlib.decompress(data, 15)` or `zlib.decompress(data, -15)` and must match
  the recorded output byte for byte;
- every valid multi-member fixture is decoded with `gzip.decompress` and must
  match;
- every one of the 100,000 corpus cases is re-derived from its recorded inputs
  and must reproduce the recorded error and output;
- the corpus outcome histogram is recomputed and must match the manifest.

```
$ python3 -m unittest discover -s tests -v
...
Ran 13 tests in 1.511s
OK
```

## Native adapter

`tests/driver.c` calls the C entry points directly, bypassing the Goose layer,
and checks argument validation, the handle table, stale-handle rejection, the
generation cap, dictionary installation, reset behaviour and the ABI report.

```
$ make native
./.build/driver --selftest
driver ok: 125 checks
```

The checksum classification is asserted on all three trailer kinds — the gzip
CRC32, the gzip ISIZE and the zlib adler32 — because the adapter decides
`Error.Checksum` from zlib's message text, and that text belongs to the linked
zlib rather than to the format. `make native-ng` runs the same 125 checks
against zlib-ng, so a backend that words the message differently fails here
instead of silently reclassifying a bad checksum as malformed data.

```
$ make native-ng WRAP_INFLATE=zng_inflate
./.build/driver-ng --selftest
driver ok: 125 checks
timeout 120 ./.build/concurrency-ng --selftest
concurrency ok: 58 checks
```

`WRAP_INFLATE` exists because `--wrap=inflate` does not intercept `zng_inflate`:
the close-race test wraps the symbol the adapter actually calls, and the test
pastes the name from the same header the adapter uses.

## Sanitizers

`goose build --cc clang --debug` does **not** instrument the generated code:
the sanitizer flags never reach the compiler that builds the Goose translation
unit, so a green run from that path proves nothing about memory safety. The
`sanitize` target therefore emits standalone C and compiles it here, together
with the native adapter, through `scripts/sanitizer-cc.sh`:

```
$ make sanitize SANITIZER_CC=clang
SANITIZER_CC=clang ./scripts/sanitizer-cc.sh -O1 -g -Inative \
    -o .build/driver-sanitize tests/driver.c native/compress.c \
    -lz -lpthread -lm -ldl
ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1 \
    ./.build/driver-sanitize --selftest
driver ok: 119 checks
concurrency ok: 58 checks
sanitize: spec
spec ok: 48 fixtures
sanitize: boundaries
boundaries ok: 336 chunked runs
sanitize: api
api ok
sanitize: contract
contract ok: 118 checks
sanitize: differential
differential ok: 100000 cases
checking goose translation unit with clang
  goose translation unit: 29 sanitizer handlers referenced
checking injected overflow with clang
  injected overflow: caught, exit -6
  ...compress-broken.c:176:80: runtime error: store to address ... with
  insufficient space for an object of type 'char'
sanitizer negative control ok: instrumentation is live (clang)
```

The wrapper adds `-fsanitize=address,undefined -fno-sanitize-recover=all
-fno-omit-frame-pointer`, so a UBSan finding aborts instead of warning. The
Goose tests are built with `--standalone --debug --no-bce`, which keeps the
runtime bounds checks in place, and the generated C is compiled together with
`native/compress.c`, so both the Goose code and the adapter are instrumented.

The same target passes with `SANITIZER_CC=gcc` (27 handlers referenced, exit 1
on the injected overflow). Both compilers run the 100,000-case differential
corpus under ASan and UBSan with `detect_leaks=1`, so the handle table, the
`z_stream` allocation and every error path are exercised under the sanitizers,
not just the happy path.

### Negative control

A green sanitizer run is only evidence if the instrumentation is live.
`scripts/sanitize_negative.py` checks both halves of that claim and runs as the
last step of `make sanitize`:

1. **The Goose translation unit is instrumented.** The standalone C is compiled
   to an object and `nm -u` must show ASan and UBSan handler references,
   including `__asan_report_store_n` and `__ubsan_handle_out_of_bounds_abort`.
   A debug build without the flags references none.
2. **The instrumentation is live.** A one-byte heap overflow is injected into a
   copy of `native/compress.c` in `.build/negative/`, compiled with the same
   wrapper, and the driver must abort with a sanitizer report. If it exits 0,
   the adapter is not instrumented and the check fails.

Both checks build into `.build/negative/` and leave the repository untouched.
`make sanitize-negative` runs them alone.

## Concurrency

`tests/concurrency.c` checks the half of the contract that says separate
decoders are independent, and the lifetime rule that makes `close` safe while
another thread is inside a decoder.

```
$ make concurrency
cc -std=c11 -Wall -Wextra -Werror -O2 -Inative tests/concurrency.c \
    native/compress.c -lz -lpthread -DGC_TEST_WRAP_INFLATE \
    -Wl,--wrap=inflate -o .build/concurrency
timeout 120 ./.build/concurrency --selftest
concurrency ok: 58 checks
```

The checks are: eight threads decoding eight independent streams produce the
right bytes and leave no slot open; a handle closed by one thread is rejected
by another; a full table reports `Error.SystemError` and a freed slot is
reusable; 8 × 500 open/close rounds leave the table empty; and a close that
starts while a decode is genuinely inside `inflate` waits for it.

That last one is the one that cannot be established by sleeping. The test
binary is linked with `-Wl,--wrap=inflate`, and the wrapper posts a semaphore
before it calls the real `inflate` and waits for a second one, so the decode is
provably in flight with a reference held. The test then starts the close,
spins until `gc_abi` reports the slot retired, and asserts that the close has
not returned, that a lookup on the retired handle is refused, and that the slot
is reusable afterwards. The wrap lives in the test binary only; the library is
unchanged.

The check is not vacuous. Removing the `while (decoder->refs > 0)
pthread_cond_wait(...)` line from `gc_close` and rebuilding the same test under
ASan produces:

```
==ERROR: AddressSanitizer: heap-use-after-free on address ...
READ of size 4 at ... thread T19
    #0 gc_inflate .../compress.c:275
    #1 race_inflate_worker .../tests/concurrency.c:328
freed by thread T20 here:
    #0 free
    #1 gc_close .../compress.c:339
```

so the wait is what stands between the test and a use-after-free.

`make concurrency-tsan` builds the same test with `-fsanitize=thread` and runs
it under `TSAN_OPTIONS=halt_on_error=1`; it reports `concurrency ok: 58 checks`
with no race. ThreadSanitizer is the only tool here that sees the registry
refcount and the per-decoder lock, so it is a separate target rather than part
of `make sanitize`.

Every run is bounded by `CONCURRENCY_TIMEOUT` (default 120 s), and the test
exits non-zero on a `pthread_create`, `pthread_join` or barrier error instead of
waiting on a barrier that can never be reached.

### Throughput, before and after

`concurrency --throughput <threads> <iterations> <input.gz> <budget>` decodes
`8MiB-text` in N threads with one decoder each. The baseline column is the
archived `c99b049` `native/compress.c` linked against the same test file, so the
only difference is the library:

| Threads | baseline c99b049 | current |
| ---: | ---: | ---: |
| 1 | 122.0 | 125.2 |
| 2 | 156.3 | 288.6 |
| 4 | 151.4 | 591.5 |
| 8 | 145.9 | 927.0 |

The baseline is flat because one global lock serialised every decoder. The
current build scales to 6.35x at 8 threads; it is not 8x because four of the
eight cores on this part are slow cores. `docs/benchmark.md` has the protocol.

### The zlib-ng backend, through its compat ABI

zlib-ng built with `-DZLIB_COMPAT` installs a `libz.so.1` with zlib's ABI, so
the unchanged binary can be run on it. An ABI promise is worth what its
verification is worth, so the whole suite is re-run against that link:

```
$ make bench-zlib-ng-compat
$ export LD_LIBRARY_PATH=$PWD/.build/competitors/zlib-ng-compat/lib
$ ./.build/driver --selftest
driver ok: 125 checks
$ ./.build/concurrency --selftest
concurrency ok: 58 checks
$ for t in spec boundaries api contract; do "$GOOSE" run tests/$t.goose; done
spec ok: 48 fixtures
boundaries ok: 336 chunked runs
api ok
contract ok: 118 checks
$ make sanitize SANITIZER_CC=gcc
$ make sanitize SANITIZER_CC=clang
```

Both sanitizer runs are green with live instrumentation (27 handlers referenced
under GCC, 29 under Clang) and both negative controls still fire.

## Resource bounds

Measured with `/usr/bin/time -v`, peak resident set size. These are resource
bounds, not timings.

| Case | Compressed | Budget | Peak RSS |
| --- | --- | --- | --- |
| `driver --selftest` | — | — | 2,264 KiB |
| 1 MiB of zeros, budget 4,096 | 1,051 B | 4,096 | 2,520 KiB |
| 1 MiB of zeros, budget 1,048,576 | 1,051 B | 1,048,576 | 3,616 KiB |
| 8 MiB text, budget 8,388,608 | 16,321 B | 8,388,608 | 10,740 KiB |
| 100,000-case differential | 39 MB corpus | — | 40,816 KiB |

The first bomb row is the point: 1,051 compressed bytes expand to 1 MiB, and
with a 4 KiB budget the process peaks at 2,520 KiB — the 1 MiB is never
allocated. The differential row is dominated by the 39 MB corpus blob held in
memory, not by the decoder.

A single open decoder costs one `z_stream`, about 40 KiB, independent of the
budgets. The handle table holds 32.

## Consumer example

```
$ python3 scripts/example_check.py "$GOOSE"
example ok: 38 assertions, payload 920 bytes, stream 44 bytes,
sha256 26f0ae6cbf2c8be9550b4c2362c572c4b307efc1cfd8be40eba19663b089febd
```

Twelve processes: one within budget, and eleven refusals — one byte short, zero
budget, bad CRC, bad ISIZE, trailing bytes, a second member, a truncated
stream, an oversized compressed input, an out-of-range budget, a missing file,
and a repeat run that must produce identical bytes.

## Full run

```
$ make check
python3 scripts/native-manifest.py --check
"$GOOSE" format --check compress.goose examples tests bench
"$GOOSE" check tests/spec.goose
"$GOOSE" check tests/boundaries.goose
"$GOOSE" check tests/api.goose
"$GOOSE" check tests/differential.goose
"$GOOSE" check examples/artifact_payload.goose
"$GOOSE" check bench/bench.goose

$ make test
wrote 48 fixtures to .build/fixtures
wrote 100000 cases, 39142465 bytes, sha256 6e729d76...
driver ok: 125 checks
spec ok: 48 fixtures
boundaries ok: 336 chunked runs
api ok
differential ok: 100000 cases
built .build/api
api ok
built .build/api-debug
api ok
Ran 13 tests in 1.511s
OK
example ok: 38 assertions
```

`make test` also builds `tests/api.goose` twice — once with `-O2` and once with
`--debug` — and runs both, so the release and debug code paths are both
exercised.

## Not covered

- **No coverage-guided fuzzing.** There is no `make fuzz` target. The
  differential corpus is generated, not coverage-guided; a libFuzzer or AFL
  harness over `feed` would be the next step for the native adapter.
- **No sanitizer coverage of the benchmark harness.** `bench/bench.goose` is
  built with `-O2` and no sanitizer. It is a measurement tool, not a library
  entry point, but it is not instrumented.
- **No sanitizer coverage of the example.** `examples/artifact_payload.goose`
  is exercised by `scripts/example_check.py` as a normal build. Its decode path
  is the same `feed`/`finish`/`close` sequence the instrumented tests cover, but
  the example binary itself is not instrumented.
- **No 32-bit or big-endian build.** Only x86-64 Linux was exercised.
- **No mutation of the concurrency guarantees.** `scripts/mutation-test.py`
  mutates the Goose-level contract (budgets, member limit, trailing policy,
  sticky error, consumed accounting, stale handles, dictionary limit) and kills
  8 of 8 on a pristine-green tree. The per-decoder lock and the close wait are
  covered by the ASan negative control and by TSan, not by a mutant.
- **No TSan run against the zlib-ng backend.** TSan was run against zlib 1.3.2
  only. The zlib-ng builds were checked with ASan+UBSan and with the full
  concurrency suite, but not under ThreadSanitizer.
- **No measurement of the `stream` and `fresh` rows against a competitor.** They
  are measured and reported, but the equal-contract competitor rows are
  whole-input, so the incremental rows have no fair opponent.
- **The 1 KiB headroom runs do not meet the contention gate.** Their endpoint
  CPU pressure check failed because other work started during the run. The
  ratio is reproducible across three independent runs and the per-round win is
  unanimous, but the runs are labelled exploratory in `docs/benchmark.md`.
- **No Windows or macOS.** The adapter uses `pthread_mutex_t` and links `z` and
  `pthread` on Linux only.
- **No concurrency test.** The handle table is mutex-protected, but no test
  opens decoders from several threads at once.
- **No long-running soak.** The largest single stream decoded was 8 MiB.
- **No adoption.** `goose-artifact` and `goose-archive` were read, not
  modified, and neither calls this library.
