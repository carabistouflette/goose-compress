/* SPDX-License-Identifier: Apache-2.0 */
/* zlib called directly, streaming, under the same contract goose-compress
 * offers: one gzip member, a hard output budget, a hard input budget, no
 * trailing bytes, and a 64 KiB step. This is the row that says what the
 * bounded incremental state machine costs on top of zlib itself, at equal
 * framing policy rather than at a looser one.
 *
 * It is not a competitor in the "different decoder" sense -- it is the same
 * zlib -- so a difference here is overhead, not a speed ranking. */
#define _POSIX_C_SOURCE 200809L
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <zlib.h>

#define STEP 65536

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

/* Returns 0 on a clean single-member decode, -1 on any contract violation.
 * `produced` is the number of bytes written, which is what the caller keeps
 * even when the call fails. */
static int decode_bounded(const uint8_t *input, size_t input_len,
                          uint8_t *output, size_t output_budget,
                          size_t *produced) {
    z_stream stream;
    memset(&stream, 0, sizeof stream);
    if (inflateInit2(&stream, 15 + 16) != Z_OK) return -1;

    size_t offset = 0;
    size_t written = 0;
    int status = -1;
    for (;;) {
        size_t remaining = output_budget - written;
        /* One byte of room even at a zero budget, so "the stream ended" and
         * "the stream produced a byte" stay distinguishable. */
        size_t room = remaining > 0 ? remaining : 1;
        if (room > STEP) room = STEP;

        stream.next_in = (Bytef *)(input + offset);
        stream.avail_in = (uInt)(input_len - offset);
        stream.next_out = (Bytef *)(output + written);
        stream.avail_out = (uInt)room;

        uInt before_in = stream.avail_in;
        uInt before_out = stream.avail_out;
        int code = inflate(&stream, Z_NO_FLUSH);
        size_t used = (size_t)(before_in - stream.avail_in);
        size_t made = (size_t)(before_out - stream.avail_out);
        offset += used;

        if (made > remaining) {
            /* Over budget: keep the bytes that fit and fail. */
            written += remaining;
            status = -1;
            break;
        }
        written += made;

        if (code == Z_STREAM_END) {
            /* Trailing bytes after the single member are a contract breach. */
            status = (offset == input_len) ? 0 : -1;
            break;
        }
        if (code != Z_OK && code != Z_BUF_ERROR) { status = -1; break; }
        if (code == Z_BUF_ERROR && used == 0 && made == 0) { status = -1; break; }
        if (offset == input_len && made == 0) { status = -1; break; }
    }
    inflateEnd(&stream);
    *produced = written;
    return status;
}

int main(int argc, char **argv) {
    if (argc != 4) {
        fprintf(stderr, "usage: %s <input.gz> <iterations> <budget>\n", argv[0]);
        return 2;
    }
    long iterations = strtol(argv[2], NULL, 10);
    long budget = strtol(argv[3], NULL, 10);
    if (iterations < 1 || budget < 0) {
        fprintf(stderr, "zlib-bounded: bad iterations or budget\n");
        return 2;
    }
    size_t input_len = 0;
    uint8_t *input = read_file(argv[1], &input_len);
    if (!input) {
        fprintf(stderr, "zlib-bounded: cannot read %s\n", argv[1]);
        return 1;
    }
    uint8_t *output = malloc((size_t)budget + 1);
    if (!output) {
        fprintf(stderr, "zlib-bounded: out of memory\n");
        free(input);
        return 1;
    }
    size_t produced = 0;
    int warm = decode_bounded(input, input_len, output, (size_t)budget, &produced);
    uint64_t digest = fnv1a(output, produced);
    int64_t start = now_ns();
    for (long i = 0; i < iterations; ++i) {
        size_t again = 0;
        int code = decode_bounded(input, input_len, output, (size_t)budget, &again);
        if (code != warm || again != produced) {
            fprintf(stderr, "zlib-bounded: timed run diverged\n");
            free(output);
            free(input);
            return 1;
        }
    }
    int64_t elapsed = now_ns() - start;
    printf("mode=bounded input=%zu output=%zu iterations=%ld total_ns=%lld "
           "per_call_ns=%lld fnv1a=%llu error=%s peak_rss_kb=%ld\n",
           input_len, produced, iterations, (long long)elapsed,
           (long long)(elapsed / iterations), (unsigned long long)digest,
           warm == 0 ? "none" : "failed", peak_rss_kb());
    free(output);
    free(input);
    return 0;
}
