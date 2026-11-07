/* SPDX-License-Identifier: Apache-2.0 */
/* The same library goose-compress wraps, called one-shot with no bounds.
 * This is the overhead baseline: it isolates what the bounded incremental
 * state machine costs on top of zlib itself. It is not a competitor. */
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <zlib.h>

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

/* One-shot inflate into a buffer the caller sized from the gzip ISIZE. */
static int inflate_once(const uint8_t *input, size_t input_len,
                        uint8_t *output, size_t output_cap, size_t *produced) {
    z_stream stream;
    memset(&stream, 0, sizeof stream);
    if (inflateInit2(&stream, 15 + 16) != Z_OK) return -1;
    stream.next_in = (Bytef *)input;
    stream.avail_in = (uInt)input_len;
    stream.next_out = output;
    stream.avail_out = (uInt)output_cap;
    int code = inflate(&stream, Z_FINISH);
    *produced = (size_t)stream.total_out;
    inflateEnd(&stream);
    return code == Z_STREAM_END ? 0 : -1;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <input.gz> <iterations> <budget>\n", argv[0]);
        return 2;
    }
    long iterations = strtol(argv[2], NULL, 10);
    long budget = strtol(argv[3], NULL, 10);
    if (iterations < 1 || budget < 0) {
        fprintf(stderr, "zlib-oneshot: bad iterations or budget\n");
        return 2;
    }
    size_t input_len = 0;
    uint8_t *input = read_file(argv[1], &input_len);
    if (!input) {
        fprintf(stderr, "zlib-oneshot: cannot read %s\n", argv[1]);
        return 1;
    }
    uint8_t *output = malloc((size_t)budget + 1);
    if (!output) {
        fprintf(stderr, "zlib-oneshot: out of memory\n");
        free(input);
        return 1;
    }
    size_t produced = 0;
    if (inflate_once(input, input_len, output, (size_t)budget, &produced) != 0) {
        fprintf(stderr, "zlib-oneshot: warm-up failed\n");
        free(output);
        free(input);
        return 1;
    }
    uint64_t digest = fnv1a(output, produced);
    int64_t start = now_ns();
    for (long i = 0; i < iterations; ++i) {
        if (inflate_once(input, input_len, output, (size_t)budget, &produced) != 0) {
            fprintf(stderr, "zlib-oneshot: timed run failed\n");
            free(output);
            free(input);
            return 1;
        }
    }
    int64_t elapsed = now_ns() - start;
    printf("mode=oneshot input=%zu output=%zu iterations=%ld total_ns=%lld "
           "per_call_ns=%lld fnv1a=%llu\n",
           input_len, produced, iterations, (long long)elapsed,
           (long long)(elapsed / iterations), (unsigned long long)digest);
    free(output);
    free(input);
    return 0;
}
