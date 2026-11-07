#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Builds and runs a bounded libFuzzer campaign over the real Goose API.

The target is `tests/fuzz.goose`, which calls `open_with_dictionary`, `feed`,
`finish` and `close`. The native adapter is compiled into the same binary, so
ASan and UBSan see the Goose translation unit and `native/compress.c` together.

Three things are proved before a campaign is allowed to count:

1. **The Goose translation unit is instrumented.** `nm -u` on its object must
   reference ASan and UBSan handlers. A debug build references none.
2. **A Goose assertion failure reaches libFuzzer.** Goose reports a failed
   assertion with `_Exit`; the translation unit is compiled with
   `-D_Exit=goose_fuzz_exit`, which aborts instead. A self-test feeds a probe
   input that forces an assertion and requires a crash artifact containing
   exactly those bytes.
3. **The sanitizer is live.** A one-byte heap overflow is injected into a copy
   of `native/compress.c`, the fuzzer is rebuilt against it, and the run must
   abort with a sanitizer report.

A campaign is only recorded when it reached both the requested execution count
and the requested duration; if the first attempt is too short it is extended
rather than reported as met.

    python3 scripts/fuzz.py --goose /path/to/goose --seconds 120 --runs 100000
    python3 scripts/fuzz.py --goose /path/to/goose --skip-campaign   # build only
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / ".build" / "fuzz"
NATIVE = ROOT / "native" / "compress.c"
DRIVER = ROOT / "tests" / "fuzz.c"
TARGET = ROOT / "tests" / "fuzz.goose"
FIXTURES = ROOT / ".build" / "fixtures"

SANITIZE = ["-fsanitize=fuzzer-no-link,address,undefined",
            "-fno-sanitize-recover=all", "-fno-omit-frame-pointer"]
LINK = ["-fsanitize=fuzzer,address,undefined",
        "-fno-sanitize-recover=all", "-fno-omit-frame-pointer"]
LIBS = ["-lz", "-lpthread", "-lm", "-ldl"]
MAX_LEN = 1024
TIMEOUT = 5
RSS_LIMIT_MB = 1024


def child_env(extra=None):
    """The pinned compiler and the gcc sanitizer runtime need the system
    libstdc++, which the harness LD_LIBRARY_PATH shadows."""
    environment = dict(os.environ)
    environment.pop("LD_LIBRARY_PATH", None)
    if extra:
        environment.update(extra)
    return environment


def run(command, env=None, cwd=ROOT, timeout=None):
    return subprocess.run([str(part) for part in command], cwd=cwd,
                          capture_output=True, text=True, check=False,
                          env=env or child_env(), timeout=timeout)


def must(command, env=None, timeout=None):
    result = run(command, env=env, timeout=timeout)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): "
                         f"{' '.join(str(p) for p in command)}\n"
                         f"{result.stdout}{result.stderr}")
    return result


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def emit_standalone(goose, work):
    """Emits the Goose translation unit and its header once."""
    source = work / "compress-fuzz.c"
    header = work / "compress-fuzz.h"
    must([goose, TARGET, "--standalone", "--debug", "--no-bce",
          "--header", header, "-o", source])
    return source, header


def build(goose, cc, work, tag, native_source, self_test=False):
    """Compiles one fuzzer binary. Returns its path."""
    source, header = emit_standalone(goose, work)
    goose_object = work / f"goose-{tag}.o"
    native_object = work / f"native-{tag}.o"
    driver_object = work / f"driver-{tag}.o"
    binary = work / f"fuzzer-{tag}"

    # The _Exit remap applies to the generated Goose C only.
    must([cc, *SANITIZE, "-O1", "-g", "-D_Exit=goose_fuzz_exit", "-c",
          source, "-o", goose_object])
    must([cc, *SANITIZE, "-O1", "-g", "-Inative", "-c", native_source,
          "-o", native_object])
    driver_flags = ["-DGOOSE_FUZZ_SELF_TEST=1"] if self_test else []
    must([cc, *SANITIZE, "-O1", "-g", "-I" + str(work), *driver_flags, "-c",
          DRIVER, "-o", driver_object])
    must([cc, *LINK, "-O1", "-g", goose_object, native_object, driver_object,
          *LIBS, "-o", binary])
    return binary, goose_object


