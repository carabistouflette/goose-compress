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


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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


def snapshot():
    """Everything that could change between the first and the last sample."""
    return {
        "load_average": list(load_average()),
        "cpu_governor": cpu_governor(),
        "binaries": {name: sha256_file(BUILD / name)
                     for name in BINARIES if (BUILD / name).is_file()},
        "runtime": {name: sha256_file(ROOT / name)
                    for name in ("compress.goose", "native/compress.c")
                    if (ROOT / name).is_file()},
        "git_head": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                                   capture_output=True, text=True).stdout.strip(),
        "git_dirty": bool(subprocess.run(["git", "status", "--porcelain"],
                                         cwd=ROOT, capture_output=True,
                                         text=True).stdout.strip()),
    }


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
    for entry in manifest["inputs"]:
        stem = entry["name"]
        iterations = measure.ITERATIONS[stem.split("-")[0]]
        for harness in HARNESSES:
            arguments = command_for(harness, stem, iterations, entry["plain_bytes"])
            lines.append(" ".join(arguments))
            total_calls += (samples + warmup) * iterations
    return lines, total_calls


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

    manifest = measure.prepare()
    print("prepared %d inputs in %s" % (len(manifest["inputs"]), INPUTS))

    if arguments.plan:
        lines, total_calls = plan(manifest, arguments.samples, arguments.warmup)
        print("%d invocations, %d decode calls in total"
              % (len(lines) * (arguments.samples + arguments.warmup), total_calls))
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

    before = snapshot()
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

    after = snapshot()
    report = {
        "provenance": measure.provenance(),
        "before": before,
        "after": after,
        "stable": before == after,
        "method": {
            "samples": arguments.samples,
            "warmup_rounds": arguments.warmup,
            "order": "alternating: each round starts one harness later",
            "timing": "CLOCK_MONOTONIC around the call loop, one warm-up call outside it",
            "statistic": "median with min/max/stdev/MAD/p90/p99/IQR over the raw samples",
            "verification": "every sample must report the expected output length and FNV-1a",
            "memory": "peak RSS read from /proc/self/status VmHWM by the harness",
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
