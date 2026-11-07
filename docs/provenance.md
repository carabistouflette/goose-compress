# Provenance and overlap

## Source identity

| Item | Value |
| --- | --- |
| Compiler | Goose, commit `eadca409f68210893f366e60d0c2f0dc29a339be` |
| Compiler binary used for validation | `goose eadca40 (runtime 3025e5810379)` |
| Compiler binary | an existing pinned build, reused read-only and not rebuilt |
| zlib linked by the library | 1.3.2 (`version_number()` = 10302) |
| `sizeof(z_stream)` | 112 |
| libdeflate (benchmark competitor only) | 1.26 |
| zlib 1.4 (benchmark competitor only) | built static under `.build/competitors/zlib-1.4` |
| zlib-ng (benchmark competitor and optional backend) | 2.3.3, `zng_` API and a `-DZLIB_COMPAT` build |
| C compiler | GCC 16.2.1; Clang for sanitizers |
| Python (oracle, fixtures, corpus) | 3.14.9 |
| CPU | Intel Core Ultra 7 258V, 8 cores, hybrid, no SMT |

`GOOSE_REVISION` holds the compiler commit. `compress.build` holds a SHA-256
over `scripts/native-manifest.py` and every file under `native/`, so a change
to the adapter invalidates Goose's local package cache. `make format`
regenerates it; `make check` fails when it is stale.

The compiler binary was reused, not rebuilt. It was verified to report the
pinned revision before use, and it was not modified. No concurrent build was
started.

## Competitor identities

Nothing from another project is vendored. Each competitor is either a system
shared object, a build under `.build/competitors` (gitignored), or a clone under
`/tmp`, and each is pinned by hash or by commit:

| Item | Identity |
| --- | --- |
| zlib 1.3.2 | `/usr/lib/libz.so.1.3.2`, hash recorded in each report's `provenance` |
| zlib 1.4 | tarball SHA-256 `02dd159783677a749c04adbdd02b55b8a2bcc7e054f51b76b549199a916ad584` |
| zlib-ng 2.3.3 | `/usr/lib/libz-ng.so.2.3.3`; source tarball SHA-256 `f9c65aa9c852eb8255b636fd9f07ce1c406f061ec19a2e7d508b318ca0c907d1` |
| zlib-ng `ZLIB_COMPAT` | `.build/competitors/zlib-ng-compat/lib/libz.so.1.3.1.zlib-ng`, SHA-256 `c4b50c20b2c6e1ec57ea57d7f0013612f339b5a4cd10aefa42b21fc67f0680a6` |
| libdeflate 1.26 | `/usr/lib/libdeflate.so.0` |
| `Macho0x/goose-deflate` | commit `3b1d0d38be908dbb9bc2e26411bfd7449c570147` |
| `kaitk/goose-compress` | commit `ebf372eac4638a692ee89eba3204c666c101d7d9` |

The `ZLIB_COMPAT` build is the one that matters for the backend question: it
installs a `libz.so.1` with zlib's ABI, so the unchanged Goose binary can be run
on zlib-ng through `LD_LIBRARY_PATH`. Nothing on the system is replaced, and the
build is reproducible with `make bench-zlib-ng-compat`, which verifies the
tarball hash before unpacking it.

## Measured identities

The published baseline is `c99b049`, archived at
`.build/baseline/archive/c99b049-v6` and verified against its 57-entry
`SHA256SUMS`. Its `variants/release/bench` binary is the OLD row in every
before/after comparison, and its `sources/` tree is what the concurrency
baseline binary is built from. Neither is rebuilt or overwritten: a measurement
that rebuilt the old code would be measuring a different thing.

The raw campaign data lives in `.build/benchmark-*.json` with a sibling
`.observations.jsonl` per run, and the confirmation campaign in
`.build/confirm-*.json`. Each report carries the provenance block, the
contention gate result before and after, and the per-round context-switch
counters of the timed children.

## Why this library exists

The Goose package catalog already lists two compression projects. Both were
read at their pinned commits before any code was written.

### `kaitk/goose-compress`

