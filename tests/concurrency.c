/* SPDX-License-Identifier: Apache-2.0 */
/* Independent decoders from several threads at once.
 *
 * The contract says one decoder is not safe to use from two threads, and that
 * separate decoders are independent. This file checks the second half: that
 * independent decoders make progress, that the handle table stays correct
 * under concurrent open/close, that a stale handle is rejected rather than
 * dereferenced while another thread is closing, and that nothing leaks.
 *
 * It also measures the thing the contract implies: N threads decoding N
 * independent streams should take about the same wall time as one thread
 * decoding one, not N times as long. `--throughput` reports that scaling so a
 * lock that serialises every decoder is visible as a number rather than as a
 * suspicion.
 *
 *   concurrency --selftest
 *   concurrency --throughput <threads> <iterations> <input.gz> <output_budget>
 *
 * Built with -DGC_TEST_WRAP_INFLATE -Wl,--wrap=inflate, the binary also holds
 * one real inflate call in flight so that gc_close can be observed waiting for
 * it. The wrap lives in this test binary only; the library is unchanged.
 */
#define _POSIX_C_SOURCE 200809L
#include "compress.h"

#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <zlib.h>

static int failures;
static int checks;

static void check(int condition, const char *name) {
    checks += 1;
    if (!condition) {
        fprintf(stderr, "concurrency: failed: %s\n", name);
        failures += 1;
    }
}

/* A test that cannot start its threads has not tested anything, so it must not
 * report success. Exiting non-zero here is what keeps a failed pthread_create
 * from turning into a barrier that waits forever. */
static void fatal(const char *what, int code) {
    fprintf(stderr, "concurrency: %s failed: %d\n", what, code);
    exit(1);
}

static void must_create(pthread_t *thread, void *(*body)(void *), void *argument) {
    int code = pthread_create(thread, NULL, body, argument);
    if (code) fatal("pthread_create", code);
}

static void must_join(pthread_t thread) {
    int code = pthread_join(thread, NULL);
    if (code) fatal("pthread_join", code);
}

static void must_barrier_init(pthread_barrier_t *barrier, unsigned count) {
    int code = pthread_barrier_init(barrier, NULL, count);
    if (code) fatal("pthread_barrier_init", code);
}

