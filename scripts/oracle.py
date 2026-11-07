# SPDX-License-Identifier: Apache-2.0
"""An independent oracle for goose-compress.

DEFLATE decoding is CPython's zlib module. The gzip and zlib wrappers are
parsed here, from RFC 1950 and RFC 1952, so that the oracle can report the
bytes a decoder produced before a trailer check failed: CPython's
`decompressobj` raises and discards them, while the documented contract
keeps them. The policy below is a second implementation of docs/api.md,
written from that document rather than from compress.goose.

`tests/test_compress.py` cross-checks this oracle against CPython's
`gzip.decompress` and `zlib.decompress` on every valid fixture, so a mistake
in the wrapper parsing shows up as a disagreement rather than as a silently
wrong expectation.
"""

import zlib

FORMAT_GZIP, FORMAT_ZLIB, FORMAT_RAW = 0, 1, 2
TRAILING_REJECT, TRAILING_ALLOW, TRAILING_MEMBERS = 0, 1, 2

(
    ERR_NONE,
    ERR_INVALID,
    ERR_LIMIT,
    ERR_MALFORMED,
    ERR_TRUNCATED,
    ERR_CHECKSUM,
    ERR_TRAILING,
    ERR_MEMBERS,
    ERR_DICT_REQUIRED,
    ERR_DICT_MISMATCH,
    ERR_SYSTEM,
) = range(11)

ERROR_NAMES = {
    ERR_NONE: "none",
    ERR_INVALID: "invalid input",
    ERR_LIMIT: "limit exceeded",
    ERR_MALFORMED: "malformed stream",
    ERR_TRUNCATED: "truncated stream",
    ERR_CHECKSUM: "checksum mismatch",
    ERR_TRAILING: "trailing data",
    ERR_MEMBERS: "too many members",
    ERR_DICT_REQUIRED: "dictionary required",
    ERR_DICT_MISMATCH: "dictionary mismatch",
    ERR_SYSTEM: "system error",
}

MAX_OUTPUT = 1 << 30
MAX_INPUT = 1 << 30
MAX_MEMBERS = 1024
MAX_DICTIONARY = 32768

WBITS = {FORMAT_GZIP: 31, FORMAT_ZLIB: 15, FORMAT_RAW: -15}

# Returned by the header parsers when the bytes end inside the header.
_INCOMPLETE = object()


def classify(message):
    """Maps a zlib error message to the library's error code."""
    text = str(message)
    if "incorrect data check" in text or "incorrect length check" in text:
        return ERR_CHECKSUM
    if "while setting zdict" in text:
        return ERR_DICT_MISMATCH
    if "Error 2 " in text or "need dictionary" in text:
        return ERR_DICT_REQUIRED
    return ERR_MALFORMED


def validate(format, trailing, dictionary, output_bytes, input_bytes, members):
    """The open-time checks, in the order docs/api.md states them."""
    if format not in (FORMAT_GZIP, FORMAT_ZLIB, FORMAT_RAW):
        return ERR_INVALID
    if trailing not in (TRAILING_REJECT, TRAILING_ALLOW, TRAILING_MEMBERS):
        return ERR_INVALID
    if output_bytes < 0 or output_bytes > MAX_OUTPUT:
        return ERR_INVALID
    if input_bytes < 0 or input_bytes > MAX_INPUT:
        return ERR_INVALID
    if members < 1 or members > MAX_MEMBERS:
        return ERR_INVALID
    if trailing != TRAILING_MEMBERS and members != 1:
        return ERR_INVALID
    if len(dictionary) > MAX_DICTIONARY:
        return ERR_INVALID
    if format == FORMAT_GZIP and dictionary:
        return ERR_INVALID
    return ERR_NONE


def gzip_header_end(data, start):
    """RFC 1952 header length, _INCOMPLETE, or ERR_MALFORMED."""
    if len(data) - start < 10:
        return _INCOMPLETE
    if data[start] != 0x1F or data[start + 1] != 0x8B:
        return ERR_MALFORMED
    if data[start + 2] != 8:
        return ERR_MALFORMED
    flags = data[start + 3]
    if flags & 0xE0:
        return ERR_MALFORMED
    position = start + 10
    if flags & 4:
        if len(data) - position < 2:
            return _INCOMPLETE
        length = int.from_bytes(data[position:position + 2], "little")
        position += 2 + length
        if position > len(data):
            return _INCOMPLETE
    for bit in (8, 16):
        if flags & bit:
            end = data.find(b"\x00", position)
            if end < 0:
                return _INCOMPLETE
            position = end + 1
    if flags & 2:
        if len(data) - position < 2:
            return _INCOMPLETE
        expected = int.from_bytes(data[position:position + 2], "little")
        if expected != (zlib.crc32(data[start:position]) & 0xFFFF):
            return ERR_MALFORMED
        position += 2
    return position


def zlib_header_end(data, start, dictionary):
    """RFC 1950 header length, _INCOMPLETE, or an error code."""
    if len(data) - start < 2:
        return _INCOMPLETE
    cmf, flg = data[start], data[start + 1]
    if cmf & 0x0F != 8:
        return ERR_MALFORMED
    if (cmf << 8 | flg) % 31 != 0:
        return ERR_MALFORMED
    if not flg & 0x20:
        return start + 2
    if len(data) - start < 6:
        return _INCOMPLETE
    identifier = int.from_bytes(data[start + 2:start + 6], "big")
    if not dictionary:
        return ERR_DICT_REQUIRED
    if (zlib.adler32(dictionary) & 0xFFFFFFFF) != identifier:
        return ERR_DICT_MISMATCH
    return start + 6


