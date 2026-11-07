# goose-compress

Bounded incremental gzip, zlib and raw DEFLATE decoding from
[Goose](https://github.com/aardappel/goose), over maintained system zlib.
Linux only, Apache-2.0, experimental API. There are no tagged releases yet;
these docs describe main. Use an exact commit for dependencies.

The library decodes a stream in caller-sized steps and stops at a cumulative
output budget, a cumulative input budget, a member count, or the first defect.
Every stop is a named error, and the bytes decoded before it stay in the
caller's builder. It does not compress.

## Try it

Install Linux build tools, Python 3.11+, CMake 3.27+, a C11 compiler and the
zlib development package. On Debian/Ubuntu the packages are
`build-essential cmake git python3 zlib1g-dev`. Use a maintained zlib package
with security fixes; the minimum version only states a build requirement.

```sh
git clone https://github.com/carabistouflette/goose-compress.git
cd goose-compress
make setup
make fixtures
mkdir -p .build/example
.toolchain/goose/build/goose run examples/artifact_payload.goose -- \
    .build/fixtures/gzip-text.in .build/example/out.bin 1048576
cmp .build/example/out.bin .build/fixtures/gzip-text.out
```

That decodes a generated 44-byte gzip stream into 168 bytes and prints:

```
outcome: none
compressed: 44 bytes
decompressed: 168 bytes
members: 1
trailing: 0 bytes
wrote: .build/example/out.bin
```

`cmp` then succeeds silently, which is the point: the example's output is
byte-identical to the fixture's recorded expectation.

To see the whole contract in one go, including every refusal, run the checked
example instead:

```sh
python3 scripts/example_check.py .toolchain/goose/build/goose
```

It writes a 920-byte payload, gzips it, and runs the example twelve times:
once within budget, and eleven times against a budget one byte short, a zero
budget, a corrupted CRC, a corrupted ISIZE, trailing bytes, a second member, a
truncated stream, an oversized compressed input, an out-of-range budget and a
missing file. It prints `example ok: 38 assertions` when every refusal is the
one the contract promises.

`make setup` builds the exact compiler in `GOOSE_REVISION`. To reuse that
compiler, supply `GOOSE=/absolute/path/to/goose` to Make and use that path for
`goose run`.

## API

Declare the dependency in the application's root source:

```goose
import compress from "carabistouflette/goose-compress@<commit>";
```

Or use `import compress from "../goose-compress";` for a sibling checkout.

- `open(decoder, format, limits)` starts one stream. `open_with_dictionary`
  adds an explicit preset dictionary for zlib and raw streams.
- `feed(decoder, input, out)` consumes all of `input` and appends what it
  decodes to `out`. A returned error is terminal.
- `finish(decoder)` declares that no more input will arrive.
- `close(decoder)` frees the native state. Closing twice is not an error.
- `decompress(input, format, limits, out)` is the one-call form of the above.
- `abi()` and `zlib_version()` report the linked zlib for provenance records.

`Limits { .. }` defaults to 16 MiB output, 32 MiB input, one member and
`Trailing.Reject`. `Progress` reports the state, the bytes consumed and
produced, the members completed and the trailing bytes accepted. Exact
semantics, the error table and the chunking guarantee are in the
[API reference](docs/api.md).

The output budget is cumulative and independent of how the caller chunks its
input. A stream that needs more than `output_bytes` fails with
`Error.LimitExceeded` whether it arrives in one call or a thousand, and the
bytes up to the budget are kept. The same holds for `input_bytes`, which also
counts bytes after the last member.

## Limits and non-goals

There is no compression, no ZIP, no zstd, no brotli, no file or network I/O,
no thread safety within one decoder, and no Windows support. The caller owns
bounded reads, trust decisions, cleanup and publication. A CRC match detects
corruption; it does not establish authenticity.

Raw DEFLATE carries no dictionary identifier, so a raw stream that needs a
dictionary cannot be detected as such: it fails as `Error.Malformed` unless the
caller supplied the right dictionary. zlib streams do carry a DICTID and report
`Error.DictionaryRequired` or `Error.DictionaryMismatch`.

Each open decoder holds one zlib state, about 40 KiB, regardless of the
budgets. The handle table holds 32 decoders; a 33rd `open` returns
`Error.SystemError`. A stale handle is rejected rather than dereferenced.

## Development and measurements

Run `make check test`. Native behavior changes also need
`make sanitize SANITIZER_CC=clang` and `make sanitize SANITIZER_CC=gcc`.
That target emits standalone C and compiles it through
`scripts/sanitizer-cc.sh` so that both the Goose code and the native adapter are
really instrumented; `goose build --cc clang --debug` alone does not do that.
It ends with a negative control that injects a heap overflow and requires the
process to abort, so a green run is evidence rather than a debug build.
Run Make serially per checkout because native cache paths are shared.
`make format` refreshes the native source fingerprint.

`make bench` builds the benchmark without measuring anything. `make bench-run`
measures, and must run alone on an idle machine; see
[bench/README.md](bench/README.md) and [docs/benchmark.md](docs/benchmark.md).
No timing result is recorded in this repository yet.

Validation evidence, including the differential corpus and the sanitizer runs,
is in [docs/validation.md](docs/validation.md). Source identity and the
overlap analysis against the existing Goose compression libraries are in
[docs/provenance.md](docs/provenance.md).
