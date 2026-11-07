#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Proves the sanitizer runs are instrumented, not merely debug builds.

`goose build --cc clang --debug` does not put the sanitizer flags on the
compiler that builds the Goose translation unit, so a green run from that path
proves nothing about memory safety. This script checks the two things that
make `make sanitize` meaningful:

1. the Goose translation unit emitted by `--standalone` references ASan and
   UBSan handlers, so the generated code is instrumented;
2. a deliberately injected one-byte heap overflow in the native adapter is
   caught and aborts the process, so the instrumentation is live rather than
   linked and unused.

    python3 scripts/sanitize_negative.py --goose /path/to/goose --cc clang

Both checks build into `.build/negative/` and leave the repository untouched.
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / ".build" / "negative"
WRAPPER = ROOT / "scripts" / "sanitizer-cc.sh"
NATIVE = ROOT / "native" / "compress.c"
DRIVER = ROOT / "tests" / "driver.c"
LIBS = ["-lz", "-lpthread", "-lm", "-ldl"]

SANITIZER_ENV = {
    "ASAN_OPTIONS": "detect_leaks=1:abort_on_error=1",
    "UBSAN_OPTIONS": "halt_on_error=1",
}


def run(command, env=None, cwd=None):
    merged = None
    if env:
        import os
        merged = dict(os.environ)
        merged.update(env)
    return subprocess.run(command, capture_output=True, text=True, check=False,
                          env=merged, cwd=cwd)


def compile_with_wrapper(cc, arguments):
    return run([str(WRAPPER)] + arguments, env={"SANITIZER_CC": cc})


def check_goose_translation_unit(goose, cc):
    """The generated C must reference sanitizer handlers."""
    source = WORK / "spec-standalone.c"
    result = run([str(goose), str(ROOT / "tests" / "spec.goose"),
                  "--standalone", "--debug", "--no-bce", "-o", str(source)])
    if result.returncode != 0:
        return f"goose --standalone failed: {result.stderr.strip()}"

    object_file = WORK / "spec-standalone.o"
    result = compile_with_wrapper(cc, ["-O1", "-g", "-Inative", "-c",
                                       "-o", str(object_file), str(source)])
    if result.returncode != 0:
        return f"compiling the standalone C failed: {result.stderr.strip()}"

    symbols = run(["nm", "-u", str(object_file)])
    if symbols.returncode != 0:
        return f"nm failed: {symbols.stderr.strip()}"
    handlers = set(re.findall(r"__(?:asan|ubsan)_[a-z0-9_]+", symbols.stdout))
    if not handlers:
        return ("the Goose translation unit references no sanitizer handler: "
                "the flags never reached the compiler that built it")
    required = {"__asan_report_store_n", "__ubsan_handle_out_of_bounds_abort"}
    missing = required - handlers
    if missing:
        return f"the Goose translation unit lacks {sorted(missing)}"
    print(f"  goose translation unit: {len(handlers)} sanitizer handlers referenced")
    return None


def check_injected_overflow(cc):
    """A deliberate heap overflow in the adapter must abort the process."""
    broken = WORK / "compress-broken.c"
    shutil.copyfile(NATIVE, broken)
    text = broken.read_text()
    marker = ("int64_t gc_inflate(int64_t handle, gc_input input, "
              "gc_output output, gc_meta meta) {")
    if marker not in text:
        return "cannot find gc_inflate in native/compress.c to inject into"
    injection = ("\n    { static int once = 0; if (!once) { once = 1; "
                 "char *p = (char *)malloc(4); p[4] = 1; free(p); } }")
    broken.write_text(text.replace(marker, marker + injection, 1))

    binary = WORK / "driver-broken"
    result = compile_with_wrapper(cc, ["-O1", "-g", "-Inative", "-o", str(binary),
                                       str(DRIVER), str(broken)] + LIBS)
    if result.returncode != 0:
        return f"compiling the injected adapter failed: {result.stderr.strip()}"

    result = run([str(binary), "--selftest"], env=SANITIZER_ENV)
    if result.returncode == 0:
        return ("the injected one-byte heap overflow was not caught: "
                "the adapter is not instrumented")
    output = result.stdout + result.stderr
    if "Sanitizer" not in output and "runtime error" not in output:
        return f"the run failed without a sanitizer report: {output.strip()[:400]}"
    first = next((line for line in output.splitlines()
                  if "runtime error" in line or "ERROR: AddressSanitizer" in line), "")
    print(f"  injected overflow: caught, exit {result.returncode}")
    print(f"  {first.strip()}")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--cc", default="clang")
    arguments = parser.parse_args()

    goose = pathlib.Path(arguments.goose).resolve()
    if not goose.is_file():
        raise SystemExit(f"no goose binary at {goose}")
    if not WRAPPER.is_file():
        raise SystemExit(f"no sanitizer wrapper at {WRAPPER}")

    WORK.mkdir(parents=True, exist_ok=True)
    failures = []
    for name, check in (("goose translation unit", lambda: check_goose_translation_unit(goose, arguments.cc)),
                        ("injected overflow", lambda: check_injected_overflow(arguments.cc))):
        print(f"checking {name} with {arguments.cc}")
        problem = check()
        if problem:
            failures.append(f"{name}: {problem}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        raise SystemExit(f"sanitizer negative control failed: {len(failures)} checks")

    print(f"sanitizer negative control ok: instrumentation is live ({arguments.cc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