def _raw_stream(dictionary):
    return (
        zlib.decompressobj(-15, zdict=dictionary)
        if dictionary
        else zlib.decompressobj(-15)
    )


def _decodes(data, start, end, dictionary):
    """True when data[start:end] inflates without a zlib error."""
    try:
        _raw_stream(dictionary).decompress(data[start:end])
        return True
    except zlib.error:
        return False


def inflate_body(data, start, dictionary, output_bytes, out):
    """Raw-inflates data[start:], appending to out.

    Returns (error, end) where end is the offset just past the DEFLATE
    stream when error is ERR_NONE. CPython's `decompress` discards the bytes
    it produced before raising, so a failing stream is re-decoded up to the
    last prefix that does not raise; that prefix is exactly what a streaming
    decoder had already handed back.
    """
    try:
        stream = _raw_stream(dictionary)
        out += stream.decompress(data[start:])
    except zlib.error as failure:
        low, high = start, len(data)
        while low < high:
            middle = (low + high + 1) // 2
            if _decodes(data, start, middle, dictionary):
                low = middle
            else:
                high = middle - 1
        if low > start:
            out += _raw_stream(dictionary).decompress(data[start:low])
        if len(out) > output_bytes:
            return ERR_LIMIT, None
        return classify(failure), None
    if len(out) > output_bytes:
        return ERR_LIMIT, None
    if not stream.eof:
        return ERR_TRUNCATED, None
    return ERR_NONE, len(data) - len(stream.unused_data)


def decode(format, trailing, dictionary, output_bytes, input_bytes, members, data):
    """Returns (error_code, output_bytes) for one whole stream.

    The input budget is a running cap: the decoder accepts `input_bytes`
    bytes, decodes them, and only then reports that the stream was longer.
    That is what makes the answer independent of the caller's chunking.
    """
    error = validate(format, trailing, dictionary, output_bytes, input_bytes, members)
    if error != ERR_NONE:
        return error, b""
    over = len(data) > input_bytes
    error, out = decode_stream(format, trailing, dictionary, output_bytes, members,
                               data[:input_bytes])
    # A stream that only looks truncated because the budget cut it is a
    # budget failure; a real defect inside the budget keeps its own code.
    if error == ERR_TRUNCATED and over:
        return ERR_LIMIT, out
    if error != ERR_NONE:
        return error, out
    if over:
        return ERR_LIMIT, out
    return ERR_NONE, out


def decode_stream(format, trailing, dictionary, output_bytes, members, data):
    """Decodes one stream whose bytes are all within the input budget."""
    out = bytearray()
    position = 0
    count = 0
    while True:
        member_start = len(out)
        if format == FORMAT_GZIP:
            header = gzip_header_end(data, position)
            if header is _INCOMPLETE:
                return ERR_TRUNCATED, bytes(out)
            if header == ERR_MALFORMED:
                return ERR_MALFORMED, bytes(out)
            position = header
        elif format == FORMAT_ZLIB:
            header = zlib_header_end(data, position, dictionary)
            if header is _INCOMPLETE:
                return ERR_TRUNCATED, bytes(out)
            if header in (ERR_MALFORMED, ERR_DICT_REQUIRED, ERR_DICT_MISMATCH):
                return header, bytes(out)
            position = header
        error, end = inflate_body(data, position, dictionary, output_bytes, out)
        if error != ERR_NONE:
            return error, bytes(out[:output_bytes])
        position = end
        if format == FORMAT_GZIP:
            if len(data) - position < 4:
                return ERR_TRUNCATED, bytes(out)
            crc = int.from_bytes(data[position:position + 4], "little")
            if crc != (zlib.crc32(bytes(out[member_start:])) & 0xFFFFFFFF):
                return ERR_CHECKSUM, bytes(out)
            if len(data) - position < 8:
                return ERR_TRUNCATED, bytes(out)
            isize = int.from_bytes(data[position + 4:position + 8], "little")
            if isize != (len(out) - member_start) & 0xFFFFFFFF:
                return ERR_CHECKSUM, bytes(out)
            position += 8
        elif format == FORMAT_ZLIB:
            if len(data) - position < 4:
                return ERR_TRUNCATED, bytes(out)
            adler = int.from_bytes(data[position:position + 4], "big")
            if adler != (zlib.adler32(bytes(out[member_start:])) & 0xFFFFFFFF):
                return ERR_CHECKSUM, bytes(out)
            position += 4
        count += 1
        if position >= len(data):
            return ERR_NONE, bytes(out)
        if trailing == TRAILING_ALLOW:
            return ERR_NONE, bytes(out)
        if trailing == TRAILING_MEMBERS and count < members:
            continue
        if trailing == TRAILING_MEMBERS:
            return ERR_MEMBERS, bytes(out)
        return ERR_TRAILING, bytes(out)


def compress(data, format, level=6, dictionary=b""):
    """The reference encoder, used only to build fixtures and corpora."""
    wbits = WBITS[format]
    if dictionary:
        stream = zlib.compressobj(level, zlib.DEFLATED, wbits, 9,
                                  zlib.Z_DEFAULT_STRATEGY, dictionary)
    else:
        stream = zlib.compressobj(level, zlib.DEFLATED, wbits)
    return stream.compress(data) + stream.flush()
