# SPDX-License-Identifier: Apache-2.0
"""Builds the specification fixtures and their index.

Every fixture is a byte string with one documented expectation. The
expectation is computed by scripts/oracle.py, never by the library under
test, and the manifest records a SHA-256 for each generated file so that a
rerun on another machine can be compared exactly.

    python3 scripts/fixtures.py [--output .build/fixtures]
"""

import argparse
import hashlib
import json
import pathlib
import struct
import sys
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import oracle  # noqa: E402

MAGIC = b"GCFIX001"


class BitWriter:
    """DEFLATE's bit order: codes most significant bit first, bytes least
    significant bit first."""

    def __init__(self):
        self.bits = []

    def write(self, value, count):
        for shift in range(count - 1, -1, -1):
            self.bits.append((value >> shift) & 1)

    def finish(self):
        out = bytearray()
        for start in range(0, len(self.bits), 8):
            byte = 0
            for offset, bit in enumerate(self.bits[start:start + 8]):
                byte |= bit << offset
            out.append(byte)
        return bytes(out)


def gzip_stream(payload, level=6, extra=b"", name=b"", comment=b"", hcrc=False):
    """A gzip member with the header fields a fixture needs."""
    body = oracle.compress(payload, oracle.FORMAT_RAW, level)
    flags = 0
    if hcrc:
        flags |= 2
    if extra:
        flags |= 4
    if name:
        flags |= 8
    if comment:
        flags |= 16
    header = bytearray(b"\x1f\x8b\x08")
    header.append(flags)
    header += b"\x00\x00\x00\x00"  # MTIME 0 keeps the fixture reproducible
    header.append(0)  # XFL
    header.append(3)  # OS: Unix
    if extra:
        header += len(extra).to_bytes(2, "little") + extra
    if name:
        header += name + b"\x00"
    if comment:
        header += comment + b"\x00"
    if hcrc:
        header += (zlib.crc32(bytes(header)) & 0xFFFF).to_bytes(2, "little")
    trailer = (zlib.crc32(payload) & 0xFFFFFFFF).to_bytes(4, "little")
    trailer += (len(payload) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(header) + body + trailer


def distance_too_far():
    """A fixed-Huffman block whose first match reaches before the output."""
    writer = BitWriter()
    writer.write(1, 1)  # BFINAL
    writer.write(1, 2)  # BTYPE 01: fixed Huffman
    writer.write(0x30 + ord("a"), 8)  # literal 'a'
    writer.write(1, 7)  # length code 257: length 3
    writer.write(29, 5)  # distance code 29: 24577..32768, far past one byte
    writer.write(0, 7)  # end of block
    return writer.finish()


def stored_length_mismatch():
    """A stored block whose NLEN is not LEN's complement."""
    return bytes([0x01, 0x05, 0x00, 0x00, 0x00]) + b"hello"


def build():
    """Returns [(name, format, trailing, dictionary, limits, data)]."""
    text = b"hello goose compress\n" * 8
    large = bytes(range(256)) * 64
    zeros = bytes(1 << 20)
    dictionary = b"the quick brown fox jumps over the lazy dog"
    dict_payload = b"the quick brown fox " * 12

    cases = []

    def add(name, format, data, trailing=oracle.TRAILING_REJECT,
            dictionary=b"", output_bytes=1 << 24, input_bytes=1 << 24, members=1):
        cases.append((name, format, trailing, dictionary,
                      (output_bytes, input_bytes, members), data))

    # gzip: header variants, block types, and the trailer.
    add("gzip-empty", oracle.FORMAT_GZIP, gzip_stream(b""))
    add("gzip-text", oracle.FORMAT_GZIP, gzip_stream(text))
    add("gzip-stored", oracle.FORMAT_GZIP, gzip_stream(text, level=0))
    add("gzip-fixed", oracle.FORMAT_GZIP, gzip_stream(b"goose", level=1))
    add("gzip-dynamic", oracle.FORMAT_GZIP, gzip_stream(large, level=9))
    add("gzip-name", oracle.FORMAT_GZIP, gzip_stream(text, name=b"payload.txt"))
    add("gzip-comment", oracle.FORMAT_GZIP, gzip_stream(text, comment=b"a comment"))
    add("gzip-extra", oracle.FORMAT_GZIP, gzip_stream(text, extra=b"XY\x02\x00ab"))
    add("gzip-hcrc", oracle.FORMAT_GZIP, gzip_stream(text, hcrc=True))
    add("gzip-all-fields", oracle.FORMAT_GZIP,
        gzip_stream(text, extra=b"XY\x02\x00ab", name=b"n", comment=b"c", hcrc=True))

    member = gzip_stream(text)
    add("gzip-two-members", oracle.FORMAT_GZIP, member + member,
        trailing=oracle.TRAILING_MEMBERS, members=2)
    add("gzip-three-members", oracle.FORMAT_GZIP, member * 3,
        trailing=oracle.TRAILING_MEMBERS, members=3)
    add("gzip-members-over-limit", oracle.FORMAT_GZIP, member * 3,
        trailing=oracle.TRAILING_MEMBERS, members=2)
    add("gzip-trailing-reject", oracle.FORMAT_GZIP, member + b"junk!")
    add("gzip-trailing-allow", oracle.FORMAT_GZIP, member + b"junk!",
        trailing=oracle.TRAILING_ALLOW)

    add("gzip-truncated-header", oracle.FORMAT_GZIP, member[:5])
    add("gzip-truncated-body", oracle.FORMAT_GZIP, member[:len(member) // 2])
    add("gzip-truncated-trailer", oracle.FORMAT_GZIP, member[:-4])
    add("gzip-bad-crc", oracle.FORMAT_GZIP,
        member[:-8] + bytes([member[-8] ^ 0xFF]) + member[-7:])
    add("gzip-bad-isize", oracle.FORMAT_GZIP, member[:-1] + bytes([member[-1] ^ 0xFF]))
    add("gzip-bomb", oracle.FORMAT_GZIP, gzip_stream(zeros, level=9), output_bytes=4096)
    add("gzip-bomb-exact", oracle.FORMAT_GZIP, gzip_stream(zeros, level=9),
        output_bytes=len(zeros))

    # zlib: the Adler-32 wrapper and its dictionary identifier.
    add("zlib-empty", oracle.FORMAT_ZLIB, oracle.compress(b"", oracle.FORMAT_ZLIB))
    add("zlib-text", oracle.FORMAT_ZLIB, oracle.compress(text, oracle.FORMAT_ZLIB))
    add("zlib-dict", oracle.FORMAT_ZLIB,
        oracle.compress(dict_payload, oracle.FORMAT_ZLIB, dictionary=dictionary),
        dictionary=dictionary)
    add("zlib-dict-missing", oracle.FORMAT_ZLIB,
        oracle.compress(dict_payload, oracle.FORMAT_ZLIB, dictionary=dictionary))
    add("zlib-dict-wrong", oracle.FORMAT_ZLIB,
        oracle.compress(dict_payload, oracle.FORMAT_ZLIB, dictionary=dictionary),
        dictionary=b"a different dictionary of the same shape")
    zlib_text = oracle.compress(text, oracle.FORMAT_ZLIB)
    add("zlib-bad-adler", oracle.FORMAT_ZLIB, zlib_text[:-1] + bytes([zlib_text[-1] ^ 0xFF]))
    add("zlib-trailing-reject", oracle.FORMAT_ZLIB, zlib_text + b"tail")
    add("zlib-trailing-allow", oracle.FORMAT_ZLIB, zlib_text + b"tail",
        trailing=oracle.TRAILING_ALLOW)
    add("zlib-truncated", oracle.FORMAT_ZLIB, zlib_text[:-3])

    # raw DEFLATE: no wrapper, so no checksum and no dictionary identifier.
    add("raw-empty", oracle.FORMAT_RAW, bytes([0x03, 0x00]))
    add("raw-stored", oracle.FORMAT_RAW, oracle.compress(text, oracle.FORMAT_RAW, level=0))
    add("raw-fixed", oracle.FORMAT_RAW, oracle.compress(b"goose", oracle.FORMAT_RAW, level=1))
    add("raw-dynamic", oracle.FORMAT_RAW, oracle.compress(large, oracle.FORMAT_RAW, level=9))
    add("raw-dict", oracle.FORMAT_RAW,
        oracle.compress(dict_payload, oracle.FORMAT_RAW, dictionary=dictionary),
        dictionary=dictionary)
    add("raw-bad-block", oracle.FORMAT_RAW, bytes([0x07, 0x00]))
    add("raw-stored-mismatch", oracle.FORMAT_RAW, stored_length_mismatch())
    add("raw-distance-too-far", oracle.FORMAT_RAW, distance_too_far())
    add("raw-truncated", oracle.FORMAT_RAW,
        oracle.compress(text, oracle.FORMAT_RAW)[:8])

    # Limits that are exactly the stream's own size, and one byte short.
    add("gzip-input-exact", oracle.FORMAT_GZIP, member, input_bytes=len(member))
    add("gzip-input-short", oracle.FORMAT_GZIP, member, input_bytes=len(member) - 1)
    add("gzip-output-exact", oracle.FORMAT_GZIP, member, output_bytes=len(text))
    add("gzip-output-short", oracle.FORMAT_GZIP, member, output_bytes=len(text) - 1)
    add("gzip-output-zero", oracle.FORMAT_GZIP, member, output_bytes=0)
    add("gzip-empty-output-zero", oracle.FORMAT_GZIP, gzip_stream(b""), output_bytes=0)
    add("gzip-one-byte", oracle.FORMAT_GZIP, gzip_stream(b"a"))
    add("gzip-one-byte-output-one", oracle.FORMAT_GZIP, gzip_stream(b"a"), output_bytes=1)

    return cases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".build/fixtures")
    arguments = parser.parse_args()
    root = pathlib.Path(arguments.output)
    root.mkdir(parents=True, exist_ok=True)

    cases = build()
    index = bytearray(MAGIC)
    index += struct.pack("<I", len(cases))
    manifest = {"fixtures": []}
    for name, format, trailing, dictionary, limits, data in cases:
        output_bytes, input_bytes, members = limits
        error, expected = oracle.decode(format, trailing, dictionary,
                                        output_bytes, input_bytes, members, data)
        (root / f"{name}.in").write_bytes(data)
        (root / f"{name}.out").write_bytes(expected)
        if dictionary:
            (root / f"{name}.dict").write_bytes(dictionary)
        encoded = name.encode()
        index += struct.pack("<B", len(encoded)) + encoded
        index += struct.pack("<BBB", format, trailing, 1 if dictionary else 0)
        index += struct.pack("<qqq", output_bytes, input_bytes, members)
        index += struct.pack("<B", error)
        manifest["fixtures"].append({
            "name": name,
            "format": format,
            "trailing": trailing,
            "dictionary": bool(dictionary),
            "limits": {"output_bytes": output_bytes, "input_bytes": input_bytes,
                       "members": members},
            "expected_error": error,
            "expected_error_name": oracle.ERROR_NAMES[error],
            "input_bytes": len(data),
            "expected_bytes": len(expected),
            "input_sha256": hashlib.sha256(data).hexdigest(),
            "expected_sha256": hashlib.sha256(expected).hexdigest(),
        })
    (root / "index.bin").write_bytes(bytes(index))
    manifest["index_sha256"] = hashlib.sha256(bytes(index)).hexdigest()
    manifest["oracle"] = "CPython zlib " + zlib.ZLIB_RUNTIME_VERSION
    (root / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    print(f"wrote {len(cases)} fixtures to {root}")


if __name__ == "__main__":
    main()
