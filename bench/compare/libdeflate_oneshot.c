/* SPDX-License-Identifier: Apache-2.0 */
/* libdeflate at the one-shot bounded contract.
 *
 * libdeflate is whole-buffer, not streaming, but it is NOT unbounded: the
 * caller passes `out_nbytes_avail` and libdeflate refuses to write past it,
 * reporting LIBDEFLATE_INSUFFICIENT_SPACE. For a single call that buffer cap
 * is exactly the output budget, so this row is comparable to goose-compress
 * `oneshot` and is labelled comparable in bench/runner.py.
 *
 * What it does not have, and what this row therefore cannot show:
 *
 *   - a cumulative budget across calls, because there are no calls: the whole
 *     input and the whole output are in memory at once;
 *   - a member policy: libdeflate_gzip_decompress_ex decodes the first member
 *     and reports where it ended through `actual_in_nbytes`, so trailing bytes
 *     are detectable but a second member is not decoded;
 *   - a trailing policy: this row implements Reject only, using
 *     `actual_in_nbytes < in_nbytes`;
 *   - a preset dictionary;
 *   - a checksum-vs-malformed distinction: libdeflate reports
 *     LIBDEFLATE_BAD_DATA for both, so this row prints `malformed_stream`
 *     where goose-compress prints `checksum_mismatch`. That is a real
 *     difference in the error taxonomy, not a defect in either.
 *
 *   libdeflate-oneshot <input.gz> <iterations> <budget> [input_budget]
 *   libdeflate-oneshot --probe <gzip|zlib|raw> <input> <output_budget> \
 *       <input_budget> <chunk> <reject|allow|members> <members>
 *
 * `chunk` is accepted and ignored: there is no incremental step to size.
 */
#define _POSIX_C_SOURCE 200809L
#include <libdeflate.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

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

static uint8_t *read_file(const char *path, size_t *length) {
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
    *length = (size_t)size;
    return data;
}

/* The status vocabulary of the other rows, so a disagreement is a string
 * comparison rather than a translation table. */
enum {
    LD_OK = 0,
    LD_INVALID,
    LD_LIMIT,
    LD_MALFORMED,
    LD_TRAILING,
    LD_SYSTEM
};

static const char *status_name(int status) {
    switch (status) {
        case LD_OK: return "none";
        case LD_INVALID: return "invalid_input";
        case LD_LIMIT: return "limit_exceeded";
        case LD_MALFORMED: return "malformed_stream";
        case LD_TRAILING: return "trailing_data";
        default: return "system_error";
    }
}

typedef struct {
    int status;
    size_t consumed;
    size_t produced;
} ld_result;

/* One bounded one-shot decode. The output cap is the budget; the input budget
 * is a length check, which for a single call is exact and free. */
static ld_result decode_bounded(struct libdeflate_decompressor *decompressor,
                                int format, const uint8_t *input,
                                size_t input_len, int64_t output_budget,
                                int64_t input_budget, uint8_t *output,
                                int trailing_policy) {
    ld_result result = {LD_OK, 0, 0};
    /* The input budget is a caller-side length check, because a one-shot call
     * is handed the whole input at once. It is applied before the decode, so a
     * stream that is both too large and malformed reports limit_exceeded here
     * where goose-compress, which checks the budget as it consumes, may report
     * the stream error first. That ordering difference is the only thing this
     * check disagrees about, and it is a property of the harness, not of
     * libdeflate, which has no input budget at all. */
    if (input_len > (size_t)input_budget) {
        result.status = LD_LIMIT;
        return result;
    }
    if (output_budget < 0) {
        result.status = LD_INVALID;
        return result;
    }
    size_t actual_in = 0;
    size_t actual_out = 0;
    enum libdeflate_result code;
    if (format == 0) {
        code = libdeflate_gzip_decompress_ex(decompressor, input, input_len,
                                             output, (size_t)output_budget,
                                             &actual_in, &actual_out);
    } else if (format == 1) {
        code = libdeflate_zlib_decompress_ex(decompressor, input, input_len,
                                             output, (size_t)output_budget,
                                             &actual_in, &actual_out);
    } else {
        code = libdeflate_deflate_decompress_ex(decompressor, input, input_len,
                                                output, (size_t)output_budget,
                                                &actual_in, &actual_out);
    }
    switch (code) {
        case LIBDEFLATE_SUCCESS:
            result.status = LD_OK;
            result.consumed = actual_in;
            result.produced = actual_out;
            /* Bytes after the member. Reject is the only policy this row
             * implements; Allow would count them and Members would decode
             * them, and libdeflate does neither. */
            if (trailing_policy == 0 && actual_in < input_len) {
                result.status = LD_TRAILING;
            }
            return result;
        case LIBDEFLATE_INSUFFICIENT_SPACE:
            /* The stream wanted more than the budget. libdeflate does not say
             * how much it produced before it stopped, so the counts stay 0. */
            result.status = LD_LIMIT;
            return result;
        case LIBDEFLATE_BAD_DATA:
            /* Covers a malformed stream, a truncated stream and a trailer
             * mismatch alike: libdeflate has no finer code. */
            result.status = LD_MALFORMED;
            return result;
        case LIBDEFLATE_SHORT_OUTPUT:
        default:
            result.status = LD_SYSTEM;
            return result;
    }
}

