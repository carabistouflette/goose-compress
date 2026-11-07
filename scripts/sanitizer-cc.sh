#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# A compiler wrapper that instruments everything it is given.
#
# `goose build --cc clang --debug` does not instrument the generated code: the
# sanitizer flags never reach the compiler that builds the Goose translation
# unit. This wrapper is the supported way to get real instrumentation, either
# through `goose build --cc scripts/sanitizer-cc.sh` or by compiling the
# `--standalone` output directly.
#
#   SANITIZER_CC=clang scripts/sanitizer-cc.sh -O1 -g -c file.c
#
# -fno-sanitize-recover=all turns every UBSan finding into a failure instead of
# a warning, so a run either passes or stops.
set -euo pipefail
exec "${SANITIZER_CC:-clang}" "$@" \
    -fsanitize=address,undefined \
    -fno-sanitize-recover=all \
    -fno-omit-frame-pointer
