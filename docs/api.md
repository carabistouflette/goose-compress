# API reference

Everything here is the contract. `tests/spec.goose` asserts it against 48
fixtures, `tests/boundaries.goose` asserts it under seven chunkings, and
`tests/differential.goose` asserts it against an independent oracle on 100,000
generated cases.

## Types

```goose
enum Format { Gzip, Zlib, Raw }
enum Trailing { Reject, Allow, Members }
enum State { Idle, Active, MemberEnd, Done, Failed }
enum Error { None, InvalidInput, LimitExceeded, Malformed, Truncated, Checksum,
             TrailingData, TooManyMembers, DictionaryRequired,
             DictionaryMismatch, SystemError }
```

`Format.Gzip` is RFC 1952, `Format.Zlib` is RFC 1950, `Format.Raw` is a bare
DEFLATE stream with no wrapper.

## Limits

```goose
struct Limits {
    output_bytes: i64 = 16777216,
    input_bytes:  i64 = 33554432,
    members:      i64 = 1,
    trailing:     Trailing = Trailing.Reject,
}
```

| Field | Range | Meaning |
| --- | --- | --- |
| `output_bytes` | 0 .. 1073741824 | Cumulative cap on decoded bytes across the whole stream. |
| `input_bytes` | 0 .. 1073741824 | Cumulative cap on accepted compressed bytes, including bytes after the last member. |
| `members` | 1 .. 1024 | Member cap. Only meaningful with `Trailing.Members`. |
| `trailing` | `Trailing` | What may follow the last member. |

`open` rejects an out-of-range value with `Error.InvalidInput`. It also rejects
`members != 1` unless `trailing == Trailing.Members`, because a member cap
without a member policy has no meaning.

### Trailing policies

| Policy | Accepts | On a byte after the last member |
| --- | --- | --- |
| `Reject` | exactly one member | `Error.TrailingData` |
| `Allow` | exactly one member | accepted, counted in `progress.trailing` |
| `Members` | up to `limits.members` members | `Error.TooManyMembers` |

`Allow` and `Members` are not interchangeable. `Allow` never decodes a second
member; it only tolerates bytes it does not read. `Members` decodes each member
and checks each trailer.

## Progress

```goose
struct Progress {
    state:    State = State.Idle,
    consumed: i64 = 0,
    produced: i64 = 0,
    members:  i64 = 0,
    trailing: i64 = 0,
}
```

`consumed` counts accepted compressed bytes, `produced` counts bytes appended
to the caller's builder, `members` counts completed members, and `trailing`
counts bytes accepted after the last member under `Trailing.Allow`.

## States

| State | Meaning | Allowed next calls |
| --- | --- | --- |
| `Idle` | not open, or closed | `open` |
| `Active` | inside a member | `feed`, `finish`, `close` |
| `MemberEnd` | a member ended; more input may still arrive | `feed`, `finish`, `close` |
| `Done` | `finish` succeeded | `close` |
| `Failed` | a terminal error was returned | `close` |

`MemberEnd` is not `Done`. A stream that ends inside a `feed` call stays in
`MemberEnd` so that a later chunk behaves exactly like the same bytes in the
same chunk. `Done` is reached only through `finish`.

## Functions

### `open(decoder, format, limits) -> Error`

Opens one decoder. `decoder` must be `Idle` or freshly zeroed. On failure the
decoder is left `Idle` and no native state is retained.

### `open_with_dictionary(decoder, format, limits, dictionary) -> Error`

As `open`, plus a preset dictionary of at most 32,768 bytes.

- `Format.Raw`: the dictionary is installed before the first byte. A raw stream
  carries no dictionary identifier, so a wrong dictionary is not detectable as
  such; it surfaces as `Error.Malformed`.
- `Format.Zlib`: the dictionary is installed and checked against the stream's
  DICTID. A stream that needs a dictionary and got none is
  `Error.DictionaryRequired`; a DICTID that does not match is
  `Error.DictionaryMismatch`.
- `Format.Gzip`: a non-empty dictionary is `Error.InvalidInput`. gzip has no
  dictionary.

### `feed(decoder, input, out) -> Error`

Consumes all of `input` and appends decoded bytes to `out`.

- `Error.None` means the input was consumed. It does not mean the stream ended.
- Any other return is terminal: the decoder is `Failed`, and later `feed` calls
  return the same error. `close` is still required.
- Bytes decoded before a failure are appended to `out`. A `Checksum` failure
  therefore reports the payload the library could not vouch for, and a
  `Malformed` failure reports the prefix it managed to decode. The caller
  decides whether to keep them.
- `out` grows by at most 65,536 bytes per internal step, and never beyond
  `limits.output_bytes` in total.

### `finish(decoder) -> Error`

