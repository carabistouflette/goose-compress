/* SPDX-License-Identifier: Apache-2.0 */
/* Exercises the native adapter directly, so that ASan and UBSan see every
 * entry point with valid, invalid and adversarial arguments. The Goose tests
 * cover the state machine; this covers the C boundary it sits on. */
#define _POSIX_C_SOURCE 200809L
#include "compress.h"

#include <stdio.h>
#include <string.h>
#include <zlib.h>

static int failures;
static int checks;

static void check(int condition, const char *name) {
    checks += 1;
    if (!condition) {
        fprintf(stderr, "driver: failed: %s\n", name);
        failures += 1;
    }
}

/* One final stored block: the simplest well-formed raw DEFLATE stream. */
static int64_t build_raw(uint8_t *out, const uint8_t *data, size_t n) {
    uint16_t nlen = (uint16_t)~(uint16_t)n;
    out[0] = 0x01;
    out[1] = (uint8_t)(n & 0xff);
    out[2] = (uint8_t)((n >> 8) & 0xff);
    out[3] = (uint8_t)(nlen & 0xff);
    out[4] = (uint8_t)((nlen >> 8) & 0xff);
    memcpy(out + 5, data, n);
    return (int64_t)(5 + n);
}

static int64_t build_gzip(uint8_t *out, const uint8_t *data, size_t n) {
    static const uint8_t header[10] = {0x1f, 0x8b, 0x08, 0x00, 0, 0, 0, 0, 0x00, 0x03};
    memcpy(out, header, sizeof header);
    int64_t position = 10 + build_raw(out + 10, data, n);
    uint32_t crc = (uint32_t)crc32(0L, data, (uInt)n);
    uint32_t size = (uint32_t)n;
    for (int i = 0; i < 4; ++i) out[position++] = (uint8_t)((crc >> (8 * i)) & 0xff);
    for (int i = 0; i < 4; ++i) out[position++] = (uint8_t)((size >> (8 * i)) & 0xff);
    return position;
}

static int64_t build_zlib(uint8_t *out, const uint8_t *data, size_t n) {
    uint32_t adler = (uint32_t)adler32(0L, Z_NULL, 0);
    adler = (uint32_t)adler32(adler, data, (uInt)n);
    out[0] = 0x78;
    out[1] = 0x01;
    int64_t position = 2 + build_raw(out + 2, data, n);
    for (int i = 3; i >= 0; --i) out[position++] = (uint8_t)((adler >> (8 * i)) & 0xff);
    return position;
}

static int64_t open_format(int64_t format) {
    int64_t meta[2] = {0, 0};
    gc_meta view = {meta, 2};
    return gc_open(format, view);
}

static int64_t feed(int64_t handle, const uint8_t *in, int64_t in_len,
                    uint8_t *out, int64_t out_len, int64_t *meta) {
    gc_input input = {in, in_len};
    gc_output output = {out, out_len};
    gc_meta view = {meta, 6};
    return gc_inflate(handle, input, output, view);
}

static void test_open_and_close(void) {
    int64_t meta[2] = {0, 0};
    gc_meta view = {meta, 2};
    int64_t gzip = gc_open(GC_GZIP, view);
    check(gzip != 0 && meta[0] == GC_OK, "open gzip");
    int64_t second = gc_open(GC_GZIP, view);
    check(second != 0, "open gzip again");
    int64_t zlib = gc_open(GC_ZLIB, view);
    check(zlib != 0, "open zlib");
    int64_t raw = gc_open(GC_RAW, view);
    check(raw != 0, "open raw");
    check(gc_open(7, view) == 0 && meta[0] == GC_INVALID, "open rejects a bad format");
    check(gc_open(-1, view) == 0 && meta[0] == GC_INVALID, "open rejects a negative format");
    gc_meta short_view = {meta, 1};
    check(gc_open(GC_GZIP, short_view) == 0, "open rejects a short meta");
    gc_meta null_view = {NULL, 2};
    check(gc_open(GC_GZIP, null_view) == 0, "open rejects a null meta");

    int64_t close_meta[1] = {0};
    gc_meta close_view = {close_meta, 1};
    check(gc_close(gzip, close_view) == GC_OK, "close");
    check(gc_close(second, close_view) == GC_OK, "close the second handle");
    check(gc_close(zlib, close_view) == GC_OK, "close zlib");
    check(gc_close(raw, close_view) == GC_OK, "close raw");
    check(gc_close(gzip, close_view) == GC_INVALID, "a stale handle is rejected");
    check(gc_close(0, close_view) == GC_INVALID, "handle zero is rejected");
    check(gc_close(-5, close_view) == GC_INVALID, "a negative handle is rejected");
    check(gc_close(1, close_view) == GC_INVALID, "an unissued handle is rejected");
}

