# Security

This experimental library has no supported stable releases. Use a maintained
system zlib build with security fixes and review the API restrictions before
use. Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/carabistouflette/goose-compress/security/advisories/new).
Private vulnerability reporting is enabled on this repository. Do not open
public issues containing exploit details before coordinated disclosure.

## Threat model

Compressed input is untrusted bytes. The library's job is to decode it without
letting it decide how much memory or time the process spends.

What the library bounds:

- **Output.** `limits.output_bytes` is a cumulative cap, enforced per step. A
  decompression bomb stops at the budget; the 1 MiB-of-zeros fixture with a
  4 KiB budget peaks at 2,520 KiB of resident memory because the 1 MiB is never
  allocated.
- **Input.** `limits.input_bytes` is a cumulative cap that also counts bytes
  after the last member.
- **Members.** `limits.members` caps concatenated members at 1024.
- **Dictionary.** At most 32,768 bytes.
- **Native state.** One `z_stream` per decoder, 32 decoders, then
  `Error.SystemError`.
- **Per-step growth.** The caller's builder grows by at most 65,536 bytes per
  internal step.

What the library does not do:

- **It does not authenticate.** A CRC-32 or Adler-32 match detects corruption;
  it does not prove the payload came from anyone in particular. An attacker who
  can rewrite the stream can rewrite the checksum. Verify a signature or a
  digest over the compressed bytes before trusting the content.
- **It does not bound time.** A stream can be crafted to decode slowly within
  the budgets. The caller owns wall-clock limits.
- **It does not bound the caller's own buffers.** `feed` appends to the
  caller's builder; the caller decides whether to keep the bytes, and the
  example shows the shape of a consumer that discards them on failure.
- **It does not detect a wrong raw dictionary.** Raw DEFLATE carries no
  dictionary identifier, so a wrong dictionary surfaces as
  `Error.Malformed`, not as a dictionary error. zlib streams do carry a DICTID
  and report `Error.DictionaryRequired` or `Error.DictionaryMismatch`.
- **It does not sandbox zlib.** A defect in the linked zlib is a defect in this
  library. Keep the system zlib patched.

## Partial output

Bytes decoded before a failure are appended to the caller's builder. A
`Checksum` failure therefore hands back the payload the library could not
vouch for. Do not act on those bytes without deciding, explicitly, that a
checksum failure is acceptable for your use case. The example refuses to write
its output file unless the whole stream verified.

## Known coupling

`native/compress.c` distinguishes a checksum failure from a malformed stream by
matching zlib's `strm->msg` text (`"incorrect data check"`,
`"incorrect length check"`). A change in zlib's wording would misclassify the
error. The differential corpus exercises both paths (4,427 checksum cases and
2,860 malformed cases), so such a change shows up as a corpus failure rather
than as a silent misclassification. It is still a coupling to a library's
message strings, and it is confined to one function.

## Reporting scope

In scope: memory safety in `native/compress.c`, budget bypasses, chunking-
dependent results, handle-table reuse, and any input that makes the library
exceed a documented bound.

Out of scope: defects in the linked zlib or libdeflate, the absence of
authentication, and resource exhaustion that stays within the caller's own
budgets.
