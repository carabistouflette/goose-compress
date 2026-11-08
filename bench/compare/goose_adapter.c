/* SPDX-License-Identifier: Apache-2.0 */
/* The goose-compress adapter, timed without the Goose layer.
 *
 * `bench/bench.goose` measures the whole public path: Goose code, the adapter,
 * and zlib. This harness measures the adapter and zlib only, in the same shape
 * as the `oneshot` mode: one open, one feed with the whole input, one close,
 * per iteration. Compiled twice, it answers two questions that the Goose-level
 * row cannot separate:
 *
 *   - adapter + zlib vs `zlib-bounded`: what the adapter itself costs on top
 *     of the same zlib under the same framing policy;
 *   - adapter + zlib-ng vs `zlib-ng-bounded`: the same question on the faster
 *     backend, which is where a fixed per-call cost matters most.
 *
 * The difference between this row and `goose-compress oneshot` is the Goose
 * layer, and it is the same number for both backends.
 *
 *   goose-adapter <input.gz> <iterations> <budget>
 *
 * Built by `make bench-build` (zlib) and `make bench-build-ng` (zlib-ng).
 */
#define _POSIX_C_SOURCE 200809L
#include "compress.h"

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

/* One decode through the public adapter, exactly as `bench.goose` oneshot does
 * it: open, one feed with the whole input, close. Returns the produced length
 * or -1. */
static int64_t decode_once(const uint8_t *input, int64_t input_len,
                           uint8_t *output, int64_t output_len) {
    int64_t open_meta[2] = {0, 0};
    gc_meta open_view = {open_meta, 2};
    int64_t handle = gc_open(GC_GZIP, open_view);
    if (!handle) return -1;
    int64_t meta[6];
    gc_input in = {input, input_len};
    gc_output out = {output, output_len};
    gc_meta view = {meta, 6};
    int64_t status = gc_inflate(handle, in, out, view);
    int64_t produced = meta[1];
    int64_t close_meta[1] = {0};
    gc_meta close_view = {close_meta, 1};
    if (gc_close(handle, close_view) != GC_OK) produced = -1;
    if (status != GC_OK || meta[3] != 1) produced = -1;
    return produced;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <input.gz> <iterations> <budget>\n", argv[0]);
        return 2;
    }
    long iterations = strtol(argv[2], NULL, 10);
    int64_t budget = strtoll(argv[3], NULL, 10);
    if (iterations < 1 || budget < 0) {
        fprintf(stderr, "goose-adapter: bad iterations or budget\n");
        return 2;
    }
    int64_t input_len = 0;
    uint8_t *input = read_file(argv[1], &input_len);
    if (!input) {
        fprintf(stderr, "goose-adapter: cannot read %s\n", argv[1]);
        return 1;
    }
    uint8_t *output = malloc((size_t)budget + 1);
    if (!output) {
        fprintf(stderr, "goose-adapter: out of memory\n");
        free(input);
        return 1;
    }
    /* One warm-up call outside the timed loop, and the digest taken from it so
     * the checksum is not part of the measurement. */
    int64_t produced = decode_once(input, input_len, output, budget);
    if (produced < 0) {
        fprintf(stderr, "goose-adapter: warm-up failed\n");
        free(output);
        free(input);
        return 1;
    }
    uint64_t digest = fnv1a(output, (size_t)produced);
    int64_t start = now_ns();
    for (long i = 0; i < iterations; ++i) {
        if (decode_once(input, input_len, output, budget) != produced) {
            fprintf(stderr, "goose-adapter: timed run diverged\n");
            free(output);
            free(input);
            return 1;
        }
    }
    int64_t elapsed = now_ns() - start;
    printf("mode=oneshot input=%lld output=%lld iterations=%ld total_ns=%lld "
           "per_call_ns=%lld fnv1a=%llu error=none peak_rss_kb=%ld\n",
           (long long)input_len, (long long)produced, iterations,
           (long long)elapsed, (long long)(elapsed / iterations),
           (unsigned long long)digest, peak_rss_kb());
    free(output);
    free(input);
    return 0;
}