def check_instrumented(goose_object):
    """The Goose translation unit must reference sanitizer handlers."""
    result = run(["nm", "-u", goose_object])
    if result.returncode != 0:
        raise SystemExit(f"nm failed on {goose_object}: {result.stderr}")
    handlers = set(re.findall(r"__(?:asan|ubsan)_[a-z0-9_]+", result.stdout))
    required = {"__asan_report_store_n", "__ubsan_handle_out_of_bounds_abort"}
    missing = required - handlers
    if missing:
        raise SystemExit(f"the Goose translation unit lacks {sorted(missing)}: "
                         "the sanitizer flags never reached its compiler")
    return sorted(handlers)


def seed_corpus(directory):
    """Deterministic seeds built from the specification fixtures.

    The fuzz input layout is the one tests/fuzz.goose documents: eight control
    bytes, then an optional dictionary, then the stream. Seeding from the
    fixtures means the campaign starts from valid gzip, zlib and raw streams
    instead of rediscovering the header format.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.iterdir():
        if stale.is_file():
            stale.unlink()
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    written = []
    for fixture in manifest["fixtures"]:
        name = fixture["name"]
        stream = (FIXTURES / f"{name}.in").read_bytes()
        dictionary = b""
        if fixture["dictionary"]:
            dictionary = (FIXTURES / f"{name}.dict").read_bytes()
        trailing = fixture["trailing"]
        members = fixture["limits"]["members"]
        control = bytearray(8)
        control[0] = fixture["format"] % 3
        control[1] = trailing % 3
        control[2] = (members - 1) % 4 if trailing == 2 else 0
        # Selector 7 is a 1 MiB output budget, so a valid stream can finish.
        control[3] = 7
        # Selector 6 is exactly the input length.
        control[4] = 6
        control[5] = 1 if dictionary else 0
        control[6] = 0
        control[7] = 0
        data = bytes(control) + dictionary + stream
        path = directory / hashlib.sha256(data).hexdigest()
        path.write_bytes(data)
        written.append(name)
    # A few degenerate shapes the fixtures do not cover.
    for extra in (b"", b"\x00", b"\x00" * 8, b"\x02\x02\x03\x00\x00\x00\x00\x00",
                  b"\x00\x00\x00\x07\x06\x00\x00\x00" + b"\x1f\x8b"):
        path = directory / hashlib.sha256(extra).hexdigest()
        path.write_bytes(extra)
    return written


def self_test(binary, work):
    """A forced Goose assertion must become a libFuzzer crash with the bytes."""
    probe = work / "self-test-seeds"
    probe.mkdir(exist_ok=True)
    for stale in probe.iterdir():
        if stale.is_file():
            stale.unlink()
    expected = b"libFuzzer Goose assertion bridge probe"
    (probe / "input").write_bytes(expected)
    crash = work / "self-test-crash"
    crash.unlink(missing_ok=True)
    log = work / "self-test.log"
    with log.open("wb") as handle:
        result = subprocess.run(
            [str(binary), str(probe), "-runs=10", "-seed=1",
             "-exact_artifact_path=" + str(crash)],
            cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
            timeout=60, env=child_env())
    text = log.read_text(errors="replace")
    if result.returncode == 0:
        raise SystemExit("the forced Goose assertion did not crash; see " + str(log))
    if not crash.is_file():
        raise SystemExit("no crash artifact was written; see " + str(log))
    if crash.read_bytes() != expected:
        raise SystemExit("the crash artifact does not hold the triggering input")
    if "assert failed" not in text:
        raise SystemExit("the crash was not a Goose assertion; see " + str(log))
    return {"artifact_sha256": sha256_file(crash), "artifact_bytes": len(expected)}


def negative_sanitizer(goose, cc, work):
    """A one-byte heap overflow in the adapter must abort the fuzzer."""
    broken = work / "compress-broken.c"
    shutil.copyfile(NATIVE, broken)
    text = broken.read_text()
    marker = "int64_t gc_open(int64_t format, gc_meta meta) {"
    if marker not in text:
        raise SystemExit("cannot find gc_open to inject into")
    injection = ("\n    { static int once = 0; if (!once) { once = 1; "
                 "char *p = (char *)malloc(4); p[4] = 1; free(p); } }")
    broken.write_text(text.replace(marker, marker + injection, 1))

    binary, _ = build(goose, cc, work, "broken", broken)
    seeds = work / "negative-seeds"
    seeds.mkdir(exist_ok=True)
    for stale in seeds.iterdir():
        if stale.is_file():
            stale.unlink()
    (seeds / "input").write_bytes(b"\x00" * 16)
    crash = work / "negative-crash"
    crash.unlink(missing_ok=True)
    log = work / "negative.log"
    with log.open("wb") as handle:
        result = subprocess.run(
            [str(binary), str(seeds), "-runs=200", "-seed=1",
             "-timeout=" + str(TIMEOUT), "-rss_limit_mb=" + str(RSS_LIMIT_MB),
             "-exact_artifact_path=" + str(crash)],
            cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
            timeout=120, env=child_env())
    text = log.read_text(errors="replace")
    if result.returncode == 0:
        raise SystemExit("the injected heap overflow was not caught; see " + str(log))
    if "AddressSanitizer" not in text and "runtime error" not in text:
        raise SystemExit("the run failed without a sanitizer report; see " + str(log))
    first = next((line for line in text.splitlines()
                  if "ERROR: AddressSanitizer" in line or "runtime error" in line), "")
    return {"exit": result.returncode, "report": first.strip(),
            "artifact": str(crash.relative_to(ROOT)) if crash.is_file() else None,
            "artifact_sha256": sha256_file(crash) if crash.is_file() else None}


def parse_stats(text):
    """Extracts the execution count, coverage and peak RSS from a run log."""
    stats = {}
    for line in text.splitlines():
        if line.startswith("stat::"):
            key, _, value = line.partition(":")
            stats[key.strip()] = value.strip()
    done = re.search(r"Done (\d+) runs? in (\d+) second", text)
    if done:
        stats["runs"] = done.group(1)
        stats["seconds"] = done.group(2)
    coverage = re.findall(r"cov: (\d+) ft: (\d+)", text)
    if coverage:
        stats["cov"] = coverage[-1][0]
        stats["ft"] = coverage[-1][1]
    # This libFuzzer build prints no stat:: lines, so peak RSS and the corpus
    # size come from the last progress line instead.
    rss = re.findall(r"rss: (\d+)Mb", text)
    if rss:
        stats["peak_rss_mb"] = rss[-1]
    corpus = re.findall(r"corp: (\d+)/(\d+)b", text)
    if corpus:
        stats["corpus_units"] = corpus[-1][0]
        stats["corpus_bytes"] = corpus[-1][1]
    return stats


def campaign(binary, work, seed, seconds, runs, label):
    """Runs one campaign and extends it until both gates are met."""
    corpus = work / "corpus"
    corpus.mkdir(exist_ok=True)
    seeds = work / "seeds"
    artifacts = work / "artifacts"
    artifacts.mkdir(exist_ok=True)
    attempt = 0
    budget = seconds
    while True:
        attempt += 1
        log = work / f"run-{label}-attempt{attempt}.log"
        command = [str(binary), str(corpus), str(seeds),
                   "-max_len=" + str(MAX_LEN),
                   "-max_total_time=" + str(budget),
                   "-runs=" + str(max(runs * 100, 100000000)),
                   "-seed=" + str(seed),
                   "-timeout=" + str(TIMEOUT),
                   "-rss_limit_mb=" + str(RSS_LIMIT_MB),
                   "-artifact_prefix=" + str(artifacts) + "/"]
        with log.open("wb") as handle:
            result = subprocess.run(command, cwd=ROOT, stdout=handle,
                                    stderr=subprocess.STDOUT,
                                    timeout=budget + 300, env=child_env())
        text = log.read_text(errors="replace")
        if result.returncode != 0:
            print(text[-8000:], file=sys.stderr)
            raise SystemExit(f"campaign {label} crashed; artifacts in {artifacts}")
        stats = parse_stats(text)
        executed = int(stats.get("runs", 0))
        elapsed = int(stats.get("seconds", 0))
        print(f"  {label} attempt {attempt}: {executed} runs in {elapsed}s "
              f"(cov {stats.get('cov', '?')}, ft {stats.get('ft', '?')})", flush=True)
        if executed >= runs and elapsed >= seconds:
            return {
                "label": label,
                "seed": seed,
                "attempts": attempt,
                "runs": executed,
                "seconds": elapsed,
                "cov": stats.get("cov"),
                "ft": stats.get("ft"),
                "peak_rss_mb": stats.get("peak_rss_mb"),
                "corpus_units": stats.get("corpus_units"),
                "corpus_bytes": stats.get("corpus_bytes"),
                "log": str(log.relative_to(ROOT)),
                "command": " ".join(str(part) for part in command),
            }
        if attempt >= 3:
            raise SystemExit(f"campaign {label} could not reach {runs} runs in "
                             f">= {seconds}s after {attempt} attempts")
        # Extend proportionally, with headroom for a slower corpus.
        budget = max(budget * 2, int(seconds * runs / max(executed, 1) * 1.5))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--runs", type=int, default=100000)
    parser.add_argument("--seeds", default="20261107,20261108")
    parser.add_argument("--skip-campaign", action="store_true")
    arguments = parser.parse_args()

    if arguments.seconds < 1 or arguments.runs < 1:
        parser.error("--seconds and --runs must be positive")
    seeds = [int(value) for value in arguments.seeds.split(",") if value.strip()]
    if len(seeds) < 2:
        parser.error("at least two distinct seeds are required")

    goose = pathlib.Path(arguments.goose)
    if not goose.is_absolute():
        goose = (ROOT / goose).resolve()
    if not goose.is_file():
        raise SystemExit(f"no goose binary at {goose}")
    if not FIXTURES.is_dir():
        raise SystemExit("run `make fixtures` first")

    WORK.mkdir(parents=True, exist_ok=True)
    report = {
        "target": "tests/fuzz.goose",
        "api": "compress::open_with_dictionary / feed / finish / close",
        "compiler": {"sha256": sha256_file(goose),
                     "version": run([goose, "--version"]).stdout.strip()},
        "cc": run([arguments.cc, "--version"]).stdout.splitlines()[0].strip(),
        "sanitizers": "address,undefined (-fno-sanitize-recover=all)",
        "limits": {"max_len": MAX_LEN, "timeout_s": TIMEOUT,
                   "rss_limit_mb": RSS_LIMIT_MB},
        "requested": {"runs": arguments.runs, "seconds": arguments.seconds,
                      "seeds": seeds},
    }

    print("building the instrumented target", flush=True)
    binary, goose_object = build(goose, arguments.cc, WORK, "main", NATIVE)
    report["instrumentation"] = check_instrumented(goose_object)
    print(f"  Goose translation unit: {len(report['instrumentation'])} "
          f"sanitizer handlers referenced", flush=True)

    print("seeding the corpus from the fixtures", flush=True)
    report["seeded_fixtures"] = len(seed_corpus(WORK / "seeds"))
    report["seed_corpus_sha256"] = hashlib.sha256(
        b"".join(sorted(path.read_bytes() for path in (WORK / "seeds").iterdir()
                        if path.is_file()))).hexdigest()

    print("proving the Goose assertion bridge", flush=True)
    self_binary, _ = build(goose, arguments.cc, WORK, "self-test", NATIVE,
                           self_test=True)
    report["assertion_bridge"] = self_test(self_binary, WORK)
    print(f"  forced assertion retained: artifact "
          f"{report['assertion_bridge']['artifact_sha256'][:16]}", flush=True)

    print("proving the sanitizer is live", flush=True)
    report["sanitizer_negative"] = negative_sanitizer(goose, arguments.cc, WORK)
    print(f"  injected overflow: {report['sanitizer_negative']['report'][:90]}",
          flush=True)

    report["fuzzer_sha256"] = sha256_file(binary)
    report["standalone_c_sha256"] = sha256_file(WORK / "compress-fuzz.c")

    if arguments.skip_campaign:
        report["campaigns"] = []
        report["note"] = "build and negative controls only; no campaign was run"
    else:
        print("running the campaigns", flush=True)
        report["campaigns"] = []
        for index, seed in enumerate(seeds):
            report["campaigns"].append(
                campaign(binary, WORK, seed, arguments.seconds, arguments.runs,
                         f"seed{index + 1}-{seed}"))
        corpus = WORK / "corpus"
        files = sorted(path for path in corpus.iterdir() if path.is_file())
        report["corpus"] = {
            "units": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "sha256": hashlib.sha256(
                b"".join(path.read_bytes() for path in files)).hexdigest(),
        }

    (WORK / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote {WORK / 'report.json'}")
    for entry in report["campaigns"]:
        print(f"  {entry['label']}: {entry['runs']} runs in {entry['seconds']}s, "
              f"cov {entry['cov']}, ft {entry['ft']}, "
              f"peak RSS {entry['peak_rss_mb']} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
