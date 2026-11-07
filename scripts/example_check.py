#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Runs the example consumer end to end and checks what it refuses.

The example is the only place where the library is used the way a real
consumer would use it, so it is executed rather than merely compiled:

    python3 scripts/example_check.py .toolchain/goose/build/goose

Every case below is a separate process. A case passes when the exit status,
the written file and the reported outcome all match the contract in
examples/artifact_payload.goose.
"""

import hashlib
import pathlib
import subprocess
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / ".build" / "example"
EXAMPLE = ROOT / "examples" / "artifact_payload.goose"


def payload():
    """A deterministic payload with enough repetition to compress."""
    words = [b"goose", b"artifact", b"payload", b"bounded", b"decode"]
    out = bytearray()
    while len(out) < 920:
        out += words[len(out) % len(words)] + b" "
    return bytes(out[:920])


def gzip_bytes(data):
    stream = zlib.compressobj(6, zlib.DEFLATED, 31)
    return stream.compress(data) + stream.flush()


def run(goose, arguments):
    result = subprocess.run([str(goose), "run", str(EXAMPLE), "--"] + arguments,
                            capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: example_check.py <goose-binary>")
    goose = pathlib.Path(sys.argv[1]).resolve()
    if not goose.is_file():
        raise SystemExit(f"no goose binary at {goose}")

    WORK.mkdir(parents=True, exist_ok=True)
    plain = payload()
    stream = gzip_bytes(plain)
    (WORK / "payload.gz").write_bytes(stream)
    (WORK / "plain.bin").write_bytes(plain)

    failures = []
    checks = 0

    def check(name, condition, detail=""):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(f"{name}: {detail}")

    def expect_ok(name, source, budget, expected):
        target = WORK / f"{name}.out"
        target.unlink(missing_ok=True)
        code, out, err = run(goose, [str(source), str(target), str(budget)])
        check(name, code == 0, f"exit {code}, stderr {err.strip()!r}")
        check(name, target.is_file(), "no output file")
        if target.is_file():
            written = target.read_bytes()
            check(name, written == expected,
                  f"{len(written)} bytes, sha256 {hashlib.sha256(written).hexdigest()}")
        check(name, f"decompressed: {len(expected)} bytes" in out, out.strip())
        return out

    def expect_refused(name, source, budget, needle):
        target = WORK / f"{name}.out"
        target.unlink(missing_ok=True)
        code, out, err = run(goose, [str(source), str(target), str(budget)])
        check(name, code == 1, f"exit {code}, stdout {out.strip()!r}")
        check(name, not target.exists(), "wrote a file it should have refused")
        check(name, needle in err, f"stderr {err.strip()!r} lacks {needle!r}")

    # 1. The happy path: the budget is exactly the payload size.
    out = expect_ok("exact", WORK / "payload.gz", len(plain), plain)
    check("exact", "outcome: none" in out, out.strip())
    check("exact", "members: 1" in out, out.strip())
    check("exact", "trailing: 0 bytes" in out, out.strip())

    # 2. One byte short: the budget must stop it, and nothing is written.
    expect_refused("short", WORK / "payload.gz", len(plain) - 1, "limit exceeded")

    # 3. A zero budget on a non-empty payload is a limit failure, not a crash.
    expect_refused("zero", WORK / "payload.gz", 0, "limit exceeded")

    # 4. A corrupted CRC is caught before anything is written.
    broken = bytearray(stream)
    broken[-8] ^= 0xFF
    (WORK / "bad-crc.gz").write_bytes(bytes(broken))
    expect_refused("bad-crc", WORK / "bad-crc.gz", len(plain), "checksum mismatch")

    # 5. A corrupted ISIZE is also a checksum failure.
    broken = bytearray(stream)
    broken[-1] ^= 0xFF
    (WORK / "bad-isize.gz").write_bytes(bytes(broken))
    expect_refused("bad-isize", WORK / "bad-isize.gz", len(plain), "checksum mismatch")

    # 6. Bytes after the single member are refused, not ignored.
    (WORK / "trailing.gz").write_bytes(stream + b"junk")
    expect_refused("trailing", WORK / "trailing.gz", len(plain), "trailing data")

    # 7. A second member is refused: the contract is exactly one member.
    (WORK / "two-members.gz").write_bytes(stream + stream)
    expect_refused("two-members", WORK / "two-members.gz", 2 * len(plain), "trailing data")

    # 8. A truncated stream is reported as truncated, not as a limit failure.
    (WORK / "truncated.gz").write_bytes(stream[: len(stream) // 2])
    expect_refused("truncated", WORK / "truncated.gz", len(plain), "truncated stream")

    # 9. The compressed-input cap is checked before decoding.
    (WORK / "oversized.gz").write_bytes(b"\x1f\x8b" + bytes(8388608))
    expect_refused("oversized", WORK / "oversized.gz", len(plain), "over the 8388608 byte cap")

    # 10. A budget outside the documented range is a usage error, not a decode.
    code, _, err = run(goose, [str(WORK / "payload.gz"), str(WORK / "bad.out"), "1073741825"])
    check("bad-budget", code == 2, f"exit {code}, stderr {err.strip()!r}")

    # 11. A missing input file is a usage error too.
    code, _, err = run(goose, [str(WORK / "absent.gz"), str(WORK / "absent.out"), "1024"])
    check("missing-input", code == 1, f"exit {code}, stderr {err.strip()!r}")

    # 12. The example is deterministic: the same input gives the same bytes.
    first = (WORK / "exact.out").read_bytes()
    expect_ok("repeat", WORK / "payload.gz", len(plain), plain)
    check("repeat", (WORK / "repeat.out").read_bytes() == first, "output differs between runs")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        raise SystemExit(f"example check failed: {len(failures)} of {checks} assertions")

    print(f"example ok: {checks} assertions, payload {len(plain)} bytes, "
          f"stream {len(stream)} bytes, sha256 {hashlib.sha256(plain).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