static void must_barrier_wait(pthread_barrier_t *barrier) {
    int code = pthread_barrier_wait(barrier);
    if (code && code != PTHREAD_BARRIER_SERIAL_THREAD) {
        fatal("pthread_barrier_wait", code);
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

static int64_t open_gzip(void) {
    int64_t meta[2] = {0, 0};
    gc_meta view = {meta, 2};
    return gc_open(GC_GZIP, view);
}

static int64_t close_handle(int64_t handle) {
    int64_t meta[1] = {0};
    gc_meta view = {meta, 1};
    return gc_close(handle, view);
}

static int64_t live_decoders(void) {
    int64_t meta[3] = {0, 0, 0};
    gc_meta view = {meta, 3};
    if (gc_abi(view) != GC_OK) return -1;
    return meta[2];
}

/* Decodes one gzip stream on its own decoder and returns the produced length,
 * or -1. Every thread owns its handle; nothing is shared but the table. */
static int64_t decode_once(const uint8_t *stream, int64_t stream_len,
                           uint8_t *output, int64_t output_len) {
    int64_t handle = open_gzip();
    if (!handle) return -1;
    int64_t meta[6];
    gc_input input = {stream, stream_len};
    gc_output out = {output, output_len};
    gc_meta view = {meta, 6};
    int64_t status = gc_inflate(handle, input, out, view);
    int64_t produced = meta[1];
    if (status != GC_OK || meta[3] != 1) produced = -1;
    if (close_handle(handle) != GC_OK) produced = -1;
    return produced;
}

/* ---- selftest ---------------------------------------------------------- */

#define THREADS 8
#define PAYLOAD 4096

static uint8_t g_stream[PAYLOAD + 64];
static int64_t g_stream_len;
static uint8_t g_payload[PAYLOAD];
static pthread_barrier_t g_barrier;
static int g_thread_failures[THREADS];

static void *independent_worker(void *argument) {
    int index = (int)(intptr_t)argument;
    uint8_t output[PAYLOAD + 64];
    must_barrier_wait(&g_barrier);
    for (int round = 0; round < 200; ++round) {
        int64_t produced = decode_once(g_stream, g_stream_len, output, sizeof output);
        if (produced != PAYLOAD || memcmp(output, g_payload, PAYLOAD) != 0) {
            g_thread_failures[index] += 1;
            break;
        }
    }
    return NULL;
}

static void test_independent_decoders(void) {
    pthread_t threads[THREADS];
    must_barrier_init(&g_barrier, THREADS);
    for (int i = 0; i < THREADS; ++i) {
        must_create(&threads[i], independent_worker, (void *)(intptr_t)i);
    }
    for (int i = 0; i < THREADS; ++i) must_join(threads[i]);
    pthread_barrier_destroy(&g_barrier);
    int total = 0;
    for (int i = 0; i < THREADS; ++i) total += g_thread_failures[i];
    check(total == 0, "independent decoders all produced the right bytes");
    check(live_decoders() == 0, "independent decoders left no slot open");
}

/* A handle closed by one thread must be rejected, not dereferenced, when
 * another thread presents it. The generation check is what makes that true. */
static int64_t g_stale_handle;
static int g_stale_status;

static void *stale_worker(void *argument) {
    (void)argument;
    uint8_t output[PAYLOAD + 64];
    int64_t meta[6];
    gc_input input = {g_stream, g_stream_len};
    gc_output out = {output, sizeof output};
    gc_meta view = {meta, 6};
    must_barrier_wait(&g_barrier);
    g_stale_status = (int)gc_inflate(g_stale_handle, input, out, view);
    return NULL;
}

static void test_stale_handle_across_threads(void) {
    g_stale_handle = open_gzip();
    check(g_stale_handle != 0, "stale: opened a handle to close");
    check(close_handle(g_stale_handle) == GC_OK, "stale: closed it");
    pthread_t thread;
    must_barrier_init(&g_barrier, 2);
    must_create(&thread, stale_worker, NULL);
    must_barrier_wait(&g_barrier);
    must_join(thread);
    pthread_barrier_destroy(&g_barrier);
    check(g_stale_status == GC_INVALID,
          "stale: a closed handle is rejected from another thread");
    check(live_decoders() == 0, "stale: no slot was resurrected");
}

/* The table is a fixed resource. A thread that cannot get a slot must be told
 * so with the documented status, and must succeed once a slot is freed. */
static int g_exhaust_status;
static int64_t g_exhaust_handle;

static void *exhaust_worker(void *argument) {
    (void)argument;
    int64_t meta[2] = {0, 0};
    gc_meta view = {meta, 2};
    must_barrier_wait(&g_barrier);
    g_exhaust_handle = gc_open(GC_GZIP, view);
    g_exhaust_status = (int)meta[0];
    return NULL;
}

static void test_exhaustion_across_threads(void) {
    int64_t held[64];
    int opened = 0;
    for (int i = 0; i < 64; ++i) {
        held[i] = open_gzip();
        if (held[i]) opened += 1;
    }
    check(opened == 32, "exhaustion: the table holds exactly 32 decoders");
    pthread_t thread;
    must_barrier_init(&g_barrier, 2);
    must_create(&thread, exhaust_worker, NULL);
    must_barrier_wait(&g_barrier);
    must_join(thread);
    pthread_barrier_destroy(&g_barrier);
    check(g_exhaust_handle == 0 && g_exhaust_status == GC_SYSTEM,
          "exhaustion: a full table is a system error, not a stream limit");
    check(close_handle(held[0]) == GC_OK, "exhaustion: freed one slot");
    held[0] = 0;
    int64_t again = open_gzip();
    check(again != 0, "exhaustion: the freed slot is reusable");
    if (again) check(close_handle(again) == GC_OK, "exhaustion: closed it");
    for (int i = 1; i < 64; ++i) {
        if (held[i]) check(close_handle(held[i]) == GC_OK, "exhaustion: close");
    }
    check(live_decoders() == 0, "exhaustion: every slot was released");
}

/* Open and close churn from several threads at once: the table must stay
 * consistent and end empty. */
static void *churn_worker(void *argument) {
    (void)argument;
    must_barrier_wait(&g_barrier);
    for (int round = 0; round < 500; ++round) {
        int64_t handle = open_gzip();
        if (!handle) continue;
        if (close_handle(handle) != GC_OK) g_thread_failures[0] += 1;
    }
    return NULL;
}

static void test_open_close_churn(void) {
    pthread_t threads[THREADS];
    g_thread_failures[0] = 0;
    must_barrier_init(&g_barrier, THREADS);
    for (int i = 0; i < THREADS; ++i) must_create(&threads[i], churn_worker, NULL);
    for (int i = 0; i < THREADS; ++i) must_join(threads[i]);
    pthread_barrier_destroy(&g_barrier);
    check(g_thread_failures[0] == 0, "churn: every close succeeded");
    check(live_decoders() == 0, "churn: the table ended empty");
}

/* ---- close while a decode is in flight -------------------------------- */

/* A stale handle is rejected after the close has finished. This is the harder
 * case: the close starts while a decode is genuinely inside inflate. The
 * decoder must not be freed under it, the handle must stop resolving at once,
 * and the slot must come back afterwards.
 *
 * The ordering is established by a linker wrap of inflate in this binary, not
 * by sleeping: the wrapped call posts `entered` before it touches the stream,
 * so the reference is provably held, and it waits for `release` so the decode
 * cannot finish early. */
#ifdef GC_TEST_WRAP_INFLATE
#include <semaphore.h>

static sem_t g_inflight_entered;
static sem_t g_inflight_release;
static int g_wrap_armed;

int __real_inflate(z_stream *stream, int flush);

int __wrap_inflate(z_stream *stream, int flush) {
    if (__atomic_exchange_n(&g_wrap_armed, 0, __ATOMIC_SEQ_CST)) {
        sem_post(&g_inflight_entered);
        while (sem_wait(&g_inflight_release) != 0) {
            /* EINTR: keep waiting, the test posts exactly once. */
        }
    }
    return __real_inflate(stream, flush);
}

static void must_sem_init(sem_t *semaphore) {
    if (sem_init(semaphore, 0, 0) != 0) fatal("sem_init", 0);
}

static void must_sem_wait(sem_t *semaphore) {
    while (sem_wait(semaphore) != 0) {
        /* EINTR */
    }
}

static void must_sem_post(sem_t *semaphore) {
    if (sem_post(semaphore) != 0) fatal("sem_post", 0);
}

static int64_t g_race_handle;
static int g_race_inflate_status;
static int64_t g_race_produced;
static int g_race_close_status;
static int g_race_close_done;

static void *race_inflate_worker(void *argument) {
    (void)argument;
    uint8_t output[PAYLOAD + 64];
    int64_t meta[6];
    gc_input input = {g_stream, g_stream_len};
    gc_output out = {output, sizeof output};
    gc_meta view = {meta, 6};
    g_race_inflate_status = (int)gc_inflate(g_race_handle, input, out, view);
    if (g_race_inflate_status == GC_OK && meta[3] == 1 &&
        meta[1] == PAYLOAD && memcmp(output, g_payload, PAYLOAD) == 0) {
        g_race_produced = PAYLOAD;
    } else {
        g_race_produced = -1;
    }
    return NULL;
}

static void *race_close_worker(void *argument) {
    (void)argument;
    g_race_close_status = (int)close_handle(g_race_handle);
    __atomic_store_n(&g_race_close_done, 1, __ATOMIC_SEQ_CST);
    return NULL;
}

static void test_close_waits_for_inflight_decode(void) {
    must_sem_init(&g_inflight_entered);
    must_sem_init(&g_inflight_release);
    g_race_handle = open_gzip();
    check(g_race_handle != 0, "close-race: opened a handle");
    if (!g_race_handle) return;
    g_race_close_done = 0;
    g_race_produced = -1;
    __atomic_store_n(&g_wrap_armed, 1, __ATOMIC_SEQ_CST);

    pthread_t inflater;
    must_create(&inflater, race_inflate_worker, NULL);
    must_sem_wait(&g_inflight_entered);
    check(live_decoders() == 1, "close-race: the decoder is live while in flight");

    pthread_t closer;
    must_create(&closer, race_close_worker, NULL);

    /* gc_close retires the slot before it waits, so an empty table is the
     * observable proof that it has reached the wait. This is a state check,
     * not a duration: it spins until the state changes or gives up. */
    int retired = 0;
    for (int spin = 0; spin < 1000000; ++spin) {
        if (live_decoders() == 0) {
            retired = 1;
            break;
        }
        sched_yield();
    }
    check(retired, "close-race: close retired the slot before waiting");
    if (retired) {
        check(__atomic_load_n(&g_race_close_done, __ATOMIC_SEQ_CST) == 0,
              "close-race: close is still waiting while a reference is held");
        int64_t meta[6];
        uint8_t output[PAYLOAD + 64];
        gc_input input = {g_stream, g_stream_len};
        gc_output out = {output, sizeof output};
        gc_meta view = {meta, 6};
        check(gc_inflate(g_race_handle, input, out, view) == GC_INVALID,
              "close-race: a retired handle is refused while close waits");
        int64_t reset_meta[1] = {0};
        gc_meta reset_view = {reset_meta, 1};
        check(gc_reset(g_race_handle, reset_view) == GC_INVALID,
              "close-race: reset on a retired handle is refused");
    }

    must_sem_post(&g_inflight_release);
    must_join(inflater);
    must_join(closer);

    check(g_race_produced == PAYLOAD,
          "close-race: the in-flight decode finished with the right bytes");
    check(g_race_close_status == GC_OK, "close-race: close then succeeded");
    check(__atomic_load_n(&g_race_close_done, __ATOMIC_SEQ_CST) == 1,
          "close-race: close returned once the reference was released");
    check(live_decoders() == 0, "close-race: the table is empty");

    /* The slot must be usable again after the deferred free. */
    int64_t again = open_gzip();
    check(again != 0, "close-race: the slot is reusable");
    if (again) check(close_handle(again) == GC_OK, "close-race: closed the reused slot");
    check(live_decoders() == 0, "close-race: the reused slot was released");

    sem_destroy(&g_inflight_entered);
    sem_destroy(&g_inflight_release);
}
#else
static void test_close_waits_for_inflight_decode(void) {
    fprintf(stderr,
            "concurrency: close-race not built (needs -DGC_TEST_WRAP_INFLATE "
            "-Wl,--wrap=inflate)\n");
    failures += 1;
    checks += 1;
}
#endif

static int selftest(void) {
    for (int i = 0; i < PAYLOAD; ++i) g_payload[i] = (uint8_t)(i * 31 + 7);
    g_stream_len = build_gzip(g_stream, g_payload, PAYLOAD);
    test_independent_decoders();
    test_stale_handle_across_threads();
    test_exhaustion_across_threads();
    test_open_close_churn();
    test_close_waits_for_inflight_decode();
    if (failures) {
        fprintf(stderr, "concurrency: %d of %d checks failed\n", failures, checks);
        return 1;
    }
    printf("concurrency ok: %d checks\n", checks);
    return 0;
}

/* ---- throughput -------------------------------------------------------- */

static uint8_t *g_input;
static int64_t g_input_len;
static long g_iterations;
static int64_t g_output_len;
static pthread_barrier_t g_start;

static void *throughput_worker(void *argument) {
    (void)argument;
    /* Each thread owns its output buffer, so the measurement includes the
     * decode and nothing shared. */
    uint8_t *output = malloc((size_t)g_output_len);
    if (!output) {
        g_thread_failures[0] += 1;
        return NULL;
    }
    must_barrier_wait(&g_start);
    for (long i = 0; i < g_iterations; ++i) {
        if (decode_once(g_input, g_input_len, output, g_output_len) < 0) {
            g_thread_failures[0] += 1;
            break;
        }
    }
    free(output);
    return NULL;
}

static int64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static int throughput(int threads, long iterations, const char *path,
                      int64_t output_len) {
    FILE *file = fopen(path, "rb");
    if (!file) {
        fprintf(stderr, "concurrency: cannot read %s\n", path);
        return 1;
    }
    fseek(file, 0, SEEK_END);
    long size = ftell(file);
    fseek(file, 0, SEEK_SET);
    g_input = malloc((size_t)size + 1);
    if (!g_input || fread(g_input, 1, (size_t)size, file) != (size_t)size) {
        fprintf(stderr, "concurrency: cannot read %s\n", path);
        fclose(file);
        return 1;
    }
    fclose(file);
    g_input_len = size;
    g_iterations = iterations;
    g_output_len = output_len;
    g_thread_failures[0] = 0;

    pthread_t *workers = calloc((size_t)threads, sizeof *workers);
    if (!workers) return 1;
    must_barrier_init(&g_start, (unsigned)threads);
    int64_t start = now_ns();
    for (int i = 0; i < threads; ++i) {
        must_create(&workers[i], throughput_worker, NULL);
    }
    for (int i = 0; i < threads; ++i) must_join(workers[i]);
    int64_t elapsed = now_ns() - start;
    pthread_barrier_destroy(&g_start);
    free(workers);

    if (g_thread_failures[0]) {
        fprintf(stderr, "concurrency: a worker failed\n");
        free(g_input);
        return 1;
    }
    long total = threads * iterations;
    printf("threads=%d iterations=%ld decodes=%ld total_ns=%lld "
           "per_decode_ns=%lld decodes_per_second=%.1f live=%lld\n",
           threads, iterations, total, (long long)elapsed,
           (long long)(elapsed / total),
           (double)total * 1e9 / (double)elapsed,
           (long long)live_decoders());
    free(g_input);
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--selftest") == 0) return selftest();
    if (argc == 6 && strcmp(argv[1], "--throughput") == 0) {
        int threads = atoi(argv[2]);
        long iterations = strtol(argv[3], NULL, 10);
        int64_t output_len = strtoll(argv[5], NULL, 10);
        if (threads < 1 || threads > 64 || iterations < 1 || output_len < 1) {
            fprintf(stderr, "concurrency: bad thread, iteration or budget\n");
            return 2;
        }
        return throughput(threads, iterations, argv[4], output_len);
    }
    fprintf(stderr, "usage: %s --selftest\n"
                    "       %s --throughput <threads> <iterations> <input.gz> "
                    "<output_budget>\n", argv[0], argv[0]);
    return 2;
}