static void test_inflate(void) {
    static const uint8_t payload[] = "goose compress driver payload";
    const size_t payload_len = sizeof payload - 1;
    uint8_t stream[256];
    uint8_t output[256];
    int64_t meta[6];

    int64_t gzip_len = build_gzip(stream, payload, payload_len);
    int64_t handle = open_format(GC_GZIP);
    check(handle != 0, "inflate: open");
    int64_t status = feed(handle, stream, gzip_len, output, sizeof output, meta);
    check(status == GC_OK, "inflate: gzip status");
    check(meta[0] == gzip_len, "inflate: gzip consumed everything");
    check(meta[1] == (int64_t)payload_len, "inflate: gzip produced the payload");
    check(meta[3] == 1, "inflate: gzip reached the stream end");
    check(memcmp(output, payload, payload_len) == 0, "inflate: gzip bytes");
    int64_t close_meta[1] = {0};
    gc_meta close_view = {close_meta, 1};
    check(gc_close(handle, close_view) == GC_OK, "inflate: close");

    /* A zero-length output buffer is refused rather than passed to zlib. */
    handle = open_format(GC_GZIP);
    check(feed(handle, stream, gzip_len, output, 0, meta) == GC_INVALID,
          "inflate: a zero output buffer is invalid");
    check(feed(handle, NULL, 4, output, sizeof output, meta) == GC_INVALID,
          "inflate: a null input with a length is invalid");
    check(feed(handle, stream, -1, output, sizeof output, meta) == GC_INVALID,
          "inflate: a negative input length is invalid");
    check(feed(handle, stream, gzip_len, NULL, 8, meta) == GC_INVALID,
          "inflate: a null output with a length is invalid");
    gc_meta short_view = {meta, 5};
    gc_input input = {stream, gzip_len};
    gc_output out_view = {output, sizeof output};
    check(gc_inflate(handle, input, out_view, short_view) == GC_INVALID,
          "inflate: a short meta is invalid");
    check(gc_close(handle, close_view) == GC_OK, "inflate: close after refusals");

    /* A corrupted trailer is a checksum failure, not a malformed stream. */
    uint8_t broken[256];
    memcpy(broken, stream, (size_t)gzip_len);
    broken[gzip_len - 8] ^= 0xff;
    handle = open_format(GC_GZIP);
    status = feed(handle, broken, gzip_len, output, sizeof output, meta);
    check(status == GC_CHECKSUM, "inflate: a bad CRC is a checksum failure");
    check(meta[1] == (int64_t)payload_len, "inflate: the payload survives the bad CRC");
    check(gc_close(handle, close_view) == GC_OK, "inflate: close after a checksum failure");

    /* An invalid block type is malformed. */
    static const uint8_t bad_block[] = {0x07, 0x00};
    handle = open_format(GC_RAW);
    status = feed(handle, bad_block, sizeof bad_block, output, sizeof output, meta);
    check(status == GC_MALFORMED, "inflate: an invalid block type is malformed");
    check(gc_close(handle, close_view) == GC_OK, "inflate: close after malformed");

    /* A truncated stream simply stops; the caller decides it is truncated. */
    handle = open_format(GC_GZIP);
    status = feed(handle, stream, gzip_len - 4, output, sizeof output, meta);
    check(status == GC_OK && meta[3] == 0, "inflate: a truncated stream is not an error");
    check(gc_close(handle, close_view) == GC_OK, "inflate: close after truncation");

    /* zlib and raw wrappers decode the same payload. */
    int64_t zlib_len = build_zlib(stream, payload, payload_len);
    handle = open_format(GC_ZLIB);
    status = feed(handle, stream, zlib_len, output, sizeof output, meta);
    check(status == GC_OK && meta[1] == (int64_t)payload_len && meta[3] == 1,
          "inflate: zlib");
    check(gc_close(handle, close_view) == GC_OK, "inflate: close zlib");

    int64_t raw_len = build_raw(stream, payload, payload_len);
    handle = open_format(GC_RAW);
    status = feed(handle, stream, raw_len, output, sizeof output, meta);
    check(status == GC_OK && meta[1] == (int64_t)payload_len && meta[3] == 1,
          "inflate: raw");
    check(gc_close(handle, close_view) == GC_OK, "inflate: close raw");
}