Declares that no more input will arrive.

- `MemberEnd` becomes `Done` and returns `Error.None`.
- `Active` returns `Error.Truncated`: the stream ended mid-member.
- `Done` returns `Error.None` again.
- `Failed` returns the original error.

### `close(decoder) -> Error`

Frees the native state and returns the decoder to `Idle`. Closing an already
closed decoder returns `Error.None`. A decoder that is never closed leaks one
zlib state and one handle slot until the process exits.

### `decompress(input, format, limits, out) -> Error`

`open`, one `feed`, `finish`, `close`. `out` receives at most
`limits.output_bytes` bytes. Use it when the whole stream is already in memory.

### `abi() -> Abi`

```goose
struct Abi { zlib: i64 = 0, stream_bytes: i64 = 0, live: i64 = 0 }
```

`zlib` is zlib's `version_number()` (`10302` for 1.3.2), `stream_bytes` is
`sizeof(z_stream)` for the linked zlib, and `live` is the number of open
decoders. `live` is the observable cleanup check: after a balanced
open/close sequence it returns to its previous value.

### `zlib_version() -> u8[>..]`

The zlib runtime version string, for provenance records.

### `describe(error) -> const u8[:]` and `state_name(state) -> const u8[:]`

Stable lowercase names, used by the tests and the example.

## Errors

| Error | Cause |
| --- | --- |
| `None` | no error |
| `InvalidInput` | bad limits, bad dictionary, closed decoder, `feed` after `Done` |
| `LimitExceeded` | `output_bytes` or `input_bytes` reached before the stream ended |
| `Malformed` | zlib rejected the stream: bad block, bad distance, bad stored length |
| `Truncated` | `finish` on a stream that had not ended |
| `Checksum` | gzip CRC-32 or ISIZE mismatch, or zlib Adler-32 mismatch |
| `TrailingData` | bytes after the last member under `Trailing.Reject` |
| `TooManyMembers` | more members than `limits.members` under `Trailing.Members` |
| `DictionaryRequired` | a zlib stream needs a dictionary and none was supplied |
| `DictionaryMismatch` | the supplied dictionary does not match the DICTID |
| `SystemError` | allocation failure, or the 32-decoder handle table is full |

## The chunking guarantee

For a fixed `Limits`, the returned error, the produced bytes, `progress.members`
and `progress.trailing` do not depend on how the caller splits its input into
`feed` calls. `tests/boundaries.goose` asserts this for chunk sizes 1, 2, 3, 7,
64, 1024 and 65536 over all 48 fixtures, plus the exact-boundary and zero cases.

`progress.consumed` is invariant under the same split **except in one case**:
when the call stops with `Error.LimitExceeded` because `limits.output_bytes` was
reached. There `consumed` reports the input the failing step had available, so
it varies with the chunking. This is not a defect that can be fixed in the
runtime: `feed` cannot know how many compressed bytes produce exactly the
remaining output without decoding them, and decoding them would append bytes
past the budget. The exact rule, asserted by `tests/contract.goose`, is:

- `produced == limits.output_bytes` exactly, and
- `consumed <= limits.input_bytes` and `consumed <= input.len`.

Everywhere else — including the input-budget stop, a malformed stream, a
trailing-data refusal and a clean end — `consumed` is identical for every
chunking. Measured on the 44-byte `gzip-text` fixture with a 168-byte payload:
`consumed` is 12..13 at `output_bytes = 0`, 13..14 at 1, 16..17 at 4, 28..29 at
16, and a stable 35 at 64 and 100, while the error, the produced bytes, the
member count and the trailing count stay fixed throughout.

Two consequences are worth stating because they are easy to get wrong:

- A zero `output_bytes` budget still decodes a stream that produces nothing.
  The library reserves one byte of room internally so that "the stream ended"
  and "the stream produced a byte" are distinguishable; a byte that arrives is
  then reported as `LimitExceeded` and not appended.
- A chunk that crosses `input_bytes` is cut at the boundary, decoded, and then
  reported as `LimitExceeded`. The bytes decoded before the cap are the same
  ones a caller would get by feeding exactly `input_bytes` bytes.

## Resource bounds

| Resource | Bound |
| --- | --- |
| Native state per decoder | one `z_stream`, about 40 KiB, fixed |
| Open decoders | 32, then `Error.SystemError` |
| Output per `feed` step | 65,536 bytes |
| Output per decoder | `limits.output_bytes`, at most 1 GiB |
| Input per decoder | `limits.input_bytes`, at most 1 GiB |
| Dictionary | 32,768 bytes |
| Members | 1024 |

## Concurrency

One decoder is not safe to use from two threads at once. Separate decoders are
independent; the handle table is mutex-protected, so `open` and `close` from
different threads are safe.