static int parse_format(const char *text) {
    if (strcmp(text, "gzip") == 0) return 0;
    if (strcmp(text, "zlib") == 0) return 1;
    if (strcmp(text, "raw") == 0) return 2;
    return -1;
}

static int parse_policy(const char *text) {
    if (strcmp(text, "reject") == 0) return 0;
    if (strcmp(text, "allow") == 0) return 1;
    if (strcmp(text, "members") == 0) return 2;
    return -1;
}

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
        fprintf(stderr, "libdeflate-oneshot: bad format or policy\n");
        return 2;
    }
    if (policy != 0) {
        /* This row implements Trailing.Reject only. Saying so is the point:
         * reporting `none` for a policy the row does not implement would look
         * like agreement with the library when it is silence. */
        printf("error=unsupported_policy produced=0 consumed=0 members=0 "
               "trailing=0 fnv1a=%llu\n",
               (unsigned long long)fnv1a(NULL, 0));
        return 0;
    }
    int64_t output_budget = strtoll(argv[4], NULL, 10);
    int64_t input_budget = strtoll(argv[5], NULL, 10);
    size_t input_len = 0;
    uint8_t *input = read_file(argv[3], &input_len);
    if (!input) {
        fprintf(stderr, "libdeflate-oneshot: cannot read %s\n", argv[3]);
        return 1;
    }
    size_t cap = output_budget > 0 ? (size_t)output_budget : 1;
    uint8_t *output = malloc(cap);
    struct libdeflate_decompressor *decompressor = libdeflate_alloc_decompressor();
    if (!output || !decompressor) {
        fprintf(stderr, "libdeflate-oneshot: out of memory\n");
        free(output);
        free(input);
        return 1;
    }
    ld_result result = decode_bounded(decompressor, format, input, input_len,
                                      output_budget, input_budget, output,
                                      policy);
    printf("error=%s produced=%zu consumed=%zu members=0 trailing=0 "
           "fnv1a=%llu\n", status_name(result.status), result.produced,
           result.consumed,
           (unsigned long long)fnv1a(output, result.produced));
    libdeflate_free_decompressor(decompressor);
    free(output);
    free(input);
    return 0;
}

int main(int argc, char **argv) {
    if (argc >= 2 && strcmp(argv[1], "--probe") == 0) return probe(argc, argv);
    if (argc != 4 && argc != 5) {
        fprintf(stderr, "usage: %s <input.gz> <iterations> <budget> "
                        "[input_budget]\n", argv[0]);
        return 2;
    }
    long iterations = strtol(argv[2], NULL, 10);
    int64_t budget = strtoll(argv[3], NULL, 10);
    int64_t input_budget = argc == 5 ? strtoll(argv[4], NULL, 10)
                                     : (int64_t)1 << 40;
    if (iterations < 1 || budget < 0) {
        fprintf(stderr, "libdeflate-oneshot: bad iterations or budget\n");
        return 2;
    }
    size_t input_len = 0;
    uint8_t *input = read_file(argv[1], &input_len);
    if (!input) {
        fprintf(stderr, "libdeflate-oneshot: cannot read %s\n", argv[1]);
        return 1;
    }
    size_t cap = budget > 0 ? (size_t)budget : 1;
    uint8_t *output = malloc(cap);
    struct libdeflate_decompressor *decompressor = libdeflate_alloc_decompressor();
    if (!output || !decompressor) {
        fprintf(stderr, "libdeflate-oneshot: out of memory\n");
        free(output);
        free(input);
        return 1;
    }
    /* One warm-up call outside the timed loop, and the digest taken from it so
     * the checksum is not part of the measurement. */
    ld_result warm = decode_bounded(decompressor, 0, input, input_len, budget,
                                    input_budget, output, 0);
    if (warm.status != LD_OK) {
        fprintf(stderr, "libdeflate-oneshot: warm-up failed: %s\n",
                status_name(warm.status));
        libdeflate_free_decompressor(decompressor);
        free(output);
        free(input);
        return 1;
    }
    uint64_t digest = fnv1a(output, warm.produced);
    size_t produced = warm.produced;
    int64_t start = now_ns();
    for (long i = 0; i < iterations; ++i) {
        ld_result result = decode_bounded(decompressor, 0, input, input_len,
                                          budget, input_budget, output, 0);
        if (result.status != LD_OK || result.produced != produced) {
            fprintf(stderr, "libdeflate-oneshot: timed run diverged\n");
            libdeflate_free_decompressor(decompressor);
            free(output);
            free(input);
            return 1;
        }
    }
    int64_t elapsed = now_ns() - start;
    printf("mode=oneshot input=%zu output=%zu iterations=%ld total_ns=%lld "
           "per_call_ns=%lld fnv1a=%llu error=none peak_rss_kb=%ld\n",
           input_len, produced, iterations, (long long)elapsed,
           (long long)(elapsed / iterations), (unsigned long long)digest,
           peak_rss_kb());
    libdeflate_free_decompressor(decompressor);
    free(output);
    free(input);
    return 0;
}
