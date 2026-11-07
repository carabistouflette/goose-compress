#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Runs the benchmark at equal contracts and records provenance.

    python3 bench/measure.py --build-only          # prepare inputs, no timing
    python3 bench/measure.py --output .build/benchmark.json

Every harness decodes the same bytes and must report the same output length
and the same FNV-1a digest, so a row that did less work is rejected rather
than reported. Timings are nanoseconds per call, measured in-process, and
reported as the median of --repetitions runs of --iterations calls.

Run this alone, on an idle machine. Concurrent runs on one CPU make every row
meaningless.
"""

import argparse
import hashlib
import json
import pathlib
import platform
import random
import shutil
import statistics
import subprocess
import sys
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
INPUTS = BUILD / "bench-inputs"

SIZES = [
    ("1KiB", 1 << 10),
    ("64KiB", 64 << 10),
    ("1MiB", 1 << 20),
    ("8MiB", 8 << 20),
]

# Iterations per size: smaller inputs need more calls to reach a stable time.
ITERATIONS = {"1KiB": 2000, "64KiB": 500, "1MiB": 100, "8MiB": 20}


def fnv1a(data):
    digest = 14695981039346656037
    for byte in data:
        digest ^= byte
        digest = (digest * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return digest


def payload(kind, size):
    """Deterministic, reproducible content: no /dev/urandom, no clock."""
    if kind == "text":
        words = [b"goose", b"compress", b"bounded", b"stream", b"deflate"]
        out = bytearray()
        while len(out) < size:
            out += words[len(out) % len(words)] + b" "
        return bytes(out[:size])
    if kind == "random":
        rng = random.Random(size)
        return bytes(rng.getrandbits(8) for _ in range(size))
    raise AssertionError(kind)


def prepare():
    INPUTS.mkdir(parents=True, exist_ok=True)
    manifest = {"inputs": []}
    for name, size in SIZES:
        for kind in ("text", "random"):
            plain = payload(kind, size)
            stream = zlib.compressobj(6, zlib.DEFLATED, 31)
            compressed = stream.compress(plain) + stream.flush()
            stem = f"{name}-{kind}"
            (INPUTS / f"{stem}.gz").write_bytes(compressed)
            (INPUTS / f"{stem}.plain").write_bytes(plain)
            manifest["inputs"].append({
                "name": stem,
                "plain_bytes": len(plain),
                "compressed_bytes": len(compressed),
                "plain_sha256": hashlib.sha256(plain).hexdigest(),
                "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
                "fnv1a": fnv1a(plain),
            })
    # Invalid inputs: a truncated stream and a corrupted CRC.
    source = (INPUTS / "64KiB-text.gz").read_bytes()
    (INPUTS / "invalid-truncated.gz").write_bytes(source[: len(source) // 2])
    broken = bytearray(source)
    broken[-8] ^= 0xFF
    (INPUTS / "invalid-crc.gz").write_bytes(bytes(broken))
    manifest["invalid"] = [
        {"name": "invalid-truncated.gz", "error": "truncated stream"},
        {"name": "invalid-crc.gz", "error": "checksum mismatch"},
    ]
    (INPUTS / "inputs.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def provenance():
    def first_line(command):
        try:
            return subprocess.run(command, capture_output=True, text=True,
                                  check=False).stdout.splitlines()[0].strip()
        except (OSError, IndexError):
            return "unknown"

    revision = (ROOT / "GOOSE_REVISION").read_text().strip()
    cpu = "unknown"
    try:
        for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "goose_revision": revision,
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "libdeflate": first_line(["pkg-config", "--modversion", "libdeflate"]),
        "cc": first_line(["cc", "--version"]),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": cpu,
        "python": platform.python_version(),
    }


def run(harness, arguments):
    result = subprocess.run([str(harness)] + arguments, capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise SystemExit(f"{harness.name} failed: {result.stderr.strip()}")
    fields = {}
    for token in result.stdout.split():
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def measure(manifest, repetitions):
    rows = []
    for entry in manifest["inputs"]:
        stem = entry["name"]
        budget = entry["plain_bytes"]
        iterations = ITERATIONS[stem.split("-")[0]]
        harnesses = [
            ("goose-compress oneshot", BUILD / "bench", ["oneshot"]),
            ("goose-compress stream", BUILD / "bench", ["stream"]),
            ("zlib oneshot (same library, no bounds)", BUILD / "zlib-oneshot", []),
            ("libdeflate oneshot (no bounds)", BUILD / "libdeflate-oneshot", []),
        ]
        for label, binary, prefix in harnesses:
            samples = []
            for _ in range(repetitions):
                fields = run(binary, prefix + [str(INPUTS / f"{stem}.gz"),
                                               str(iterations), str(budget)])
                if int(fields["output"]) != entry["plain_bytes"]:
                    raise SystemExit(f"{label} on {stem}: wrong output length")
                if int(fields["fnv1a"]) != entry["fnv1a"]:
                    raise SystemExit(f"{label} on {stem}: wrong output digest")
                samples.append(int(fields["per_call_ns"]))
            rows.append({
                "input": stem,
                "harness": label,
                "plain_bytes": entry["plain_bytes"],
                "compressed_bytes": entry["compressed_bytes"],
                "iterations": iterations,
                "repetitions": repetitions,
                "per_call_ns_median": int(statistics.median(samples)),
                "per_call_ns_samples": samples,
                "fnv1a": entry["fnv1a"],
            })
    return rows


def measure_invalid(manifest, repetitions):
    """Only this library has a bounded error path to measure."""
    rows = []
    for entry in manifest["invalid"]:
        name = entry["name"]
        samples = []
        for _ in range(repetitions):
            fields = run(BUILD / "bench",
                         ["invalid", str(INPUTS / name), "200", str(1 << 20)])
            if fields["error"] != entry["error"]:
                raise SystemExit(f"{name}: expected {entry['error']}, got {fields['error']}")
            samples.append(int(fields["per_call_ns"]))
        rows.append({
            "input": name,
            "error": entry["error"],
            "harness": "goose-compress invalid",
            "iterations": 200,
            "repetitions": repetitions,
            "per_call_ns_median": int(statistics.median(samples)),
            "per_call_ns_samples": samples,
            "note": "zlib and libdeflate one-shot have no bounded error path "
                    "to compare at equal contract",
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".build/benchmark.json")
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--cc", default="cc")
    arguments = parser.parse_args()

    manifest = prepare()
    print(f"prepared {len(manifest['inputs'])} inputs in {INPUTS}")
    if arguments.build_only:
        print("build-only: no timing was run")
        return 0

    for binary in ("bench", "zlib-oneshot", "libdeflate-oneshot"):
        if not (BUILD / binary).exists():
            raise SystemExit(f"missing {binary}; run `make bench-build` first")

    rows = measure(manifest, arguments.repetitions)
    rows += measure_invalid(manifest, arguments.repetitions)
    report = {
        "provenance": provenance(),
        "method": {
            "timing": "CLOCK_MONOTONIC around the call loop, one warm-up call outside it",
            "statistic": f"median of {arguments.repetitions} runs of per-call nanoseconds",
            "verification": "every run must report the expected output length and FNV-1a",
            "contract": "one gzip stream in, the whole payload out, one member, no trailing bytes",
            "excluded": "no cross-library speed ranking; rows differ in bounds and allocation",
        },
        "inputs": manifest["inputs"],
        "rows": rows,
    }
    pathlib.Path(arguments.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
