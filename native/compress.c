/* SPDX-License-Identifier: Apache-2.0 */
/* A minimal zlib adapter for goose-compress. It owns one z_stream per
 * decoder and reports what zlib did; every policy decision (cumulative
 * budgets, member counting, trailing bytes, state) lives in compress.goose
 * so that it is observable and testable without C. */
#define _POSIX_C_SOURCE 200809L
#include "compress.h"

#include <limits.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

#if !defined(__linux__)
#error "goose-compress currently supports Linux only"
#endif

#ifndef GC_MAX_DECODERS
#define GC_MAX_DECODERS 32
#endif
#ifndef GC_MAX_GENERATION
#define GC_MAX_GENERATION 0x7FFFFFFFFFFFLL
#endif
#if GC_MAX_DECODERS < 1 || GC_MAX_DECODERS > 32767
#error "GC_MAX_DECODERS must fit the handle index"
#endif

struct decoder {
    z_stream stream;
    int format;
    int has_dictionary;
    uint8_t *dictionary;
    size_t dictionary_len;
};

typedef struct {
    uint64_t generation;
    struct decoder *decoder;
} decoder_slot;

/* The lock protects the registry only. A live decoder belongs to the caller
 * that opened it; concurrent use of one handle is the caller's error. */
static pthread_mutex_t decoders_lock = PTHREAD_MUTEX_INITIALIZER;
static decoder_slot decoders[GC_MAX_DECODERS];

static int64_t encode_handle(size_t index, uint64_t generation) {
    return (int64_t)((generation << 16) | UINT64_C(0x8000) | (uint64_t)(index + 1));
}

/* Returns the decoder for a live handle, or NULL. Caller holds the lock. */
static struct decoder *find_decoder(int64_t handle) {
    if (handle <= 0) return NULL;
    uint64_t token = (uint64_t)handle;
    uint64_t low = token & UINT64_C(0xffff);
    if (!(low & UINT64_C(0x8000))) return NULL;
    uint64_t index = low & UINT64_C(0x7fff);
    uint64_t generation = token >> 16;
    if (!index || index > GC_MAX_DECODERS || !generation) return NULL;
    decoder_slot *slot = &decoders[index - 1];
    if (!slot->decoder || slot->generation != generation) return NULL;
    return slot->decoder;
}

static int window_bits(int64_t format) {
    if (format == GC_GZIP) return 15 + 16;
    if (format == GC_ZLIB) return 15;
    if (format == GC_RAW) return -15;
    return 0;
}

/* zlib reports a trailer mismatch as Z_DATA_ERROR with one of these
 * messages; every other Z_DATA_ERROR is a malformed stream. */
static int is_checksum_message(const char *message) {
    if (!message) return 0;
    return strstr(message, "incorrect data check") != NULL ||
           strstr(message, "incorrect length check") != NULL;
}

static int64_t status_of(int code) {
    if (code == Z_MEM_ERROR || code == Z_STREAM_ERROR) return GC_SYSTEM;
    if (code == Z_DATA_ERROR) return GC_MALFORMED;
    return GC_SYSTEM;
}

int64_t gc_open(int64_t format, gc_meta meta) {
    /* A handle is nonzero on success and zero on failure; the status is in
     * meta[0] either way. */
    if (meta.len < 2 || !meta.data) return 0;
    meta.data[0] = GC_INVALID;
    meta.data[1] = 0;
    int bits = window_bits(format);
    if (!bits) return 0;
    struct decoder *decoder = calloc(1, sizeof *decoder);
    if (!decoder) {
        meta.data[0] = GC_SYSTEM;
        return 0;
    }
    int code = inflateInit2(&decoder->stream, bits);
    if (code != Z_OK) {
        free(decoder);
        meta.data[0] = GC_SYSTEM;
        return 0;
    }
    decoder->format = (int)format;
    int64_t handle = 0;
    pthread_mutex_lock(&decoders_lock);
    for (size_t i = 0; i < GC_MAX_DECODERS; ++i) {
        decoder_slot *slot = &decoders[i];
        if (!slot->generation) slot->generation = 1;
        if (slot->decoder || slot->generation >= (uint64_t)GC_MAX_GENERATION) continue;
        slot->decoder = decoder;
        handle = encode_handle(i, slot->generation);
        break;
    }
    pthread_mutex_unlock(&decoders_lock);
    if (!handle) {
        inflateEnd(&decoder->stream);
        free(decoder);
        meta.data[0] = GC_LIMIT;
        return 0;
    }
    meta.data[0] = GC_OK;
    meta.data[1] = bits;
    return handle;
}

