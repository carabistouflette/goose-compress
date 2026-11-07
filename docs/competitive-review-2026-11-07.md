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
| zlib | C, streaming | 1.3.2, `/usr/lib/libz.so.1.3.2` sha256 `6c78c5e5…075ca9` | `zlibVersion()`, `pacman -Q zlib` |
| zlib-ng | C, streaming, `zng_` prefix | 2.3.3, `/usr/lib/libz-ng.so.2.3.3` sha256 `471f8566…65ec6` | `ZLIBNG_VERSION`, `pacman -Q zlib-ng` |
| libdeflate | C, whole-buffer | 1.26, `/usr/lib/libdeflate.so.0` sha256 `22449665…3dd3dd` | `LIBDEFLATE_VERSION_STRING`, `pacman -Q libdeflate` |

Upstream release pages, read on 2026-11-07:

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
| Output budget enforced by the decoder | yes, `Limits.output_bytes` | no, caller sizes `avail_out` and must stop | no, caller allocates the whole output | no, "no output-size cap" | n/a |
| Input budget enforced by the decoder | yes, `Limits.input_bytes` | no, caller sizes `avail_in` | no | no | n/a |
| Member policy | yes, `Trailing.Reject/Allow/Members` + `Limits.members` | no, caller loops `inflateReset` | no | one member, rejects bytes after it | n/a |
| Trailing-data policy | yes, three explicit policies | no | no | reject only | n/a |
| Error taxonomy | 11 named `Error` values | `Z_*` codes + `msg` strings | `libdeflate_result` enum | trailing `bool` | n/a |
| Preset dictionary | yes, `dictionary`, with `DictionaryRequired`/`DictionaryMismatch` | yes, `inflateSetDictionary` | no | rejected | n/a |
| Handle lifetime / stale handle rejection | yes, generation-checked handle | caller owns `z_stream` | caller owns buffers | n/a | n/a |
| Independent decoders from several threads | yes, per-decoder lock, measured | yes, one `z_stream` per thread | yes, stateless | n/a | n/a |
| Same handle from two threads | contract violation, serialised, not freed under the caller | undefined | n/a | n/a | n/a |

Two rows decide the whole comparison:

1. **Output budget.** goose-compress refuses a stream that would exceed
   `output_bytes` with `Error.LimitExceeded` and stops. zlib and zlib-ng will
   happily keep decoding as long as the caller keeps handing them output space;
   the budget is the caller's loop, not the library's promise. libdeflate and
   goose-deflate have no budget at all — goose-deflate's own `PORTING.md` says
   "There is no streaming API and no output-size cap. A hostile stream can grow
   until the data stack stops it."
2. **Input budget.** Same shape. `Limits.input_bytes` is enforced inside the
   decoder, including bytes after the last member.

So a row that decodes a valid 8 MiB stream faster than goose-compress has not
been shown to be a better decoder for this package's job. It has been shown to
be faster at the part of the job that both do. That is why
`bench/compare/zlib_bounded.c` exists: it re-implements the budget, member and
trailing policies on top of zlib so that the zlib row is measured at the same
contract. Without it the zlib row would be a different program.

### The zlib reference row is a transliteration, not a wrapper

`bench/compare/zlib_bounded.c` is a line-by-line transliteration of
`compress.goose`'s `feed` loop onto `inflate`: the same input budget, the same
output budget, the same 64 KiB output step, the same one-byte sentinel that
distinguishes "budget reached" from "stream ended", the same member and
trailing policies, and the same status codes. `bench/compare/contract_parity.py`
drives both through the same 1081 cases and compares status, consumed,
produced, members and trailing on every one.

Result on both backends: **1081 cases, 2162 checks, 0 disagreements**, on zlib
1.3.2 and on zlib-ng 2.3.3. Three dictionary fixtures are excluded from the
parity run and reported as excluded, because the reference row does not
implement `inflateSetDictionary`; they are covered by `tests/contract.goose`
instead.

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

Both were driven through `bench/compare/contract_parity.py --reference`, which
compares the reference row against the Goose row case by case, so a backend
that classified a trailer mismatch as `Error.Malformed` would show up as a
disagreement rather than as a silent difference. This is a real fragility: a
future zlib that rewords those messages would turn `Error.Checksum` into
`Error.Malformed` without any test failing on the old backend. The mitigation
is that the parity run is part of `make bench-parity` and names the backend it
ran against.

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

### libdeflate — whole-buffer, no streaming, no budget

API: `libdeflate_gzip_decompress_ex` into a caller-allocated buffer, returning
a `libdeflate_result`.

- **Ownership.** The caller must know the output size in advance or allocate a
  guess and retry. There is no incremental state, so a stream that arrives in
  pieces must be buffered whole first.
- **Interop.** A C dependency with its own ABI, its own versioning and its own
  allocator hooks. It is the fastest whole-buffer decoder here, and it is
  fastest precisely because it is allowed to assume the whole input and the
  whole output are in memory at once.
- **API cost.** No member policy, no trailing policy, no dictionary, no
  streaming. Its result enum is coarser than this package's `Error`.
- **Measured as a one-shot row** (`bench/compare/libdeflate_oneshot.c`), on
  valid input only, labelled `comparable: false` for the bounded contract.

### zlib and zlib-ng — streaming, budget is the caller's loop

API: `inflate`/`inflateInit2`/`inflateReset`/`inflateSetDictionary`, with
`avail_in`/`avail_out`/`next_in`/`next_out`.

- **Ownership.** The caller owns the `z_stream` and both buffers. That is the
  same ownership model this package has, which is why the reference row can be
  a faithful transliteration.
- **Interop.** zlib is the ABI this package already links. zlib-ng is a
  drop-in with a `zng_` prefix and a different `z_stream` layout, so switching
  backends is a rebuild, not a rewrite — but it is a different shared object
  with a different `msg` wording, which is exactly why the checksum
  classification above is tested per backend rather than assumed.
- **API cost.** The budget, member and trailing policies are the caller's code.
  A caller that gets them wrong gets a decoder that is fast and unbounded. The
  reference row is the proof that getting them right costs measurable time, and
  that cost is part of the comparison rather than hidden in a footnote.

## What this means for ranking

- **Bounded streaming decode** (this package's contract): the only comparable
  rows are goose-compress and `zlib_bounded` on zlib 1.3.2 and on zlib-ng
  2.3.3. Those are ranked in `docs/benchmark.md` with the exact workload.
- **Whole-buffer decode of valid input**: libdeflate and goose-deflate are
  measured, and are not ranked against the bounded rows. A whole-buffer decoder
  being faster on valid input says nothing about whether it refuses the same
  inputs or enforces the same budget, and this document does not pretend
  otherwise.
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

# Trailer-mismatch classification, on each backend actually linked
for f in gzip-bad-crc gzip-bad-isize zlib-bad-adler; do
    fmt=gzip; case $f in zlib*) fmt=zlib;; esac
    ./.build/goose-probe        $fmt .build/fixtures/$f.in 16777216 33554432 65536 reject 1
    ./.build/zlib-bounded     --probe $fmt .build/fixtures/$f.in 16777216 33554432 65536 reject 1
    ./.build/zlib-ng-bounded  --probe $fmt .build/fixtures/$f.in 16777216 33554432 65536 reject 1
done
```

All three rows print `error=checksum_mismatch` for all three fixtures, with the
same `produced`, `consumed` and payload hash.

The clones live under `/tmp` and `.build/competitors/` (gitignored). Nothing
from another package is vendored into this repository.
