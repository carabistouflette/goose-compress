/* SPDX-License-Identifier: Apache-2.0 */
/* zlib called directly, streaming, under the same contract goose-compress
 * offers: one gzip/zlib/raw stream, a cumulative output budget, a cumulative
 * input budget, a member policy, a trailing-byte policy, and a 64 KiB output
 * step. This is the row that says what the bounded incremental state machine
 * costs on top of zlib itself, at equal framing policy rather than at a looser
 * one.
 *
 * It is not a competitor in the "different decoder" sense -- it is the same
 * zlib -- so a difference here is overhead, not a speed ranking.
 *
 * The contract is not asserted by this comment. `--probe` prints the full
 * outcome for one stream, and `bench/compare/contract_parity.py` runs this
 * binary and the Goose library over every fixture at several chunkings and
 * requires the same error, the same produced bytes, the same consumed count,
 * the same member count and the same trailing count. A row that has not passed
 * that parity check is not an equal-contract row.
 *
 *   zlib_bounded <stream|whole> <input> <iterations> <output_budget> [input_budget]
 *   zlib_bounded --probe <gzip|zlib|raw> <input> <output_budget> <input_budget> \
 *                <chunk> <reject|allow|members> <members>
 */
#define _POSIX_C_SOURCE 200809L
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#if defined(ZB_ZLIB_NG)
#include "zlib_ng_shim.h"
#else
#include <zlib.h>
#endif

/* The same status codes goose-compress reports, so a parity check can compare
 * them directly instead of translating. */
enum {
    ZB_OK = 0,
    ZB_INVALID = 1,
    ZB_LIMIT = 2,
    ZB_MALFORMED = 3,
    ZB_CHECKSUM = 4,
    ZB_TRAILING = 5,
    ZB_MEMBERS = 6,
    ZB_DICT_REQUIRED = 7,
    ZB_DICT_MISMATCH = 8,
    ZB_SYSTEM = 9,
    ZB_TRUNCATED = 10
};

enum { ZB_GZIP = 0, ZB_ZLIB = 1, ZB_RAW = 2 };
enum { ZB_REJECT = 0, ZB_ALLOW = 1, ZB_MEMBERS_POLICY = 2 };
enum { ZB_ACTIVE = 0, ZB_MEMBER_END = 1, ZB_DONE = 2, ZB_FAILED = 3 };

#define ZB_STEP 65536
#define ZB_MAX_OUTPUT (INT64_C(1073741824))
#define ZB_MAX_INPUT (INT64_C(1073741824))
#define ZB_MAX_MEMBERS (INT64_C(1024))

/* The same names goose-compress reports, with underscores so that a probe line
 * stays one whitespace-separated token per field. */
static const char *status_name(int status) {
    switch (status) {
        case ZB_OK: return "none";
        case ZB_INVALID: return "invalid_input";
        case ZB_LIMIT: return "limit_exceeded";
        case ZB_MALFORMED: return "malformed_stream";
        case ZB_CHECKSUM: return "checksum_mismatch";
        case ZB_TRAILING: return "trailing_data";
        case ZB_MEMBERS: return "too_many_members";
        case ZB_DICT_REQUIRED: return "dictionary_required";
        case ZB_DICT_MISMATCH: return "dictionary_mismatch";
        case ZB_SYSTEM: return "system_error";
        default: return "truncated_stream";
    }
}

/* zlib takes uInt for avail_in/avail_out. The adapter clamps the same way, so
 * a slice longer than UINT_MAX is truncated identically on both sides. */
static int64_t clamp_u32(int64_t n) {
    if (n < 0) return 0;
    return n > (int64_t)UINT_MAX ? (int64_t)UINT_MAX : n;
}

typedef struct {
    z_stream stream;
    int format;
    int64_t output_budget;
    int64_t input_budget;
    int64_t members_limit;
    int trailing_policy;
    int64_t consumed;
    int64_t produced;
    int64_t members;
    int64_t trailing;
    int state;
    int error;
} zb;

static int window_bits(int format) {
    if (format == ZB_GZIP) return 15 + 16;
    if (format == ZB_ZLIB) return 15;
    return -15;
}