int64_t gc_dictionary(int64_t handle, gc_input dictionary, gc_meta meta) {
    if (meta.len < 1 || !meta.data) return GC_INVALID;
    meta.data[0] = GC_INVALID;
    if (dictionary.len < 0 || (dictionary.len > 0 && !dictionary.data)) return GC_INVALID;
    if (dictionary.len > 32768) {
        meta.data[0] = GC_LIMIT;
        return GC_LIMIT;
    }
    pthread_mutex_lock(&decoders_lock);
    struct decoder *decoder = find_decoder(handle);
    if (!decoder) {
        pthread_mutex_unlock(&decoders_lock);
        return GC_INVALID;
    }
    if (decoder->format == GC_GZIP || decoder->has_dictionary) {
        pthread_mutex_unlock(&decoders_lock);
        return GC_INVALID;
    }
    uint8_t *copy = NULL;
    if (dictionary.len > 0) {
        copy = malloc((size_t)dictionary.len);
        if (!copy) {
            pthread_mutex_unlock(&decoders_lock);
            meta.data[0] = GC_SYSTEM;
            return GC_SYSTEM;
        }
        memcpy(copy, dictionary.data, (size_t)dictionary.len);
    }
    decoder->dictionary = copy;
    decoder->dictionary_len = (size_t)dictionary.len;
    decoder->has_dictionary = 1;
    /* A raw stream carries no DICTID, so zlib must be told before the first
     * inflate. A zlib stream asks with Z_NEED_DICT and is answered then. */
    int code = Z_OK;
    if (decoder->format == GC_RAW) {
        code = inflateSetDictionary(&decoder->stream, decoder->dictionary,
                                    (uInt)decoder->dictionary_len);
    }
    pthread_mutex_unlock(&decoders_lock);
    if (code != Z_OK) {
        meta.data[0] = status_of(code);
        return status_of(code);
    }
    meta.data[0] = GC_OK;
    return GC_OK;
}

int64_t gc_inflate(int64_t handle, gc_input input, gc_output output, gc_meta meta) {
    if (meta.len < 6 || !meta.data) return GC_INVALID;
    for (int i = 0; i < 6; ++i) meta.data[i] = 0;
    if (input.len < 0 || output.len < 0) return GC_INVALID;
    if (input.len > 0 && !input.data) return GC_INVALID;
    if (output.len > 0 && !output.data) return GC_INVALID;
    if (output.len == 0) return GC_INVALID;
    pthread_mutex_lock(&decoders_lock);
    struct decoder *decoder = find_decoder(handle);
    if (!decoder) {
        pthread_mutex_unlock(&decoders_lock);
        return GC_INVALID;
    }
    z_stream *stream = &decoder->stream;
    stream->next_in = (Bytef *)input.data;
    stream->avail_in = (uInt)(input.len > (int64_t)UINT_MAX ? UINT_MAX : input.len);
    stream->next_out = output.data;
    stream->avail_out = (uInt)(output.len > (int64_t)UINT_MAX ? UINT_MAX : output.len);
    uInt before_in = stream->avail_in;
    uInt before_out = stream->avail_out;
    int code = inflate(stream, Z_NO_FLUSH);
    if (code == Z_NEED_DICT) {
        if (!decoder->has_dictionary) {
            meta.data[0] = (int64_t)(before_in - stream->avail_in);
            meta.data[1] = (int64_t)(before_out - stream->avail_out);
            meta.data[2] = code;
            meta.data[3] = 2;
            meta.data[4] = (int64_t)stream->adler;
            meta.data[5] = GC_DICT_REQUIRED;
            pthread_mutex_unlock(&decoders_lock);
            return GC_DICT_REQUIRED;
        }
        uLong expected = adler32(0L, Z_NULL, 0);
        expected = adler32(expected, decoder->dictionary, (uInt)decoder->dictionary_len);
        if (expected != stream->adler) {
            meta.data[0] = (int64_t)(before_in - stream->avail_in);
            meta.data[1] = (int64_t)(before_out - stream->avail_out);
            meta.data[2] = code;
            meta.data[3] = 2;
            meta.data[4] = (int64_t)stream->adler;
            meta.data[5] = GC_DICT_MISMATCH;
            pthread_mutex_unlock(&decoders_lock);
            return GC_DICT_MISMATCH;
        }
        int set = inflateSetDictionary(stream, decoder->dictionary,
                                       (uInt)decoder->dictionary_len);
        if (set != Z_OK) {
            meta.data[5] = status_of(set);
            pthread_mutex_unlock(&decoders_lock);
            return status_of(set);
        }
        code = inflate(stream, Z_NO_FLUSH);
    }
    meta.data[0] = (int64_t)(before_in - stream->avail_in);
    meta.data[1] = (int64_t)(before_out - stream->avail_out);
    meta.data[2] = code;
    meta.data[3] = code == Z_STREAM_END ? 1 : 0;
    meta.data[4] = (int64_t)stream->adler;
    int64_t status = GC_OK;
    if (code == Z_DATA_ERROR) {
        status = is_checksum_message(stream->msg) ? GC_CHECKSUM : GC_MALFORMED;
    } else if (code != Z_OK && code != Z_STREAM_END && code != Z_BUF_ERROR) {
        status = status_of(code);
    }
    meta.data[5] = status;
    pthread_mutex_unlock(&decoders_lock);
    return status;
}

