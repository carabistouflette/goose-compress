/* SPDX-License-Identifier: Apache-2.0 */
/* The libFuzzer driver for tests/fuzz.goose.
 *
 * The target itself is Goose: `fuzz_decode` calls the public API, so the
 * state machine, the budgets and the native adapter all run under the
 * sanitizers. This file only adapts libFuzzer to it.
 *
 * Goose reports a failed assertion or a bounds violation with `_Exit`, which
 * would end the process without libFuzzer seeing a crash and without the
 * triggering input being saved. The Goose translation unit is therefore
 * compiled with `-D_Exit=goose_fuzz_exit`, and the function below turns a
 * non-zero status into `abort()` so libFuzzer retains the input. The driver
 * self-tests that path before a campaign runs; see scripts/fuzz.py.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "compress-fuzz.h"

/* Remapped only in the generated Goose C, never in this file or the runtime. */
_Noreturn void goose_fuzz_exit(int status) {
    if (status != 0) abort();
    _Exit(status);
}

int LLVMFuzzerInitialize(int *argc, char ***argv) {
    goose_init(*argc, *argv);
    atexit(goose_exit);
    return 0;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    if (size > (size_t)INT64_MAX) return 0;
#ifdef GOOSE_FUZZ_SELF_TEST
    /* A forced Goose assertion, so the campaign can prove that an assertion
     * failure becomes a libFuzzer crash with the exact bytes retained. */
    static const uint8_t probe[] = "libFuzzer Goose assertion bridge probe";
    if (size == sizeof(probe) - 1 && memcmp(data, probe, size) == 0)
        fuzz_assertion_probe(0);
#endif
    (void)fuzz_decode((sl_u8){(uint8_t *)data, (int64_t)size});
    return 0;
}
