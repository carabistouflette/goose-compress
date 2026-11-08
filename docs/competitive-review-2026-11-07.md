# Competitive review, 2026-11-07

What goose-compress is measured against, what each alternative actually
promises, and what it would cost to use it instead. The point of the contract
matrix is that a decode rate is only comparable between two implementations
that refuse the same inputs and enforce the same budgets. Where they do not,
this document says so instead of ranking them.

Every identity below was verified on this machine, not quoted from a README.
The commands are in [How the identities were verified](#how-the-identities-were-verified).

## The field

| Implementation | Kind | Pinned identity | Verified how |
| --- | --- | --- | --- |
| goose-compress (this package) | Goose + C adapter over zlib | baseline `c99b049`, work `36d9b45`, this cycle's commits | `git log`, `.build/baseline/archive/c99b049-v6` (57/57 SHA256SUMS) |
| kaitk/goose-compress | Goose, encoder only | `ebf372eac4638a692ee89eba3204c666c101d7d9` | `git clone` + `git log -1` |
| Macho0x/goose-deflate | Goose, whole-buffer codec | `3b1d0d38be908dbb9bc2e26411bfd7449c570147` (v0.2.3) | `git clone` + `git log -1` |
| zlib (distribution) | C, streaming | 1.3.2, `/usr/lib/libz.so.1.3.2` sha256 `6c78c5e5…075ca9` | `zlibVersion()`, `pacman -Q zlib` |
| zlib (upstream latest) | C, streaming | 1.4, built static into `.build/competitors/zlib-1.4` | tarball sha256 `02dd1597…6ad584`, `ZLIB_VERSION` |
| zlib-ng | C, streaming, `zng_` prefix | 2.3.3, `/usr/lib/libz-ng.so.2.3.3` sha256 `471f8566…65ec6` | `ZLIBNG_VERSION`, `pacman -Q zlib-ng` |
| libdeflate | C, whole-buffer | 1.26, `/usr/lib/libdeflate.so.0` sha256 `22449665…3dd3dd` | `LIBDEFLATE_VERSION_STRING`, `pacman -Q libdeflate` |

Upstream release pages, read on 2026-11-07:

- zlib 1.4, released 2026-11-04 — <https://www.zlib.net/manual.html> states
  "version 1.4, November 4th, 2026" and `ZLIB_VERNUM 0x1400`. Its ChangeLog
  lists inflate fixes this package depends on: "Fix inaccurate total_in after
  Z_NEED_DICT returned", "Detect and report bad deflateSetDictionary() calls",
  "Fix window write overrun after inflateSync()", "Fix inflateGetDictionary()
  write past buffer on invalid window size", and the documented behaviour
  change "Raw deflate that completes exactly at the end of the output buffer
  now returns Z_STREAM_END instead of Z_OK". The distribution still ships
  1.3.2, so 1.4 is built static under `.build/competitors/zlib-1.4` and never
  replaces the system library.
- zlib 1.3.2, released 2026-02-17, "Address the findings of the just-completed
  7ASecurity audit" — <https://github.com/madler/zlib/releases/tag/v1.3.2>
- zlib-ng 2.3.3 — <https://github.com/zlib-ng/zlib-ng/releases>
- libdeflate 1.26, described by its own packagers as "a library for fast,
  whole-buffer DEFLATE-based compression and decompression" —
  <https://github.com/ebiggers/libdeflate/releases>

## Contract matrix

`yes` means the implementation enforces it. `no` means it does not, and the
consequence is named. This is the table that decides what may be ranked.

| Guarantee | goose-compress | zlib / zlib-ng (streaming) | libdeflate | goose-deflate | kaitk/goose-compress |
| --- | --- | --- | --- | --- | --- |
| Streaming decode | yes, `feed`/`finish` | yes, `inflate` | no, whole buffer only | no | n/a, encoder only |
| Output budget enforced by the decoder | yes, `Limits.output_bytes`, cumulative across `feed` | no, caller sizes `avail_out` and must stop | yes, `out_nbytes_avail` per call, `LIBDEFLATE_INSUFFICIENT_SPACE` | no, "no output-size cap" | n/a |
| Input budget enforced by the decoder | yes, `Limits.input_bytes`, cumulative | no, caller sizes `avail_in` | no, but a one-shot call knows `in_nbytes`, so the check is exact and free | no | n/a |
| Member policy | yes, `Trailing.Reject/Allow/Members` + `Limits.members` | no, caller loops `inflateReset` | no: first member only, `actual_in_nbytes` locates its end | one member, rejects bytes after it | n/a |
| Trailing-data policy | yes, three explicit policies | no | no policy, but `actual_in_nbytes < in_nbytes` detects it | reject only | n/a |
| Error taxonomy | 11 named `Error` values | `Z_*` codes + `msg` strings | 4-value `libdeflate_result`; no checksum/malformed split | trailing `bool` | n/a |
| Preset dictionary | yes, `dictionary`, with `DictionaryRequired`/`DictionaryMismatch` | yes, `inflateSetDictionary` | no | rejected | n/a |
| Cumulative budget across calls | yes | caller's loop | no, there are no calls | no | n/a |
| Handle lifetime / stale handle rejection | yes, generation-checked handle | caller owns `z_stream` | caller owns buffers | n/a | n/a |
| Independent decoders from several threads | yes, per-decoder lock, measured | yes, one `z_stream` per thread | yes, stateless | n/a | n/a |
| Same handle from two threads | contract violation, serialised, not freed under the caller | undefined | n/a | n/a | n/a |

Three things decide the comparison, and they are not the same thing:

1. **A bounded output buffer.** libdeflate has this: `out_nbytes_avail` is a
   real cap, and a stream that would exceed it returns
   `LIBDEFLATE_INSUFFICIENT_SPACE` instead of writing past the buffer. So
   libdeflate is *not* unbounded, and an earlier draft of this document was
   wrong to say it was. For a single call, the buffer cap and the output budget
   are the same number, which is why the libdeflate row is comparable to
   goose-compress `oneshot`.
2. **A cumulative budget across calls.** libdeflate does not have this, and
   cannot: there are no calls, because the whole input and the whole output are
   in memory at once. goose-compress's `Limits.output_bytes` is cumulative
   across `feed`, so a caller can hand it a stream in pieces and still be
   guaranteed a total. That guarantee is what a streaming decoder is for, and
   it is the one libdeflate cannot make.
3. **A refusal taxonomy.** libdeflate reports `LIBDEFLATE_BAD_DATA` for a
   malformed stream, a truncated stream and a trailer mismatch alike. It cannot
   tell a caller "the CRC was wrong" as opposed to "the Huffman code was
   invalid". goose-compress has eleven named errors. That is a contract
   difference, not a speed difference.

zlib and zlib-ng sit in between: they stream, but the budget is the caller's
loop, not the library's promise. A caller that gets the loop wrong gets a
decoder that is fast and unbounded. `bench/compare/zlib_bounded.c` exists to
show what the loop costs when it is written correctly, so the zlib row is
measured at the same contract rather than at a looser one.

goose-deflate is the only row with genuinely no cap: its own `PORTING.md` says
"There is no streaming API and no output-size cap. A hostile stream can grow
until the data stack stops it."

So a row that decodes a valid 8 MiB stream faster than goose-compress has not
been shown to be a better decoder for this package's job. It has been shown to
be faster at the part of the job that both do. The rows are grouped by which of
the three guarantees they make, and only rows in the same group are ranked.

### The zlib reference row shares the contract, with a different step strategy

`bench/compare/zlib_bounded.c` implements the same framing/input/output budget
contract over `inflate`. Its fixed 64 KiB output step differs from the Goose
implementation’s 4 KiB first step followed by 64 KiB steps. Both use the same one-byte sentinel that
distinguishes "budget reached" from "stream ended", the same member and
trailing policies, and the same status codes. `bench/compare/contract_parity.py`
drives both through the same 1081 cases and compares status, consumed,
produced, members and trailing on every one.

The original checker reported **1081 cases, 2162 counters, 0 disagreements** on
zlib 1.3.2, zlib-ng 2.3.3 and zlib 1.4. Those counters predate the
error-token/budget-guard correction described in `docs/validation.md`; they
do not establish that the old output-stop branch executed. Three dictionary fixtures are
excluded from the parity run and reported as excluded, because the reference row
does not implement `inflateSetDictionary`; they are covered by
`tests/contract.goose` instead.

zlib 1.4 is the interesting one, because it changes inflate behaviour this
package depends on: raw deflate that completes exactly at the end of the output
buffer now returns `Z_STREAM_END` instead of `Z_OK`. `feed` accepts both, and
the parity run confirms it: the same 1081 cases, the same answers.

The cases include: empty input, truncated at every prefix length, a corrupted
byte at the start, middle and end of the compressed stream, a corrupted CRC, a
corrupted ISIZE, a corrupted Adler-32, trailing garbage under each of the three
policies, a second member under each policy, output budget exactly equal to the
payload, one byte under, one byte over, and input slices larger than `UINT_MAX`
(the adapter clamps to `UINT_MAX` per call and the test proves it with a
4 GiB + 10 mmap).

### `is_checksum_message` is a message match, and it is tested per backend

`native/compress.c` classifies a trailer mismatch by matching zlib's `msg`
string against `"incorrect data check"` and `"incorrect length check"`. zlib
has no dedicated error code for "the trailer did not match", so there is no
code to test instead. That makes the classification a property of the *linked*
zlib, not of the ABI, and it must be re-verified whenever the backend changes.

It was verified on both backends actually linked here:

| Backend | gzip bad CRC | gzip bad ISIZE | zlib bad Adler-32 |
| --- | --- | --- | --- |
| zlib 1.3.2 | `Error.Checksum` | `Error.Checksum` | `Error.Checksum` |
| zlib-ng 2.3.3 | `Error.Checksum` | `Error.Checksum` | `Error.Checksum` |
| zlib 1.4 | `Error.Checksum` | `Error.Checksum` | `Error.Checksum` |

All three were driven through `bench/compare/contract_parity.py --reference`,
which compares the reference row against the Goose row case by case, so a
backend that classified a trailer mismatch as `Error.Malformed` would show up
as a disagreement rather than as a silent difference. This is a real fragility: a
future zlib that rewords those messages would turn `Error.Checksum` into
`Error.Malformed` without any test failing on the old backend. The mitigation
is that the parity run is part of `make bench-parity` and names the backend it
ran against.

### The libdeflate row was checked the same way, and disagrees on purpose

`bench/compare/libdeflate_oneshot.c` implements the one-shot bounded contract on
top of `libdeflate_gzip_decompress_ex`: `out_nbytes_avail` is the output budget,
`actual_in_nbytes < in_nbytes` is `Trailing.Reject`, and the input budget is a
length check. Running the same parity script against it is expected to report
disagreements, and it does:

```
$ python3 bench/compare/contract_parity.py --goose "$GOOSE" \
      --reference .build/libdeflate-oneshot \
      --output .build/contract-parity-libdeflate.json
contract parity: 2776 disagreement(s) over 1081 cases
```

Classified by the `error` field, over 1081 cases:

| Reference (libdeflate row) | Library (goose-compress) | Cases | Why |
| --- | --- | --- | --- |
| `unsupported_policy` | anything | 365 | the row implements `Trailing.Reject` only, and says so instead of reporting `none` |
| `malformed_stream` | `truncated_stream` | 53 | libdeflate has no truncation code |
| `malformed_stream` | `checksum_mismatch` | 33 | libdeflate has no trailer-mismatch code |
| `malformed_stream` | `dictionary_required` | 11 | libdeflate has no preset dictionary |
| `malformed_stream` | `limit_exceeded` | 6 | budget ordering, see below |
| `limit_exceeded` | `trailing_data` / `malformed_stream` / `checksum_mismatch` / `dictionary_required` / `truncated_stream` | 14 | budget ordering, see below |

The remaining disagreements are `members` (libdeflate does not count members),
and `produced` / `consumed` / `fnv1a` on a failure (libdeflate reports nothing
partial, goose-compress reports the prefix it decoded).

The 20 budget-ordering cases are a property of the harness, not of libdeflate:
a one-shot call is handed the whole input at once, so the input budget is a
length check applied before the decode, while goose-compress checks the budget
as it consumes. A stream that is both too large and malformed therefore reports
`limit_exceeded` in the row and the stream error in the library. libdeflate has
no input budget at all, so there is nothing to compare there.

What the row *does* agree on is the part that matters for the output budget:
at an exact budget, one byte under, one byte over and zero, both report
`none` / `limit_exceeded` / `none` / `limit_exceeded` on the same fixture. That
is the check that makes the row comparable at all.

## Interop, ownership and API cost

Before ranking anything, what it costs to use it.

### kaitk/goose-compress — encoder only

Its README's status table says `🔴 Decoding | Out of scope for now.` It cannot
be a decoder alternative at all. It is listed because a comparison that omits
the other Goose compression package would be a comparison against nothing, and
because it is the natural encoder counterpart if this package ever grows one.
No decode row is measured for it, and no ranking is implied.

### Macho0x/goose-deflate — whole-buffer, no budget

API: `inflate::decode(src: u8[:]) -> u8[>..], bool`, plus
`gzip::decompress`/`zlib::decompress` with the same shape.

- **Ownership.** The decoder allocates and returns the whole output. The caller
  cannot cap it, cannot stream it, and cannot ask "how much would this produce
  before I commit the memory". For this package's job — decode untrusted input
  under a byte budget — that is not a slower version of the same thing, it is a
  different thing.
- **Interop.** Pure Goose, no C, no `compress.build` native manifest, no
  `-lz`. That is a genuine advantage: it builds with the Goose compiler alone.
  It is also why it cannot reuse zlib's hardened inflate.
- **API cost.** Failure is a trailing `bool`, so a caller cannot tell a
  truncated stream from a bad checksum from a malformed Huffman code. Its
  `PORTING.md` states the limits plainly: no streaming, no output cap, preset
  dictionaries rejected, one gzip member, bytes after the member rejected.
- **Measured at its own scope only.** `bench/compare/goose_deflate_bench.goose`
  times a whole-member decode of a buffer the caller already holds. The row is
  labelled `comparable: false` in `bench/runner.py`. It is reported, never
  ranked against a bounded row.

### libdeflate — whole-buffer, bounded per call, no streaming

API: `libdeflate_gzip_decompress_ex(decompressor, in, in_nbytes, out,
out_nbytes_avail, &actual_in_nbytes, &actual_out_nbytes)`, returning a
`libdeflate_result`.

- **Ownership.** The caller owns both buffers and must size the output. The cap
  is enforced: libdeflate will not write past `out_nbytes_avail`, and reports
  `LIBDEFLATE_INSUFFICIENT_SPACE` when the stream wants more. There is no
  incremental state, so a stream that arrives in pieces must be buffered whole
  first, and the caller must know or guess the output size before the call.
- **Interop.** A C dependency with its own ABI, its own versioning and its own
  allocator hooks. It is the fastest whole-buffer decoder here, and it is
  fastest precisely because it is allowed to assume the whole input and the
  whole output are in memory at once.
- **API cost.** No cumulative budget, no member policy, no trailing policy, no
  dictionary, no streaming, and a four-value result enum that cannot separate a
  checksum mismatch from a malformed stream. `actual_in_nbytes` does let a
  caller detect trailing bytes after the first member, which is what
  `bench/compare/libdeflate_oneshot.c` does to implement `Trailing.Reject`.
- **Measured as a bounded one-shot row** (`bench/compare/libdeflate_oneshot.c`),
  labelled `comparable: true` against goose-compress `oneshot` and against
  nothing else. It does not rank the streaming rows, because it cannot make the
  cumulative-budget guarantee they make.

### zlib and zlib-ng — streaming, budget is the caller's loop

API: `inflate`/`inflateInit2`/`inflateReset`/`inflateSetDictionary`, with
`avail_in`/`avail_out`/`next_in`/`next_out`.

- **Ownership.** The caller owns the `z_stream` and both buffers. That is the
  same ownership model this package has, which is why the reference row can be
  a matching framing/budget contract with a different output-step strategy.
- **Interop.** zlib is the ABI this package already links. zlib-ng is a
  drop-in with a `zng_` prefix and a different `z_stream` layout, so switching
  backends is a rebuild, not a rewrite — but it is a different shared object
  with a different `msg` wording, which is exactly why the checksum
  classification above is tested per backend rather than assumed.
- **API cost.** The budget, member and trailing policies are the caller's code.
  A caller that gets them wrong gets a decoder that is fast and unbounded. The
  reference row is the proof that getting them right costs measurable time, and
  that cost is part of the comparison rather than hidden in a footnote.

### zlib-ng as a backend for this package, not just as a comparator

The comparison above answers "how fast is zlib-ng". The question that decides
anything is "what would it cost us to use it", and that is a different
measurement. Three routes were tried, and only the third is honest:

1. `goose build -DGC_ZLIB_NG` — **does not work**. `-D` reaches the generated
   program, not the package's native C, so the package object is still compiled
   against `<zlib.h>` and `ldd` shows `libz.so.1`. Confirmed with `--dry-run`.
2. A `LIBRARY_PATH` symlink to a `libz.so` that is really zlib-ng — **does not
   work** either, for the same reason: the include is resolved at compile time.
3. `native/zlib_backend.h` maps the zlib spellings onto `zng_` entry points
   under `-DGC_ZLIB_NG`, and `make native-ng` runs the same 125 driver checks
   and the same 58 concurrency checks against it. This measures the adapter on
   zlib-ng, and `bench/compare/goose_adapter.c` measures it without the Goose
   layer.

There is a fourth route that needs no code change at all, and it is the one
that answers the real question: zlib-ng built with `-DZLIB_COMPAT` installs a
`libz.so.1` with zlib's ABI. Pointing `LD_LIBRARY_PATH` at it runs the
**unchanged** Goose binary on the faster backend. `make bench-zlib-ng-compat`
builds it under `.build/competitors`; nothing on the system is replaced.

| Input | this package on zlib | this package on zlib-ng | ratio |
| --- | ---: | ---: | ---: |
| 1 KiB, exact budget | 1,105 ns | 340 ns | 3.29x |
| 64 KiB, exact budget | 48,102 ns | 11,099 ns | 4.34x |
| 1 MiB, exact budget | 761,904 ns | 161,816 ns | 4.70x |
| 8 MiB, exact budget | 6,207,912 ns | 1,412,928 ns | 4.39x |

The ABI promise is worth exactly what its verification is worth, so the whole
suite was re-run against that link: contract parity 1081 cases and 2382 checks
with 0 disagreements, the driver's 125 checks, the concurrency suite's 58
checks, and ASan+UBSan on both GCC and Clang with live instrumentation. The
checksum classification was checked on the compat ABI specifically, because
`is_checksum_message` reads zlib's message text and a compat build is a
different shared object.

**Why it is not the default.** zlib-ng is not installed everywhere, the
maintained codec is zlib, and a backend choice is a packaging decision rather
than a library one. What this cycle establishes is the size of the prize and
that the adapter does not stand in its way: on zlib-ng the adapter costs 15% at
1 KiB and 0.1% at 8 MiB, so the remaining gap is the Goose layer, not the
native code.

## What this means for ranking

- **Bounded streaming decode** (this package's contract): goose-compress,
  `zlib_bounded` on zlib 1.3.2, on zlib-ng 2.3.3 and on zlib 1.4. These share
  a contract and are ranked in `docs/benchmark.md` with the exact workload.
- **Bounded one-shot decode**: goose-compress `oneshot` and
  `libdeflate_oneshot`. Both cap the output; neither can be handed a stream in
  pieces. Ranked against each other, and not against the streaming rows.
- **Unbounded whole-buffer decode**: goose-deflate only. Measured, never
  ranked.
- **Encoding**: out of scope for this cycle. kaitk/goose-compress is the
  reference if it ever comes in scope.

No row here is declared a universal winner. Where an alternative is ahead on a
named workload, `docs/benchmark.md` names the workload and the next iteration
that the profile justifies.

## How the identities were verified

```sh
# Goose alternatives, at the commits named above
git clone https://github.com/kaitk/goose-compress.git /tmp/goose-c1-compress-competitors/goose-compress
git -C /tmp/goose-c1-compress-competitors/goose-compress log -1 --format='%H %s'
git clone https://github.com/Macho0x/goose-deflate.git /tmp/goose-c1-compress-competitors/goose-deflate
git -C /tmp/goose-c1-compress-competitors/goose-deflate log -1 --format='%H %s'

# C backends actually linked
python3 -c 'import zlib; print(zlib.ZLIB_VERSION, zlib.ZLIB_RUNTIME_VERSION)'
grep -m1 'ZLIBNG_VERSION ' /usr/include/zlib-ng.h
grep -m1 'LIBDEFLATE_VERSION_STRING' /usr/include/libdeflate.h
sha256sum /usr/lib/libz.so.1.3.2 /usr/lib/libz-ng.so.2.3.3 /usr/lib/libdeflate.so.0

# Contract parity of the reference row, per backend
make bench-parity
python3 bench/compare/contract_parity.py --goose "$GOOSE" --reference .build/zlib-ng-bounded
make bench-zlib14
python3 bench/compare/contract_parity.py --goose "$GOOSE" --reference .build/zlib14-bounded

# The libdeflate row, which is expected to disagree: see the table above
python3 bench/compare/contract_parity.py --goose "$GOOSE" \
    --reference .build/libdeflate-oneshot \
    --output .build/contract-parity-libdeflate.json

# Trailer-mismatch classification, on each backend actually linked
for f in gzip-bad-crc gzip-bad-isize zlib-bad-adler; do
    fmt=gzip; case $f in zlib*) fmt=zlib;; esac
    ./.build/goose-probe        $fmt .build/fixtures/$f.in 16777216 33554432 65536 reject 1
    ./.build/zlib-bounded     --probe $fmt .build/fixtures/$f.in 16777216 33554432 65536 reject 1
    ./.build/zlib-ng-bounded  --probe $fmt .build/fixtures/$f.in 16777216 33554432 65536 reject 1
done

# The same classification, on every backend, as a suite rather than a spot check
for ref in zlib-bounded zlib-ng-bounded zlib14-bounded zlib-ng-compat-bounded; do
    python3 bench/compare/contract_parity.py --goose "$GOOSE" \
        --reference .build/$ref --output .build/contract-parity-$ref.json
done

# The zlib-ng backend under this package's own API, and through its compat ABI
make native-ng WRAP_INFLATE=zng_inflate
make bench-zlib-ng-compat
LD_LIBRARY_PATH=$PWD/.build/competitors/zlib-ng-compat/lib ./.build/driver --selftest
LD_LIBRARY_PATH=$PWD/.build/competitors/zlib-ng-compat/lib ./.build/concurrency --selftest
```

All four reference rows print `error=checksum_mismatch` for all three fixtures,
with the same `produced`, `consumed` and payload hash. `contract_parity.py` now
corrupts a payload byte and each trailer field of every fixture and requires the
two sides to agree on the error name: 110 corrupted cases per backend, on top of
the 1081 contract cases, all green on zlib 1.3.2, zlib 1.4, zlib-ng's `zng_` API
and zlib-ng's `ZLIB_COMPAT` ABI.

The clones live under `/tmp` and `.build/competitors/` (gitignored). Nothing
from another package is vendored into this repository.