static void test_dictionary(void) {
    static const uint8_t dictionary[] = "the quick brown fox jumps over the lazy dog";
    static const uint8_t payload[] = "the quick brown fox the quick brown fox";
    uint8_t stream[256];
    uint8_t output[256];
    int64_t meta[6];
    int64_t dict_meta[1] = {0};
    gc_meta dict_view = {dict_meta, 1};
    gc_input dict = {dictionary, (int64_t)sizeof dictionary - 1};
    int64_t close_meta[1] = {0};
    gc_meta close_view = {close_meta, 1};

    /* A raw stream takes the dictionary before its first byte. */
    z_stream encoder;
    memset(&encoder, 0, sizeof encoder);
    check(deflateInit2(&encoder, 6, Z_DEFLATED, -15, 9, Z_DEFAULT_STRATEGY) == Z_OK,
          "dictionary: encoder init");
    check(deflateSetDictionary(&encoder, dictionary, (uInt)sizeof dictionary - 1) == Z_OK,
          "dictionary: encoder dictionary");
    encoder.next_in = (Bytef *)payload;
    encoder.avail_in = (uInt)sizeof payload - 1;
    encoder.next_out = stream;
    encoder.avail_out = (uInt)sizeof stream;
    check(deflate(&encoder, Z_FINISH) == Z_STREAM_END, "dictionary: encode");
    int64_t raw_len = (int64_t)encoder.total_out;
    deflateEnd(&encoder);

    int64_t handle = open_format(GC_RAW);
    check(gc_dictionary(handle, dict, dict_view) == GC_OK, "dictionary: raw registration");
    check(gc_dictionary(handle, dict, dict_view) == GC_INVALID,
          "dictionary: a second registration is refused");
    check(feed(handle, stream, raw_len, output, sizeof output, meta) == GC_OK,
          "dictionary: raw decode");
    check(meta[1] == (int64_t)sizeof payload - 1, "dictionary: raw length");
    check(memcmp(output, payload, sizeof payload - 1) == 0, "dictionary: raw bytes");
    check(gc_close(handle, close_view) == GC_OK, "dictionary: close raw");

    /* gzip has no dictionary. */
    handle = open_format(GC_GZIP);
    check(gc_dictionary(handle, dict, dict_view) == GC_INVALID,
          "dictionary: gzip refuses one");
    check(gc_close(handle, close_view) == GC_OK, "dictionary: close gzip");

    /* An oversized dictionary is refused before it is copied. */
    uint8_t oversized[32769];
    memset(oversized, 0, sizeof oversized);
    gc_input big = {oversized, (int64_t)sizeof oversized};
    handle = open_format(GC_RAW);
    check(gc_dictionary(handle, big, dict_view) == GC_LIMIT,
          "dictionary: over 32 KiB is refused");
    check(gc_close(handle, close_view) == GC_OK, "dictionary: close after refusal");

    /* A zlib stream that needs a dictionary reports it, then accepts it. */
    memset(&encoder, 0, sizeof encoder);
    check(deflateInit2(&encoder, 6, Z_DEFLATED, 15, 9, Z_DEFAULT_STRATEGY) == Z_OK,
          "dictionary: zlib encoder init");
    check(deflateSetDictionary(&encoder, dictionary, (uInt)sizeof dictionary - 1) == Z_OK,
          "dictionary: zlib encoder dictionary");
    encoder.next_in = (Bytef *)payload;
    encoder.avail_in = (uInt)sizeof payload - 1;
    encoder.next_out = stream;
    encoder.avail_out = (uInt)sizeof stream;
    check(deflate(&encoder, Z_FINISH) == Z_STREAM_END, "dictionary: zlib encode");
    int64_t zlib_len = (int64_t)encoder.total_out;
    deflateEnd(&encoder);

    /* Without a registered dictionary the stream reports that it needs one.
     * The Goose layer treats that as terminal, so the supported path is to
     * register the dictionary before the first byte. */
    handle = open_format(GC_ZLIB);
    check(feed(handle, stream, zlib_len, output, sizeof output, meta) == GC_DICT_REQUIRED,
          "dictionary: zlib asks for one");
    check(meta[3] == 2, "dictionary: the need-dictionary flag is set");
    check(gc_close(handle, close_view) == GC_OK, "dictionary: close after need-dictionary");

    handle = open_format(GC_ZLIB);
    check(gc_dictionary(handle, dict, dict_view) == GC_OK, "dictionary: zlib registration");
    check(feed(handle, stream, zlib_len, output, sizeof output, meta) == GC_OK,
          "dictionary: zlib decode after registration");
    check(meta[1] == (int64_t)sizeof payload - 1, "dictionary: zlib length");
    check(memcmp(output, payload, sizeof payload - 1) == 0, "dictionary: zlib bytes");
    check(gc_close(handle, close_view) == GC_OK, "dictionary: close zlib");

    /* A wrong dictionary is a mismatch, not a malformed stream. */
    static const uint8_t wrong[] = "a different dictionary of the same shape";
    gc_input wrong_view = {wrong, (int64_t)sizeof wrong - 1};
    handle = open_format(GC_ZLIB);
    check(gc_dictionary(handle, wrong_view, dict_view) == GC_OK,
          "dictionary: wrong registration");
    check(feed(handle, stream, zlib_len, output, sizeof output, meta) == GC_DICT_MISMATCH,
          "dictionary: a wrong dictionary is a mismatch");
    check(gc_close(handle, close_view) == GC_OK, "dictionary: close after mismatch");
}