/* zlib reports a trailer mismatch as Z_DATA_ERROR with one of these messages;
 * every other Z_DATA_ERROR is a malformed stream. This is a message match, not
 * a dedicated code, so it is backend-dependent: `contract_parity.py` asserts
 * the classification on whichever zlib is actually linked, and the zlib-ng
 * build re-runs the same check rather than assuming the strings match. */
static int is_checksum_message(const char *message) {
    if (!message) return 0;
    return strstr(message, "incorrect data check") != NULL ||
           strstr(message, "incorrect length check") != NULL;
}

static int status_of(int code) {
    if (code == Z_MEM_ERROR || code == Z_STREAM_ERROR) return ZB_SYSTEM;
    return ZB_SYSTEM;
}

static int zb_open(zb *state, int format, int64_t output_budget,
                   int64_t input_budget, int64_t members_limit,
                   int trailing_policy) {
    memset(state, 0, sizeof *state);
    if (output_budget < 0 || output_budget > ZB_MAX_OUTPUT) return ZB_INVALID;
    if (input_budget < 0 || input_budget > ZB_MAX_INPUT) return ZB_INVALID;
    if (members_limit < 1 || members_limit > ZB_MAX_MEMBERS) return ZB_INVALID;
    if (trailing_policy != ZB_MEMBERS_POLICY && members_limit != 1) return ZB_INVALID;
    if (inflateInit2(&state->stream, window_bits(format)) != Z_OK) return ZB_SYSTEM;
    state->format = format;
    state->output_budget = output_budget;
    state->input_budget = input_budget;
    state->members_limit = members_limit;
    state->trailing_policy = trailing_policy;
    state->state = ZB_ACTIVE;
    return ZB_OK;
}

static int zb_fail(zb *state, int error) {
    state->error = error;
    state->state = ZB_FAILED;
    return error;
}

/* One `feed` call, transliterated from compress.goose so that the two can be
 * compared line by line. `out` has room for output_budget + 1 bytes. */
