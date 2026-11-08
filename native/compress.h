/* SPDX-License-Identifier: Apache-2.0 */
#ifndef GOOSE_COMPRESS_H
#define GOOSE_COMPRESS_H
#include <stdint.h>

/* The layouts Goose passes for `const u8[:]`, `u8[:]` and `i64[:]`
 * (spec §7.10, C.2). No pointer is retained past the call. */
typedef struct { const uint8_t *data; int64_t len; } gc_input;
typedef struct { uint8_t *data; int64_t len; } gc_output;
typedef struct { int64_t *data; int64_t len; } gc_meta;

/* Status codes shared with compress.goose. GC_OK is the only success. */
enum {
    GC_OK = 0,
    GC_INVALID = 1,
    GC_LIMIT = 2,
    GC_MALFORMED = 3,
    GC_CHECKSUM = 4,
    GC_TRAILING = 5,
    GC_MEMBERS = 6,
    GC_DICT_REQUIRED = 7,
    GC_DICT_MISMATCH = 8,
    GC_SYSTEM = 9,
    GC_TRUNCATED = 10
};

/* Formats, matching compress::Format. */
enum { GC_GZIP = 0, GC_ZLIB = 1, GC_RAW = 2 };

/* gc_open: meta[0] = status, meta[1] = zlib's inflate window bits used.
 * Returns a nonzero handle on success, 0 on failure. */
int64_t gc_open(int64_t format, gc_meta meta);

/* gc_dictionary: registers the preset dictionary for this decoder. Raw
 * streams take it immediately; zlib streams take it when the stream asks.
 * Dictionary representation/length is checked before looking up the handle.
 * meta[0] = status. Returns GC_OK or an error. */
int64_t gc_dictionary(int64_t handle, gc_input dictionary, gc_meta meta);

/* gc_inflate: one zlib inflate step.
 * meta[0] = input bytes consumed, meta[1] = output bytes produced,
 * meta[2] = zlib's return code, meta[3] = flags (1 stream end, 2 needs
 * dictionary), meta[4] = zlib's adler32 field (the DICTID when it needs a
 * dictionary), meta[5] = status. Returns the status. */
int64_t gc_inflate(int64_t handle, gc_input input, gc_output output, gc_meta meta);

/* gc_reset: starts a new member on the same decoder. meta[0] = status. */
int64_t gc_reset(int64_t handle, gc_meta meta);

/* gc_close: frees the decoder. Idempotent for a stale handle, which reports
 * GC_INVALID rather than dereferencing freed memory. meta[0] = status. */
int64_t gc_close(int64_t handle, gc_meta meta);

/* gc_abi: meta[0] = zlib runtime version as a number, meta[1] = sizeof
 * (z_stream), meta[2] = live decoder slots. Provenance only. */
int64_t gc_abi(gc_meta meta);

/* gc_version: copies zlib's runtime version string without a NUL terminator.
 * Returns status and writes the same status to meta[0] when meta is valid.
 * A short output buffer returns GC_LIMIT without changing that buffer. */
int64_t gc_version(gc_output out, gc_meta meta);

#endif