- Commit `ebf372eac4638a692ee89eba3204c666c101d7d9` (v0.1.4), Apache-2.0.
- README: `https://github.com/kaitk/goose-compress/blob/ebf372eac4638a692ee89eba3204c666c101d7d9/README.md`
- Catalog entry: `docs/sources.json` `repositories[31]`, with the recorded
  caution `{"terms": ["decoder", "decode", "decompression", "decompress"],
  "note": "Encoder only; no decoder is documented."}`
- Catalog prose: `docs/CHOICES.md`, "Compression and archives" — "Streaming
  encoding, including zstd … Native helpers; no decoder."

What it provides: gzip, zlib, raw DEFLATE and zstd **encoders**, with a
streaming `write`/`flush`/`finish` shape, appending output to a caller-supplied
builder.

What it does not provide: any decoder, any output bound, any input bound, any
trailing-byte or member policy, any dictionary handling, any checksum
reporting.

### `Macho0x/goose-deflate`

- Commit `3b1d0d38be908dbb9bc2e26411bfd7449c570147` (0.2.3), Apache-2.0.
- README: `https://github.com/Macho0x/goose-deflate/blob/3b1d0d38be908dbb9bc2e26411bfd7449c570147/README.md`
- Catalog entry: `docs/sources.json` `repositories[43]`, aliases
  `decompression`, `decompress`.
- Catalog prose: `docs/CHOICES.md` — "DEFLATE decompression and gzip/zlib
  wrappers … Includes a decoder and a deliberately simpler encoder."

What it provides: a pure-Goose `inflate::decode` over a whole buffer, plus a
simple encoder and gzip/zlib wrappers.

What it does not provide: incremental state across calls, a cumulative output
or input budget, a trailing-byte policy, a member policy, explicit dictionary
handling, or a distinction between a checksum failure and a malformed stream.
Its decoder is whole-buffer: the caller must already hold the entire compressed
input and the entire output.

### The gap this library fills

| Capability | kaitk/goose-compress | Macho0x/goose-deflate | goose-compress |
| --- | --- | --- | --- |
| Encode | yes | yes (simple) | no |
| Decode | no | yes, whole buffer | yes, incremental |
| Cumulative output budget | no | no | yes |
| Cumulative input budget | no | no | yes |
| Chunking-invariant result | n/a | n/a | yes, asserted |
| Trailing-byte policy | no | no | `Reject` / `Allow` |
| Concatenated members | no | no | `Trailing.Members` with a cap |
| Explicit dictionary | no | no | zlib DICTID + raw |
| Checksum vs malformed | no | no | separate errors |
| Partial output on failure | n/a | n/a | documented and kept |
| Observable cleanup | no | no | `abi().live` |

The useful difference is not "another decoder". It is a decoder whose answer is
a function of the bytes and the budgets alone — not of how the caller happened
to chunk its reads — with every stop named and every resource bounded.

## Consumer candidate

`examples/artifact_payload.goose` is a **candidate** consumer contract, shaped
like the step a downloader needs after fetching a `.gz` artifact. It is
executed by `scripts/example_check.py` and by `make test`.

It is **not** an adoption. Nothing in `goose-artifact` or `goose-archive` calls
this library, and neither project was modified. The example exists to make the
contract concrete and to prove it runs:

| Contract point | Value in the example |
| --- | --- |
| Compressed input cap | 8,388,608 bytes, checked before decoding |
| Decode step | 4,096 bytes |
| Output budget | caller-supplied, 0 .. 1,073,741,824 |
| Members | exactly 1 |
| Trailing bytes | rejected |
| Checksum | CRC-32 and ISIZE must match |
| Write policy | the output file is written only when the whole stream verified |

A real adoption would need its own review: `goose-artifact` currently bounds
downloads and verifies digests, and `goose-archive` bounds tar.gz inspection
through libarchive. Replacing either path with this library is a separate
decision with its own validation, and is not claimed here.

## What was not done

- No performance measurement was run. The harness is prepared and its
  correctness checks were exercised with one iteration each; no timing is
  recorded anywhere in this repository.
- No remote publication. No commit was pushed.
- No change to any other project, to the package catalog, or to any
  coordination metadata.