static int zb_feed(zb *state, const uint8_t *input, int64_t input_len,
                   uint8_t *out) {
    if (state->state == ZB_FAILED) return state->error;
    if (state->state == ZB_DONE) return ZB_INVALID;

    int64_t allowed = state->input_budget - state->consumed;
    if (allowed < 0) return zb_fail(state, ZB_LIMIT);
    int64_t usable = input_len < allowed ? input_len : allowed;
    int over = usable < input_len;
    const uint8_t *data = input;
    int64_t data_len = usable;

    if (state->state == ZB_MEMBER_END) {
        if (data_len == 0) {
            if (over) return zb_fail(state, ZB_LIMIT);
            return ZB_OK;
        }
        if (state->trailing_policy == ZB_ALLOW) {
            state->consumed += data_len;
            state->trailing += data_len;
            if (over) return zb_fail(state, ZB_LIMIT);
            return ZB_OK;
        }
        if (state->trailing_policy == ZB_MEMBERS_POLICY &&
            state->members < state->members_limit) {
            if (inflateReset(&state->stream) != Z_OK) return zb_fail(state, ZB_SYSTEM);
            state->state = ZB_ACTIVE;
        } else if (state->trailing_policy == ZB_MEMBERS_POLICY) {
            return zb_fail(state, ZB_MEMBERS);
        } else {
            return zb_fail(state, ZB_TRAILING);
        }
    }

    int64_t offset = 0;
    int pending = 0;
    for (;;) {
        if (offset >= data_len && !pending) break;
        int64_t remaining = state->output_budget - state->produced;
        int64_t reserve = remaining > 1 ? remaining : 1;
        if (reserve > ZB_STEP) reserve = ZB_STEP;

        state->stream.next_in = (Bytef *)(data + offset);
        state->stream.avail_in = (uInt)clamp_u32(data_len - offset);
        state->stream.next_out = out + state->produced;
        state->stream.avail_out = (uInt)clamp_u32(reserve);

        uInt before_in = state->stream.avail_in;
        uInt before_out = state->stream.avail_out;
        int code = inflate(&state->stream, Z_NO_FLUSH);
        int64_t consumed = (int64_t)(before_in - state->stream.avail_in);
        int64_t made = (int64_t)(before_out - state->stream.avail_out);
        offset += consumed;
        state->consumed += consumed;

        if (made > remaining) {
            state->produced += remaining;
            return zb_fail(state, ZB_LIMIT);
        }
        state->produced += made;

        if (code == Z_DATA_ERROR) {
            return zb_fail(state, is_checksum_message(state->stream.msg)
                                      ? ZB_CHECKSUM : ZB_MALFORMED);
        }
        /* This reference never supplies a preset dictionary, so a stream that
         * asks for one is reported the way the library reports it when the
         * caller supplied none. Dictionary *mismatch* needs a supplied
         * dictionary and is therefore out of this row's scope; the fixtures
         * that need one are excluded from the parity check. */
        if (code == Z_NEED_DICT) return zb_fail(state, ZB_DICT_REQUIRED);
        if (code != Z_OK && code != Z_STREAM_END && code != Z_BUF_ERROR) {
            return zb_fail(state, status_of(code));
        }
        pending = made == reserve;

        if (code == Z_STREAM_END) {
            state->members += 1;
            if (offset < data_len) {
                if (state->trailing_policy == ZB_MEMBERS_POLICY &&
                    state->members < state->members_limit) {
                    if (inflateReset(&state->stream) != Z_OK) {
                        return zb_fail(state, ZB_SYSTEM);
                    }
                    pending = 0;
                    continue;
                }
                if (state->trailing_policy == ZB_ALLOW) {
                    state->consumed += data_len - offset;
                    state->trailing += data_len - offset;
                    offset = data_len;
                    state->state = ZB_MEMBER_END;
                    pending = 0;
                    break;
                }
                if (state->trailing_policy == ZB_MEMBERS_POLICY) {
                    return zb_fail(state, ZB_MEMBERS);
                }
                return zb_fail(state, ZB_TRAILING);
            }
            state->state = ZB_MEMBER_END;
            pending = 0;
            break;
        }
        if (code == Z_BUF_ERROR && consumed == 0 && made == 0) break;
    }
    if (over) return zb_fail(state, ZB_LIMIT);
    return ZB_OK;
}

static int zb_finish(zb *state) {
    if (state->state == ZB_FAILED) return state->error;
    if (state->state == ZB_DONE) return ZB_OK;
    if (state->state == ZB_MEMBER_END) {
        state->state = ZB_DONE;
        return ZB_OK;
    }
    return zb_fail(state, ZB_TRUNCATED);
}

static void zb_close(zb *state) {
    inflateEnd(&state->stream);
}

/* Runs one whole stream: open, feed in `chunk`-sized slices, finish, close. */
static int zb_decode(const uint8_t *input, int64_t input_len, int format,
                     int64_t output_budget, int64_t input_budget, int64_t chunk,
                     int trailing_policy, int64_t members_limit,
                     uint8_t *out, zb *result) {
    zb state;
    int status = zb_open(&state, format, output_budget, input_budget,
                         members_limit, trailing_policy);
    if (status != ZB_OK) {
        memset(result, 0, sizeof *result);
        result->error = status;
        result->state = ZB_FAILED;
        return status;
    }
    int64_t offset = 0;
    status = ZB_OK;
    while (offset < input_len) {
        int64_t end = offset + chunk;
        if (end > input_len) end = input_len;
        status = zb_feed(&state, input + offset, end - offset, out);
        if (status != ZB_OK) break;
        offset = end;
    }
    if (status == ZB_OK) status = zb_finish(&state);
    *result = state;
    result->error = status;
    zb_close(&state);
    return status;
}

