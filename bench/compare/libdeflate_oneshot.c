/* SPDX-License-Identifier: Apache-2.0 */
/* A maintained competitor at the same contract: one gzip stream in, the
 * whole payload out, no bounds. libdeflate is a separate implementation, so
 * this row measures the cost of bounded incremental decoding against a
 * different decoder, not against zlib's own overhead. */
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

/* One decompressor for the whole run, as a caller would keep one. */
static int decompress_once(struct libdeflate_decompressor *decompressor,
                           const uint8_t *input, size_t input_len,
                           uint8_t *output, size_t output_cap, size_t *produced) {
    enum libdeflate_result result = libdeflate_gzip_decompress_ex(
        decompressor, input, input_len, output, output_cap, &input_len, produced);
    return result == LIBDEFLATE_SUCCESS ? 0 : -1;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <input.gz> <iterations> <budget>\n", argv[0]);
        return 2;
    }
    long iterations = strtol(argv[2], NULL, 10);
    long budget = strtol(argv[3], NULL, 10);
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
    uint8_t *output = malloc((size_t)budget + 1);
    struct libdeflate_decompressor *decompressor = libdeflate_alloc_decompressor();
    if (!output || !decompressor) {
        fprintf(stderr, "libdeflate-oneshot: out of memory\n");
        free(output);
        free(input);
        return 1;
    }
    size_t produced = 0;
    if (decompress_once(decompressor, input, input_len, output, (size_t)budget,
                        &produced) != 0) {
        fprintf(stderr, "libdeflate-oneshot: warm-up failed\n");
        libdeflate_free_decompressor(decompressor);
        free(output);
        free(input);
        return 1;
    }
    uint64_t digest = fnv1a(output, produced);
    int64_t start = now_ns();
    for (long i = 0; i < iterations; ++i) {
        if (decompress_once(decompressor, input, input_len, output, (size_t)budget,
                            &produced) != 0) {
            fprintf(stderr, "libdeflate-oneshot: timed run failed\n");
            libdeflate_free_decompressor(decompressor);
            free(output);
            free(input);
            return 1;
        }
    }
    int64_t elapsed = now_ns() - start;
    printf("mode=oneshot input=%zu output=%zu iterations=%ld total_ns=%lld "
           "per_call_ns=%lld fnv1a=%llu peak_rss_kb=%ld\n",
           input_len, produced, iterations, (long long)elapsed,
           (long long)(elapsed / iterations), (unsigned long long)digest,
           peak_rss_kb());
    libdeflate_free_decompressor(decompressor);
    free(output);
    free(input);
    return 0;
}
