#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The serialised measurement runner.

`bench/measure.py` answers "how fast is one call". This answers "is the
difference real": it keeps every raw sample, alternates the harness order so
slow drift lands on all rows instead of one, reports dispersion and tails
rather than a single median, and records provenance before and after the run
so a machine that changed underneath the measurement is visible.

Nothing here runs unless it is asked to. `--plan` prints the exact commands,
the input set and the duration budget without executing anything, and
`--build-only` prepares the inputs and binaries without timing.

    python3 bench/runner.py --plan
    python3 bench/runner.py --build-only
    python3 bench/runner.py --samples 20 --output .build/benchmark.json

Run it alone, on an idle machine. It refuses to start when the load average
says something else is using the CPU.
"""

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shutil
import statistics
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
INPUTS = BUILD / "bench-inputs"

sys.path.insert(0, str(ROOT / "bench"))
import measure  # noqa: E402  (prepare, provenance, fnv1a, SIZES, ITERATIONS)

# Every row states its contract. A row whose contract is looser than the
# library's is marked `comparable: false` and is never used to rank the
# library against a competitor.
HARNESSES = [
    {
        "label": "goose-compress oneshot",
        "binary": "bench",
        "mode": "oneshot",
        "contract": "bounded: output budget, input budget, 1 member, trailing rejected",
        "buffer": "reused across calls",
        "comparable": True,
        "role": "the library, one call",
    },
    {
        "label": "goose-compress stream",
        "binary": "bench",
        "mode": "stream",
        "contract": "bounded: same limits, 64 KiB steps",
        "buffer": "reused across calls",
        "comparable": True,
        "role": "the library, incremental",
    },
    {
        "label": "goose-compress fresh",
        "binary": "bench",
        "mode": "fresh",
        "contract": "bounded: same limits, 64 KiB steps",
        "buffer": "allocated inside every call",
        "comparable": True,
        "role": "the library, caller without a buffer",
    },
    {
        "label": "goose-compress tail",
        "binary": "bench",
        "mode": "tail",
        "contract": "bounded: finish + close after the last byte",
        "buffer": "reused across calls",
        "comparable": True,
        "role": "end-of-stream consumer cost",
    },
    {
        "label": "zlib bounded (same contract)",
        "binary": "zlib-bounded",
        "mode": None,
        "contract": "bounded: output budget, input budget, 1 member, trailing rejected",
        "buffer": "reused across calls",
        "comparable": True,
        "role": "the same zlib under the same framing policy: overhead, not a ranking",
    },
    {
        "label": "zlib oneshot (no bounds)",
        "binary": "zlib-oneshot",
        "mode": None,
        "contract": "unbounded: no output budget, no input budget, no trailing check",
        "buffer": "reused across calls",
        "comparable": False,
        "role": "overhead baseline only; looser contract, never a ranking",
    },
    {
        "label": "libdeflate oneshot (no bounds)",
        "binary": "libdeflate-oneshot",
        "mode": None,
        "contract": "unbounded: no output budget, no input budget, no trailing check",
        "buffer": "reused across calls",
        "comparable": False,
        "role": "throughput reference only; one-shot, so it does not rank streaming",
    },
]

BINARIES = ["bench", "zlib-bounded", "zlib-oneshot", "libdeflate-oneshot"]
INVALID_HARNESS = {
    "label": "goose-compress invalid",
    "binary": "bench",
    "mode": "invalid",
    "contract": "bounded: rejects a truncated stream or bad gzip checksum",
    "buffer": "reused across calls",
    "comparable": False,
    "role": "error-path timing only; no equal-contract competitor row",
}

SOURCE_FILES = [
    "GOOSE_REVISION",
    "compress.goose",
    "compress.build",
    "native/compress.c",
    "native/compress.h",
    "bench/bench.goose",
    "bench/runner.py",
    "bench/measure.py",
    "bench/compare/zlib_bounded.c",
    "bench/compare/zlib_oneshot.c",
    "bench/compare/libdeflate_oneshot.c",
]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def command_info(command):
    """Resolve and identify a compiler executable without shell expansion."""
    resolved = shutil.which(command)
    if resolved is None:
        candidate = pathlib.Path(command)
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        resolved = str(candidate.resolve())
    path = pathlib.Path(resolved).resolve()
    if not path.is_file():
        raise SystemExit("compiler binary is missing: " + str(path))
    version = subprocess.run([str(path), "--version"], capture_output=True,
                             text=True, check=False).stdout.splitlines()
    if not version:
        raise SystemExit("compiler version is unavailable: " + str(path))
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "version": version[0].strip() if version else "unknown",
    }


def linked_dependencies(binary):
    """Record resolved shared-library paths and bytes, not ASLR addresses."""
    try:
        result = subprocess.run(["ldd", str(binary)], capture_output=True,
                                text=True, check=False)
    except OSError as error:
        return {"error": str(error)}
    if result.returncode != 0:
        return {"error": result.stderr.strip() or "ldd failed"}

    dependencies = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if "=>" in fields:
            arrow = fields.index("=>")
            name = " ".join(fields[:arrow])
            if arrow + 1 >= len(fields) or fields[arrow + 1] == "not":
                dependencies[name] = {"path": None, "sha256": None}
                continue
            resolved = fields[arrow + 1]
        elif fields[0].startswith("/"):
            resolved = fields[0]
            name = pathlib.Path(resolved).name
        else:
            continue
        path = pathlib.Path(resolved).resolve()
        dependencies[name] = {
            "path": str(path),
            "sha256": sha256_file(path) if path.is_file() else None,
        }
    return dependencies


def load_average():
    try:
        return os.getloadavg()
    except OSError:
        return (0.0, 0.0, 0.0)


def cpu_governor():
    path = pathlib.Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    try:
        return path.read_text().strip()
    except OSError:
        return "unknown"


def snapshot(goose_path, cc, affinity_allowed, affinity_cpu):
    """Everything that could change between the first and the last sample."""
    goose_binary = pathlib.Path(goose_path).resolve()
    cc_info = command_info(cc)
    source_hashes = {
        name: sha256_file(ROOT / name)
        for name in SOURCE_FILES if (ROOT / name).is_file()
    }
    return {
        "load_average": list(load_average()),
        "cpu_governor": cpu_governor(),
        "cpu_affinity_allowed_before_pin": affinity_allowed,
        "cpu_affinity_selected": affinity_cpu,
        "goose_compiler": {
            "path": str(goose_binary),
            "sha256": sha256_file(goose_binary) if goose_binary.is_file() else None,
            "revision": (ROOT / "GOOSE_REVISION").read_text().strip(),
        },
        "c_compiler": cc_info,
        "build_flags": {
            "goose": ["build", "bench/bench.goose", "--cc", cc, "-O2"],
            "c": ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O2"],
        },
        "source_sha256": source_hashes,
        "binaries": {name: sha256_file(BUILD / name)
                     for name in BINARIES if (BUILD / name).is_file()},
        "shared_dependencies": {
            name: linked_dependencies(BUILD / name)
            for name in BINARIES if (BUILD / name).is_file()
        },
        "runtime": {name: sha256_file(ROOT / name)
                    for name in ("compress.goose", "native/compress.c")
                    if (ROOT / name).is_file()},
        "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                   capture_output=True, text=True).stdout.strip(),
        "git_status": subprocess.run(["git", "status", "--porcelain=v1", "-uall"],
                                      cwd=ROOT, capture_output=True,
                                      text=True).stdout.splitlines(),
    }


def stable_snapshot(snapshot):
    """Load average is an observation, not immutable build provenance.

    The benchmark itself changes load average, so comparing that field would
    reject an uncontended run. Keep it in both snapshots while comparing all
    compiler, source, binary, dependency and platform identity fields.
    """
    return {key: value for key, value in snapshot.items()
            if key != "load_average"}


def command_for(harness, stem, iterations, budget):
    binary = BUILD / harness["binary"]
    arguments = [str(binary)]
    if harness["mode"]:
        arguments.append(harness["mode"])
    arguments += [str(INPUTS / (stem + ".gz")), str(iterations), str(budget)]
    return arguments


def run_once(harness, stem, iterations, budget):
    result = subprocess.run(command_for(harness, stem, iterations, budget),
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit("%s on %s failed: %s"
                         % (harness["label"], stem, result.stderr.strip()))
    fields = {}
    for token in result.stdout.split():
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def dispersion(samples):
    ordered = sorted(samples)
    count = len(ordered)
    median = statistics.median(ordered)
    deviations = [abs(value - median) for value in ordered]
    return {
        "count": count,
        "min": ordered[0],
        "max": ordered[-1],
        "median": int(median),
        "mean": int(statistics.fmean(ordered)),
        "stdev": int(statistics.stdev(ordered)) if count > 1 else 0,
        "mad": int(statistics.median(deviations)),
        "p90": ordered[min(count - 1, int(round(0.90 * (count - 1))))],
        "p99": ordered[min(count - 1, int(round(0.99 * (count - 1))))],
        "iqr": ordered[min(count - 1, int(round(0.75 * (count - 1))))] -
               ordered[int(round(0.25 * (count - 1)))],
        "samples": ordered,
    }


def plan(manifest, samples, warmup):
    lines = []
    total_calls = 0
    valid_payload_bytes = 0
    rejection_calls = 0
    for entry in manifest["inputs"]:
        stem = entry["name"]
        iterations = measure.ITERATIONS[stem.split("-")[0]]
        for harness in HARNESSES:
            arguments = command_for(harness, stem, iterations, entry["plain_bytes"])
            lines.append(" ".join(arguments))
            total_calls += (samples + warmup) * iterations
            valid_payload_bytes += ((samples + warmup) * iterations *
                                    entry["plain_bytes"])
    for entry in manifest.get("invalid", []):
        stem = pathlib.Path(entry["name"]).stem
        iterations = 200
        lines.append(" ".join(command_for(
            INVALID_HARNESS, stem, iterations, 1 << 20)))
        total_calls += (samples + warmup) * iterations
        rejection_calls += (samples + warmup) * iterations
    return lines, total_calls, valid_payload_bytes, rejection_calls


def invalid_rows(manifest, samples, warmup):
    """Measure rejection paths separately; they have no fair competitor row."""
    rows = []
    for entry in manifest.get("invalid", []):
        stem = pathlib.Path(entry["name"]).stem
        iterations = 200
        budget = 1 << 20
        input_path = INPUTS / entry["name"]
        timings = []
        rss = []
        content = None
        error = entry["error"]
        for round_index in range(samples + warmup):
            fields = run_once(INVALID_HARNESS, stem, iterations, budget)
            if fields.get("error") != error:
                raise SystemExit("%s: expected %s, got %s" %
                                 (entry["name"], error, fields.get("error")))
            observed = (int(fields["output"]), int(fields["fnv1a"]))
            if content is None:
                content = observed
            elif observed != content:
                raise SystemExit("%s: rejected output changed between samples" %
                                 entry["name"])
            if round_index >= warmup:
                timings.append(int(fields["per_call_ns"]))
                if "peak_rss_kb" in fields:
                    rss.append(int(fields["peak_rss_kb"]))
        row = {
            "input": entry["name"],
            "compressed_bytes": input_path.stat().st_size,
            "input_sha256": sha256_file(input_path),
            "output_bytes": content[0],
            "iterations": iterations,
            "harness": INVALID_HARNESS["label"],
            "contract": INVALID_HARNESS["contract"],
            "buffer": INVALID_HARNESS["buffer"],
            "comparable": False,
            "role": INVALID_HARNESS["role"],
            "error": error,
            "fnv1a": content[1],
            "per_call_ns": dispersion(timings),
            "command": " ".join(command_for(
                INVALID_HARNESS, stem, iterations, budget)),
        }
        if rss:
            row["peak_rss_kb"] = dispersion(rss)
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".build/benchmark.json")
    parser.add_argument("--samples", type=int, default=20,
                        help="retained samples per row, after the warm-up rounds")
    parser.add_argument("--warmup", type=int, default=2,
                        help="rounds discarded before the first retained sample")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--max-load", type=float, default=0.5,
                        help="refuse to start when the 1-minute load average is above this")
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--cc", default="cc")
    arguments = parser.parse_args()

    if arguments.samples < 20:
        raise SystemExit("--samples must be at least 20; fewer cannot show a tail")
    if arguments.warmup < 1:
        raise SystemExit("--warmup must be at least 1")

    manifest = measure.prepare()
    print("prepared %d inputs in %s" % (len(manifest["inputs"]), INPUTS))

    if arguments.plan:
        lines, total_calls, payload_bytes, rejection_calls = plan(
            manifest, arguments.samples, arguments.warmup)
        print("%d invocations, %d decode calls in total"
              % (len(lines) * (arguments.samples + arguments.warmup), total_calls))
        print("%.1f GiB valid payload bytes; %d invalid-input rejection calls"
              % (payload_bytes / (1 << 30), rejection_calls))
        for line in lines:
            print(line)
        print("plan only: nothing was timed")
        return 0

    if arguments.build_only:
        print("build-only: no timing was run")
        return 0

    for name in BINARIES:
        if not (BUILD / name).is_file():
            raise SystemExit("missing %s; run `make bench-build` first" % name)

    load = load_average()[0]
    if load > arguments.max_load:
        raise SystemExit("load average %.2f is above --max-load %.2f; "
                         "run this alone" % (load, arguments.max_load))

    goose_path = pathlib.Path(arguments.goose)
    if not goose_path.is_absolute():
        goose_path = ROOT / goose_path
    goose_path = goose_path.resolve()
    try:
        affinity_allowed = sorted(os.sched_getaffinity(0))
        affinity_cpu = affinity_allowed[0]
        os.sched_setaffinity(0, {affinity_cpu})
    except (AttributeError, OSError, IndexError):
        affinity_allowed = None
        affinity_cpu = None

    before = snapshot(goose_path, arguments.cc, affinity_allowed, affinity_cpu)
    rows = []
    for entry in manifest["inputs"]:
        stem = entry["name"]
        iterations = measure.ITERATIONS[stem.split("-")[0]]
        budget = entry["plain_bytes"]
        collected = {harness["label"]: [] for harness in HARNESSES}
        memory = {harness["label"]: [] for harness in HARNESSES}
        # Alternating order: each round starts one harness later, so a slow
        # drift over the run is spread across every row instead of landing on
        # whichever row happened to run last.
        for round_index in range(arguments.samples + arguments.warmup):
            shift = round_index % len(HARNESSES)
            order = HARNESSES[shift:] + HARNESSES[:shift]
            for harness in order:
                fields = run_once(harness, stem, iterations, budget)
                if int(fields["output"]) != entry["plain_bytes"]:
                    raise SystemExit("%s on %s: wrong output length"
                                     % (harness["label"], stem))
                if int(fields["fnv1a"]) != entry["fnv1a"]:
                    raise SystemExit("%s on %s: wrong output digest"
                                     % (harness["label"], stem))
                if round_index < arguments.warmup:
                    continue
                collected[harness["label"]].append(int(fields["per_call_ns"]))
                if "peak_rss_kb" in fields:
                    memory[harness["label"]].append(int(fields["peak_rss_kb"]))
        for harness in HARNESSES:
            samples = collected[harness["label"]]
            if len(samples) != arguments.samples:
                raise SystemExit("%s on %s: %d samples, expected %d"
                                 % (harness["label"], stem, len(samples),
                                    arguments.samples))
            row = {
                "input": stem,
                "plain_bytes": entry["plain_bytes"],
                "compressed_bytes": entry["compressed_bytes"],
                "iterations": iterations,
                "harness": harness["label"],
                "contract": harness["contract"],
                "buffer": harness["buffer"],
                "comparable": harness["comparable"],
                "role": harness["role"],
                "fnv1a": entry["fnv1a"],
                "per_call_ns": dispersion(samples),
                "command": " ".join(command_for(harness, stem, iterations, budget)),
            }
            if memory[harness["label"]]:
                row["peak_rss_kb"] = dispersion(memory[harness["label"]])
            rows.append(row)

    rows.extend(invalid_rows(manifest, arguments.samples, arguments.warmup))

    after = snapshot(goose_path, arguments.cc, affinity_allowed, affinity_cpu)
    report = {
        "provenance": measure.provenance(),
        "before": before,
        "after": after,
        "stable": stable_snapshot(before) == stable_snapshot(after),
        "method": {
            "samples": arguments.samples,
            "warmup_rounds": arguments.warmup,
            "order": "alternating: each round starts one harness later",
            "timing": "CLOCK_MONOTONIC around the call loop, one warm-up call outside it",
            "statistic": "median with min/max/stdev/MAD/p90/p99/IQR over the raw samples",
            "verification": "every sample must report the expected output length and FNV-1a",
            "memory": "peak RSS read from /proc/self/status VmHWM by the harness",
            "affinity": "all measured child processes inherit one selected allowed CPU",
            "load_average": "recorded before and after; excluded from identity comparison because the run changes it",
            "toolchain": "Goose and C compiler paths, binary hashes and versions; linked shared-object hashes recorded before and after",
            "ranking": "only rows with comparable=true share a contract; "
                       "libdeflate is a throughput reference and does not rank streaming",
        },
        "inputs": manifest["inputs"],
        "rows": rows,
    }
    pathlib.Path(arguments.output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("wrote %s" % arguments.output)
    if not report["stable"]:
        print("WARNING: provenance changed during the run; the samples are suspect")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