static uint64_t fnv1a(const uint8_t *data, size_t n) {
    uint64_t hash = UINT64_C(14695981039346656037);
    for (size_t i = 0; i < n; ++i) {
        hash ^= data[i];
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

/* Peak resident set size in KiB, so a row that traded memory for time shows
 * it. -1 when /proc is unavailable. */
static long peak_rss_kb(void) {
    FILE *file = fopen("/proc/self/status", "r");
    if (!file) return -1;
    char line[256];
    long value = -1;
    while (fgets(line, sizeof line, file)) {
        if (strncmp(line, "VmHWM:", 6) == 0) {
            value = strtol(line + 6, NULL, 10);
            break;
        }
    }
    fclose(file);
    return value;
}

static uint8_t *read_file(const char *path, int64_t *length) {
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    if (fseek(file, 0, SEEK_END) != 0) { fclose(file); return NULL; }
    long size = ftell(file);
    if (size < 0 || fseek(file, 0, SEEK_SET) != 0) { fclose(file); return NULL; }
    uint8_t *data = malloc((size_t)size + 1);
    if (!data) { fclose(file); return NULL; }
    if (fread(data, 1, (size_t)size, file) != (size_t)size) {
        free(data);
        fclose(file);
        return NULL;
    }
    fclose(file);
    *length = (int64_t)size;
    return data;
}

static int parse_format(const char *text) {
    if (strcmp(text, "gzip") == 0) return ZB_GZIP;
    if (strcmp(text, "zlib") == 0) return ZB_ZLIB;
    if (strcmp(text, "raw") == 0) return ZB_RAW;
    return -1;
}

static int parse_policy(const char *text) {
    if (strcmp(text, "reject") == 0) return ZB_REJECT;
    if (strcmp(text, "allow") == 0) return ZB_ALLOW;
    if (strcmp(text, "members") == 0) return ZB_MEMBERS_POLICY;
    return -1;
}

/* The clamp is defensive: `limits.input_bytes` is capped at 1 GiB and the
 * output step at 64 KiB, so no slice the documented contract allows can exceed
 * UINT_MAX. It is still checked, because a reference row that silently
 * truncated a long slice differently from the adapter would not be an
 * equal-contract row. */
static int clamp_selftest(void) {
    static const struct { int64_t in; int64_t want; } cases[] = {
        {-1, 0},
        {0, 0},
        {1, 1},
        {(int64_t)UINT_MAX - 1, (int64_t)UINT_MAX - 1},
        {(int64_t)UINT_MAX, (int64_t)UINT_MAX},
        {(int64_t)UINT_MAX + 1, (int64_t)UINT_MAX},
        {INT64_C(4294967296) + 10, (int64_t)UINT_MAX},
        {INT64_MAX, (int64_t)UINT_MAX},
    };
    int checks = 0;
    int failures = 0;
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; ++i) {
        checks += 1;
        if (clamp_u32(cases[i].in) != cases[i].want) {
            fprintf(stderr, "clamp: %lld -> %lld, expected %lld\n",
                    (long long)cases[i].in, (long long)clamp_u32(cases[i].in),
                    (long long)cases[i].want);
            failures += 1;
        }
    }
    if (failures) {
        fprintf(stderr, "clamp selftest: %d of %d checks failed\n",
                failures, checks);
        return 1;
    }
    printf("clamp ok: %d checks\n", checks);
    return 0;
}

/* Prints the full outcome of one stream so a parity check can compare it with
 * the Goose library's answer field by field. */
static int probe(int argc, char **argv) {
    if (argc != 9) {
        fprintf(stderr, "usage: %s --probe <gzip|zlib|raw> <input> "
                        "<output_budget> <input_budget> <chunk> "
                        "<reject|allow|members> <members>\n", argv[0]);
        return 2;
    }
    int format = parse_format(argv[2]);
    int policy = parse_policy(argv[7]);
    if (format < 0 || policy < 0) {
        fprintf(stderr, "zlib-bounded: bad format or policy\n");
        return 2;
    }
    int64_t output_budget = strtoll(argv[4], NULL, 10);
    int64_t input_budget = strtoll(argv[5], NULL, 10);
    int64_t chunk = strtoll(argv[6], NULL, 10);
    int64_t members = strtoll(argv[8], NULL, 10);
    if (chunk < 1) {
        fprintf(stderr, "zlib-bounded: chunk must be positive\n");
        return 2;
    }
    int64_t input_len = 0;
    uint8_t *input = read_file(argv[3], &input_len);
    if (!input) {
        fprintf(stderr, "zlib-bounded: cannot read %s\n", argv[3]);
        return 1;
    }
    int64_t capacity = output_budget > 0 ? output_budget + 1 : 1;
    uint8_t *out = malloc((size_t)capacity);
    if (!out) {
        fprintf(stderr, "zlib-bounded: out of memory\n");
        free(input);
        return 1;
    }
    zb result;
    int status = zb_decode(input, input_len, format, output_budget, input_budget,
                           chunk, policy, members, out, &result);
    printf("error=%s produced=%lld consumed=%lld members=%lld trailing=%lld "
           "fnv1a=%llu\n",
           status_name(status), (long long)result.produced,
           (long long)result.consumed, (long long)result.members,
           (long long)result.trailing,
           (unsigned long long)fnv1a(out, (size_t)result.produced));
    free(out);
    free(input);
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "--probe") == 0) return probe(argc, argv);
    if (argc >= 2 && strcmp(argv[1], "--clamp-selftest") == 0) {
        return clamp_selftest();
    }
    if (argc != 5 && argc != 6) {
        fprintf(stderr, "usage: %s <stream|whole> <input> <iterations> "
                        "<output_budget> [input_budget]\n", argv[0]);
        return 2;
    }
    int64_t chunk;
    if (strcmp(argv[1], "stream") == 0) chunk = ZB_STEP;
    else if (strcmp(argv[1], "whole") == 0) chunk = INT64_MAX;
    else {
        fprintf(stderr, "zlib-bounded: mode must be stream or whole\n");
        return 2;
    }
    long iterations = strtol(argv[3], NULL, 10);
    int64_t budget = strtoll(argv[4], NULL, 10);
    if (iterations < 1 || budget < 0) {
        fprintf(stderr, "zlib-bounded: bad iterations or budget\n");
        return 2;
    }
    int64_t input_len = 0;
    uint8_t *input = read_file(argv[2], &input_len);
    if (!input) {
        fprintf(stderr, "zlib-bounded: cannot read %s\n", argv[2]);
        return 1;
    }
    /* The Goose rows set input_bytes to the whole input, so the input budget
     * never binds there. Default to the same value; an explicit sixth argument
     * lets a caller make it bind. */
    int64_t input_budget = input_len;
    if (argc == 6) input_budget = strtoll(argv[5], NULL, 10);
    if (input_budget < 0) {
        fprintf(stderr, "zlib-bounded: bad input budget\n");
        free(input);
        return 2;
    }
    int64_t capacity = budget > 0 ? budget + 1 : 1;
    uint8_t *output = malloc((size_t)capacity);
    if (!output) {
        fprintf(stderr, "zlib-bounded: out of memory\n");
        free(input);
        return 1;
    }
    zb warm;
    int warm_status = zb_decode(input, input_len, ZB_GZIP, budget, input_budget,
                                chunk, ZB_REJECT, 1, output, &warm);
    uint64_t digest = fnv1a(output, (size_t)warm.produced);
    int64_t produced = warm.produced;
    int64_t start = now_ns();
    for (long i = 0; i < iterations; ++i) {
        zb again;
        int code = zb_decode(input, input_len, ZB_GZIP, budget, input_budget,
                             chunk, ZB_REJECT, 1, output, &again);
        if (code != warm_status || again.produced != produced) {
            fprintf(stderr, "zlib-bounded: timed run diverged\n");
            free(output);
            free(input);
            return 1;
        }
    }
    int64_t elapsed = now_ns() - start;
    printf("mode=%s input=%lld output=%lld iterations=%ld total_ns=%lld "
           "per_call_ns=%lld fnv1a=%llu error=%s peak_rss_kb=%ld\n",
           argv[1], (long long)input_len, (long long)produced, iterations,
           (long long)elapsed, (long long)(elapsed / iterations),
           (unsigned long long)digest, status_name(warm_status), peak_rss_kb());
    free(output);
    free(input);
    return 0;
}