static void test_reset(void) {
    static const uint8_t payload[] = "member";
    uint8_t stream[128];
    uint8_t output[128];
    int64_t meta[6];
    int64_t reset_meta[1] = {0};
    gc_meta reset_view = {reset_meta, 1};
    int64_t close_meta[1] = {0};
    gc_meta close_view = {close_meta, 1};

    int64_t gzip_len = build_gzip(stream, payload, sizeof payload - 1);
    int64_t handle = open_format(GC_GZIP);
    check(feed(handle, stream, gzip_len, output, sizeof output, meta) == GC_OK &&
          meta[3] == 1, "reset: first member");
    check(gc_reset(handle, reset_view) == GC_OK, "reset: accepted");
    check(feed(handle, stream, gzip_len, output, sizeof output, meta) == GC_OK &&
          meta[3] == 1 && meta[1] == (int64_t)sizeof payload - 1, "reset: second member");
    check(gc_close(handle, close_view) == GC_OK, "reset: close");
    check(gc_reset(handle, reset_view) == GC_INVALID, "reset: a stale handle is rejected");
}

static void test_slots(void) {
    int64_t handles[64];
    int64_t opened = 0;
    for (int i = 0; i < 64; ++i) {
        handles[i] = open_format(GC_RAW);
        if (handles[i] != 0) opened += 1;
    }
    check(opened > 0 && opened <= 32, "slots: the registry is bounded");
    int64_t meta[2] = {0, 0};
    gc_meta view = {meta, 2};
    check(gc_open(GC_RAW, view) == 0 && meta[0] == GC_LIMIT,
          "slots: exhaustion is a limit, not a crash");
    int64_t close_meta[1] = {0};
    gc_meta close_view = {close_meta, 1};
    for (int i = 0; i < 64; ++i) {
        if (handles[i] != 0) check(gc_close(handles[i], close_view) == GC_OK, "slots: close");
    }
    int64_t reused = open_format(GC_RAW);
    check(reused != 0, "slots: a freed slot is reusable");
    check(gc_close(reused, close_view) == GC_OK, "slots: close the reused slot");
}

static void test_abi(void) {
    int64_t meta[3] = {0, 0, 0};
    gc_meta view = {meta, 3};
    check(gc_abi(view) == GC_OK, "abi: reported");
    check(meta[0] >= 10200, "abi: a plausible zlib version");
    check(meta[1] > 0, "abi: a positive z_stream size");
    check(meta[2] == 0, "abi: no decoder is left open");

    uint8_t text[32];
    int64_t version_meta[1] = {0};
    gc_meta version_view = {version_meta, 1};
    gc_output out = {text, (int64_t)sizeof text};
    check(gc_version(out, version_view) == GC_OK, "abi: version string");
    check(version_meta[0] == GC_OK, "abi: version status");
    gc_output tiny = {text, 1};
    check(gc_version(tiny, version_view) == GC_LIMIT, "abi: a short buffer is a limit");
}

int main(int argc, char **argv) {
    if (argc != 2 || strcmp(argv[1], "--selftest") != 0) {
        fprintf(stderr, "usage: %s --selftest\n", argv[0]);
        return 2;
    }
    test_open_and_close();
    test_inflate();
    test_dictionary();
    test_reset();
    test_slots();
    test_abi();
    if (failures) {
        fprintf(stderr, "driver: %d of %d checks failed\n", failures, checks);
        return 1;
    }
    printf("driver ok: %d checks\n", checks);
    return 0;
}
