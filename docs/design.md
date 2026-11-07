# Design

## Why a C adapter exists

zlib's `z_stream` holds function pointers (`zalloc`, `zfree`, `opaque`) and its
layout depends on the zlib build. Goose cannot declare that struct, and it
cannot call `inflate` without one. The adapter therefore owns exactly one
`z_stream` per decoder and exposes seven functions that take and return plain
integers and byte slices.

The adapter holds no policy. It does not know about budgets, members, trailing
bytes, chunking or the state machine. It reports what zlib did — bytes
consumed, bytes produced, the zlib return code, and whether a member ended —
and the Goose code decides what that means. `native/compress.c` is 333 lines,
most of it argument validation and the handle table.

The alternative, a pure-Goose inflater, was rejected: it would be a second
DEFLATE implementation to audit, and the project's stated goal is bounded
decoding over a maintained zlib, not a new decompressor.

## Handle table

`native/compress.c` keeps 32 slots, each holding a `z_stream` pointer and a
generation counter, behind one `pthread_mutex_t`.

A handle is `(index << 47) | generation`. `find_decoder` rejects a zero index,
an index past the table, a zero generation, a slot whose generation does not
match, and a slot with no decoder. A stale handle from a closed decoder
therefore fails as `GC_INVALID` instead of dereferencing freed memory. The
generation counter is capped at `0x7FFFFFFFFFFF`; a slot that reaches the cap
is retired rather than wrapped, so a handle can never be reused.

The table is a fixed array, so `open` has no allocation of its own beyond the
`z_stream` and its window. A 33rd concurrent decoder returns `Error.SystemError`.

**Known defect, blocking.** The adapter's table-full branch currently sets
`meta.data[0] = GC_LIMIT`, and `to_error(2)` is `Error.LimitExceeded`, so the
33rd `open` returns `LimitExceeded` instead of the promised `SystemError`.
Reproduced in `.build/probe/limit.goose` and asserted by `tests/contract.goose`,
which fails until `gc_open` sets `GC_SYSTEM` there. The fix is one line in
`native/compress.c` and is held back until the frozen baseline reference window
closes.

## Output budget and the one-byte sentinel

`feed` cannot know how many bytes a step will produce, so it must offer zlib
some room. It reserves

```
reserve = min(max(output_bytes - produced, 1), 65536)
```

and passes `out[base .. base + reserve]` to `gc_inflate`. Three cases follow.

- **Room left.** `reserve` is the remaining budget, capped at 64 KiB. zlib
  fills at most that much, so the budget cannot be overrun.
- **Budget exhausted, stream still producing.** `reserve` is 1. zlib writes one
  byte, the adapter reports `produced == 1`, and `feed` sees
  `produced > remaining == 0`, truncates the byte away and returns
  `LimitExceeded`. The byte is never appended.
- **Budget exhausted, stream ending.** `reserve` is 1, zlib consumes the
  trailer and produces nothing, `produced == 0`, and the call succeeds. This is
  what makes an empty gzip stream with `output_bytes = 0` a success rather than
  a limit failure.

Without the sentinel, a zero budget would have to either refuse every stream
(including empty ones) or trust zlib to stop by itself. The sentinel costs one
byte of transient buffer and makes the distinction observable.

## Input budget as a running cap

The naive reading of `input_bytes` is "reject a chunk that would cross it".
That makes the answer depend on chunking: feeding 100 bytes with a 50-byte
budget would reject all 100, while feeding 50 then 50 would decode the first
50 and then fail. The library instead cuts the chunk at the boundary, decodes
the prefix, and then fails:

```
allowed = input_bytes - consumed
usable  = min(input.len, allowed)
over    = usable < input.len
```

`over` is remembered and turned into `LimitExceeded` after the prefix has been
decoded. The result is that `consumed`, `produced` and the error are the same
for every chunking of the same bytes.

That invariance is specific to the input-budget path. When the *output* budget
is what stops the call, `consumed` reports the input the failing step had
available and therefore varies with the chunking; `docs/api.md` states the
exact rule and `tests/contract.goose` asserts it. The runtime cannot do better
without decoding bytes it is not allowed to append.

Bytes accepted after the last member under `Trailing.Allow` also count against
`input_bytes` and are added to `consumed`. They are input the caller supplied,
so leaving them out would let a caller bypass the cap by appending padding.

## MemberEnd versus Done

A stream that ends inside a `feed` call moves to `MemberEnd`, not `Done`. The
reason is the chunking guarantee: if the same bytes arrived in two chunks, the
first `feed` would end the member and the second would see `MemberEnd`. Making
the single-chunk case reach `Done` would give the two chunkings different
states and different behaviour on the next call.

`Done` is therefore reachable only through `finish`, which is the caller's
statement that no more input exists. `feed` after `Done` is `InvalidInput`;
`feed` after `MemberEnd` is legal and is interpreted by the trailing policy.

## Partial output on failure

When zlib reports a defect, the bytes it decoded before the defect are already
in the caller's builder. The library keeps them. Two reasons:

- A `Checksum` failure is the interesting case for a consumer: the payload is
  probably intact and the trailer is not. Discarding it would force the caller
  to decode twice to find out.
- A `Malformed` failure reports the prefix, which is what a streaming consumer
  has already written to its own output.

The contract is explicit about this in `docs/api.md`, and the oracle reproduces
it, so it is a documented behaviour rather than an accident.

## Checksum versus malformed

zlib returns `Z_DATA_ERROR` for both a bad gzip CRC and a corrupt Huffman
block. The adapter distinguishes them by inspecting `strm->msg`: zlib sets it
to `"incorrect data check"` or `"incorrect length check"` for a trailer
mismatch, and to a block-level message otherwise. `is_checksum_message` maps
the first two to `GC_CHECKSUM` and everything else to `GC_MALFORMED`.

This is a string match on a maintained library's message, which is a real
coupling. It is confined to one function, and the differential corpus exercises
both paths (4,427 checksum cases and 2,860 malformed cases), so a change in
zlib's wording would show up as a corpus failure rather than as a silent
misclassification.

## Dictionaries

- **zlib.** The stream carries a DICTID. `inflate` returns `Z_NEED_DICT` with
  the expected Adler-32, and the adapter compares it with the supplied
  dictionary's Adler-32. A mismatch is `GC_DICT_MISMATCH`; no dictionary at all
  is `GC_DICT_REQUIRED`.
- **raw.** There is no DICTID. The dictionary is installed with
  `inflateSetDictionary` before the first byte. A wrong dictionary produces
  back-references into garbage, which surfaces as `GC_MALFORMED` — not as a
  dictionary error. This is a documented limitation, not a bug: the format
  carries no way to tell.
- **gzip.** No dictionary. `open_with_dictionary` rejects one.

`inflateReset` clears the window, so a multi-member raw stream would lose its
dictionary between members. `gc_reset` re-installs it after the reset.

## What is deliberately absent

- No compression. `kaitk/goose-compress` already provides encoders.
- No whole-buffer convenience that ignores limits. `decompress` takes `Limits`
  like every other entry point.
- No file or network I/O. The caller bounds its own reads; the example shows
  the shape of that.
- No thread pool, no async, no callbacks. `feed` is a synchronous step.
- No ZIP, zstd, brotli or lzma. Each would be a separate format with its own
  trailer and dictionary rules.
