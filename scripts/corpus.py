# SPDX-License-Identifier: Apache-2.0
"""Builds the deterministic differential corpus.

Every case is a byte string, a format, a trailing policy, an optional
dictionary and three budgets. The expectation comes from scripts/oracle.py,
which decodes with CPython's zlib and applies docs/api.md itself.

    python3 scripts/corpus.py --cases 100000 --seed 20261107

The corpus is reproducible: the same seed and case count produce the same
bytes and the same SHA-256, on any machine with the same CPython zlib.
"""

import argparse
import hashlib
import json
import pathlib
import random
import struct
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import oracle  # noqa: E402

MAGIC = b"GCCORP01"
HUGE = 1 << 24

WORDS = [b"goose", b"compress", b"bounded", b"stream", b"deflate", b"zlib",
         b"gzip", b"member", b"trailer", b"checksum", b"dictionary", b"limit"]


def payload(rng, kind, size):
    if kind == "empty":
        return b""
    if kind == "zeros":
        return bytes(size)
    if kind == "random":
        return bytes(rng.getrandbits(8) for _ in range(size))
    if kind == "text":
        out = bytearray()
        while len(out) < size:
            out += rng.choice(WORDS) + b" "
        return bytes(out[:size])
    if kind == "runs":
        out = bytearray()
        while len(out) < size:
            out += bytes([rng.getrandbits(8)]) * rng.randint(1, 64)
        return bytes(out[:size])
    if kind == "mixed":
        out = bytearray()
        while len(out) < size:
            if rng.random() < 0.5:
                out += bytes(rng.randint(1, 32))
            else:
                out += bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 32)))
        return bytes(out[:size])
    raise AssertionError(kind)


def pick_size(rng):
    """Most cases are small; a few reach the 32 KiB window boundary.

    The distribution keeps the corpus near 40 MB while still covering the
    sizes where a DEFLATE window, a stored block and a 64 KiB output step
    change behaviour.
    """
    roll = rng.random()
    if roll < 0.40:
        return rng.choice([0, 1, 2, 3, 4, 5, 7, 8, 15, 16, 17, 31, 32, 33])
    if roll < 0.75:
        return rng.randint(0, 64)
    if roll < 0.95:
        return rng.randint(0, 256)
    if roll < 0.99:
        return rng.randint(0, 2048)
    return rng.choice([8191, 8192, 8193, 16384, 32768])


def make_case(rng):
    format = rng.choices(
        [oracle.FORMAT_GZIP, oracle.FORMAT_ZLIB, oracle.FORMAT_RAW],
        weights=[5, 3, 3])[0]
    kind = rng.choices(
        ["empty", "zeros", "random", "text", "runs", "mixed"],
        weights=[1, 2, 3, 4, 2, 2])[0]
    size = pick_size(rng)
    plain = payload(rng, kind, size)
    level = rng.choice([0, 1, 2, 6, 9])

    dictionary = b""
    if format != oracle.FORMAT_GZIP and rng.random() < 0.15:
        dictionary = payload(rng, rng.choice(["text", "random"]), rng.randint(1, 512))

    try:
        stream = oracle.compress(plain, format, level, dictionary)
    except Exception:
        stream = oracle.compress(plain, format, level)

    mutation = rng.choices(
        ["none", "truncate", "flip", "append", "concat"],
        weights=[6, 2, 2, 2, 2])[0]
    trailing = oracle.TRAILING_REJECT
    members = 1
    if mutation == "truncate" and len(stream) > 1:
        stream = stream[:rng.randint(0, len(stream) - 1)]
    elif mutation == "flip" and stream:
        position = rng.randrange(len(stream))
        stream = stream[:position] + bytes([stream[position] ^ (1 << rng.randrange(8))]) \
            + stream[position + 1:]
    elif mutation == "append":
        stream = stream + payload(rng, "random", rng.randint(1, 16))
        trailing = rng.choice([oracle.TRAILING_REJECT, oracle.TRAILING_ALLOW])
    elif mutation == "concat":
        copies = rng.randint(2, 4)
        stream = stream * copies
        if rng.random() < 0.7:
            trailing = oracle.TRAILING_MEMBERS
            members = rng.choice([copies, copies - 1, copies + 1, 1, 1024])
        else:
            trailing = rng.choice([oracle.TRAILING_REJECT, oracle.TRAILING_ALLOW])

    output_bytes = HUGE
    roll = rng.random()
    if roll < 0.10:
        output_bytes = 0
    elif roll < 0.20:
        output_bytes = max(0, len(plain) - 1)
    elif roll < 0.30:
        output_bytes = len(plain)
    elif roll < 0.35:
        output_bytes = len(plain) + 1
    elif roll < 0.40:
        output_bytes = rng.randint(0, max(0, len(plain)))

    input_bytes = HUGE
    roll = rng.random()
    if roll < 0.05:
        input_bytes = 0
    elif roll < 0.15:
        input_bytes = len(stream)
    elif roll < 0.20:
        input_bytes = max(0, len(stream) - 1)

    chunk = rng.choices([0, 7, 64, 1024, 65536], weights=[4, 1, 2, 2, 4])[0]
    if len(stream) <= 512 and rng.random() < 0.15:
        chunk = 1

    return {
        "format": format,
        "trailing": trailing,
        "dictionary": dictionary,
        "output_bytes": output_bytes,
        "input_bytes": input_bytes,
        "members": members,
        "chunk": chunk,
        "data": stream,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--output", default=".build/corpus")
    arguments = parser.parse_args()
    root = pathlib.Path(arguments.output)
    root.mkdir(parents=True, exist_ok=True)

    rng = random.Random(arguments.seed)
    body = bytearray()
    counts = {}
    total = 0
    for _ in range(arguments.cases):
        case = make_case(rng)
        error, expected = oracle.decode(
            case["format"], case["trailing"], case["dictionary"],
            case["output_bytes"], case["input_bytes"], case["members"], case["data"])
        counts[oracle.ERROR_NAMES[error]] = counts.get(oracle.ERROR_NAMES[error], 0) + 1
        body += struct.pack("<BBBB", case["format"], case["trailing"],
                            1 if case["dictionary"] else 0, error)
        body += struct.pack("<qqq", case["output_bytes"], case["input_bytes"],
                            case["members"])
        body += struct.pack("<qqq", case["chunk"], len(case["dictionary"]),
                            len(case["data"]))
        body += struct.pack("<q", len(expected))
        body += case["dictionary"] + case["data"] + expected
        total += 1

    blob = MAGIC + struct.pack("<I", total) + bytes(body)
    (root / "corpus.bin").write_bytes(blob)
    manifest = {
        "cases": total,
        "seed": arguments.seed,
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "oracle": "CPython zlib " + zlib.ZLIB_RUNTIME_VERSION,
        "expected_outcomes": dict(sorted(counts.items())),
    }
    (root / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    print(f"wrote {total} cases, {len(blob)} bytes, sha256 {manifest['sha256']}")
    for name, count in sorted(counts.items()):
        print(f"  {name:20s} {count}")


if __name__ == "__main__":
    main()