int64_t gc_reset(int64_t handle, gc_meta meta) {
    if (meta.len < 1 || !meta.data) return GC_INVALID;
    meta.data[0] = GC_INVALID;
    pthread_mutex_lock(&decoders_lock);
    struct decoder *decoder = find_decoder(handle);
    if (!decoder) {
        pthread_mutex_unlock(&decoders_lock);
        return GC_INVALID;
    }
    int code = inflateReset(&decoder->stream);
    /* inflateReset drops the window, and a raw stream carries no DICTID to
     * ask for the dictionary again, so it is re-applied here. A zlib stream
     * asks with Z_NEED_DICT on its own. */
    if (code == Z_OK && decoder->format == GC_RAW && decoder->has_dictionary) {
        code = inflateSetDictionary(&decoder->stream, decoder->dictionary,
                                    (uInt)decoder->dictionary_len);
    }
    pthread_mutex_unlock(&decoders_lock);
    if (code != Z_OK) {
        meta.data[0] = status_of(code);
        return status_of(code);
    }
    meta.data[0] = GC_OK;
    return GC_OK;
}

int64_t gc_close(int64_t handle, gc_meta meta) {
    if (meta.len < 1 || !meta.data) return GC_INVALID;
    meta.data[0] = GC_INVALID;
    pthread_mutex_lock(&decoders_lock);
    struct decoder *decoder = find_decoder(handle);
    if (!decoder) {
        pthread_mutex_unlock(&decoders_lock);
        return GC_INVALID;
    }
    uint64_t token = (uint64_t)handle;
    uint64_t index = (token & UINT64_C(0x7fff));
    decoder_slot *slot = &decoders[index - 1];
    slot->decoder = NULL;
    slot->generation += 1;
    pthread_mutex_unlock(&decoders_lock);
    inflateEnd(&decoder->stream);
    free(decoder->dictionary);
    free(decoder);
    meta.data[0] = GC_OK;
    return GC_OK;
}

/* "1.3.2" as 10302, "1.2.11" as 10211: each dotted field is a decimal
 * number, so a two-digit patch level stays distinct. */
static int64_t version_number(const char *text) {
    int64_t parts[3] = {0, 0, 0};
    int field = 0;
    for (const char *p = text; *p && field < 3; ++p) {
        if (*p == '.') {
            field += 1;
        } else if (*p >= '0' && *p <= '9') {
            parts[field] = parts[field] * 10 + (*p - '0');
        } else {
            break;
        }
    }
    return parts[0] * 10000 + parts[1] * 100 + parts[2];
}

int64_t gc_abi(gc_meta meta) {
    if (meta.len < 3 || !meta.data) return GC_INVALID;
    meta.data[0] = version_number(zlibVersion());
    meta.data[1] = (int64_t)sizeof(z_stream);
    int64_t live = 0;
    pthread_mutex_lock(&decoders_lock);
    for (size_t i = 0; i < GC_MAX_DECODERS; ++i) {
        if (decoders[i].decoder) live += 1;
    }
    pthread_mutex_unlock(&decoders_lock);
    meta.data[2] = live;
    return GC_OK;
}

int64_t gc_version(gc_output out, gc_meta meta) {
    if (meta.len < 1 || !meta.data) return GC_INVALID;
    meta.data[0] = GC_INVALID;
    if (out.len < 0 || (out.len > 0 && !out.data)) return GC_INVALID;
    const char *text = zlibVersion();
    size_t n = strlen(text);
    if ((int64_t)n > out.len) return GC_LIMIT;
    memcpy(out.data, text, n);
    meta.data[0] = GC_OK;
    return GC_OK;
}
