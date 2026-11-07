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

Run it alone. The default contention protocol measures whether the CPU it
pinned to deschedules it, rather than reading a machine-wide load average that
says nothing about the core in use; see the "Contention protocol" section.
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


# ---------------------------------------------------------------------------
# Contention protocol
#
# The previous protocol refused to start when the 1-minute load average was
# above 0.5. On a multi-core machine that number is not a measure of whether
# *this* measurement is contended: three unrelated single-threaded processes on
# eight cores put it above 0.5 while leaving seven cores idle, and one busy
# process on the core we pinned to can leave it below 0.5 while corrupting
# every sample. It is kept as `--protocol absolute` so old runs stay
# interpretable, but it is no longer the default.
#
# The default protocol measures the thing that actually matters: whether the
# pinned CPU deschedules us. A fixed CPU-bound calibration runs on the selected
# CPU and reports, per batch, wall time over process CPU time. On an
# uncontended core that ratio is 1.0; every deschedule pushes it up. The gate
# is on the p90 of that ratio, not on a machine-wide average, and it is
# recorded before and after the run so a machine that changed underneath the
# measurement is visible.
# ---------------------------------------------------------------------------

CPU_ROOT = pathlib.Path("/sys/devices/system/cpu")


def read_int(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def cpu_topology():
    """Per-CPU max frequency, core class and SMT siblings.

    A hybrid part has fast and slow cores, so which CPU a row ran on is part of
    the measurement's identity, not a footnote. `core_type` is not exposed on
    every kernel, so the class is derived from the maximum frequency and the
    derivation is recorded.
    """
    topology = {}
    for entry in sorted(CPU_ROOT.glob("cpu[0-9]*")):
        index = entry.name[3:]
        if not index.isdigit():
            continue
        maximum = read_int(entry / "cpufreq" / "cpuinfo_max_freq")
        siblings = None
        try:
            siblings = (entry / "topology" / "thread_siblings_list").read_text().strip()
        except OSError:
            pass
        topology[int(index)] = {
            "max_freq_khz": maximum,
            "thread_siblings": siblings,
            "core_type": (entry / "topology" / "core_type").read_text().strip()
            if (entry / "topology" / "core_type").is_file() else None,
        }
    frequencies = [t["max_freq_khz"] for t in topology.values()
                   if t["max_freq_khz"]]
    if frequencies:
        fastest = max(frequencies)
        # A hybrid part's fast and slow clusters differ by a lot (4.7-4.8 GHz
        # against 3.7 GHz here), while cores inside one cluster differ by a
        # few tens of MHz of per-core turbo headroom. Grouping on an exact
        # match would split a cluster; 90% of the fastest separates them.
        for record in topology.values():
            if record["max_freq_khz"] is None:
                record["class"] = "unknown"
            elif record["max_freq_khz"] >= 0.9 * fastest:
                record["class"] = "fast"
            else:
                record["class"] = "slow"
    return topology


def cpu_frequency(cpu):
    base = CPU_ROOT / ("cpu%d" % cpu) / "cpufreq"
    return {
        "avg_khz": read_int(base / "cpuinfo_avg_freq"),
        "max_khz": read_int(base / "cpuinfo_max_freq"),
        "cur_khz": read_int(base / "scaling_cur_freq"),
    }


def thermal_celsius():
    readings = []
    for zone in sorted(pathlib.Path("/sys/class/thermal").glob("thermal_zone*")):
        value = read_int(zone / "temp")
        if value is not None:
            readings.append(value / 1000.0)
    if not readings:
        return None
    return {"max_c": max(readings), "zones": len(readings)}


def psi_cpu():
    """Linux pressure-stall information for the CPU, if the kernel exports it."""
    try:
        text = pathlib.Path("/proc/pressure/cpu").read_text()
    except OSError:
        return None
    record = {}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        kind = fields[0]
        values = {}
        for token in fields[1:]:
            key, _, value = token.partition("=")
            try:
                values[key] = float(value)
            except ValueError:
                values[key] = value
        record[kind] = values
    return record


def per_cpu_stat():
    """Cumulative per-CPU jiffies, so a caller can difference two reads."""
    counters = {}
    try:
        text = pathlib.Path("/proc/stat").read_text()
    except OSError:
        return counters
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].startswith("cpu"):
            continue
        name = fields[0]
        if name == "cpu":
            continue
        try:
            values = [int(value) for value in fields[1:]]
        except ValueError:
            continue
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        counters[name] = {"idle": idle, "total": total}
    return counters


def cpu_busy_fraction(before, after):
    """Busy fraction per CPU between two `per_cpu_stat` reads."""
    busy = {}
    for name, start in before.items():
        end = after.get(name)
        if not end:
            continue
        total = end["total"] - start["total"]
        idle = end["idle"] - start["idle"]
        if total <= 0:
            busy[name] = None
        else:
            busy[name] = round(1.0 - (idle / total), 4)
    return busy


def context_switches():
    """Voluntary and involuntary context switches of this process.

    A deschedule shows up here even when the wall/CPU ratio of a single batch
    is too coarse to see it, so the calibration records the delta across its
    own window instead of inferring contention from timing alone.
    """
    record = {}
    try:
        text = pathlib.Path("/proc/self/status").read_text()
    except OSError:
        return record
    for line in text.splitlines():
        if line.startswith("voluntary_ctxt_switches:"):
            record["voluntary"] = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            record["nonvoluntary"] = int(line.split()[1])
    return record


def calibrate(cpu, milliseconds, repeats, minimum):
    """Measures descheduling and foreign CPU use on one CPU.

    Two signals, both taken over the same window so they describe the same
    moment:

    - `wall / process_cpu` per batch. It is 1.0 when the process was never
      taken off the core and grows with every deschedule, so its p90 is a
      direct contention signal rather than a proxy.
    - the fraction of the selected CPU that was busy with something other than
      this process. `/proc/stat` counts every process on the core, so
      subtracting our own CPU time from the core's busy time leaves exactly the
      foreign share. A core that is 40% busy because *we* are running on it is
      not contended; a core that is 5% busy with someone else is.
    """
    import time

    def batch():
        start_wall = time.monotonic()
        start_cpu = time.process_time()
        accumulator = 0
        for i in range(200000):
            accumulator = (accumulator * 1103515245 + 12345) & 0x7FFFFFFF
        return time.monotonic() - start_wall, time.process_time() - start_cpu

    # One discarded batch so the first one does not pay for import and warm-up.
    batch()
    ratios = []
    walls = []
    name = "cpu%d" % cpu
    stat_before = per_cpu_stat()
    switches_before = context_switches()
    wall_start = time.monotonic()
    cpu_start = time.process_time()
    deadline = wall_start + milliseconds / 1000.0
    while len(ratios) < repeats and time.monotonic() < deadline:
        wall, used = batch()
        if used <= 0:
            continue
        ratios.append(wall / used)
        walls.append(wall)
    cpu_used = time.process_time() - cpu_start
    window = time.monotonic() - wall_start
    stat_after = per_cpu_stat()
    switches_after = context_switches()
    if not ratios:
        return None
    busy = cpu_busy_fraction(stat_before, stat_after).get(name)
    own_share = (cpu_used / window) if window > 0 else None
    foreign = None
    if busy is not None and own_share is not None:
        foreign = round(max(0.0, busy - own_share), 4)
    ordered = sorted(ratios)
    count = len(ordered)
    # A quantile over one or two observations is not a quantile. The gate
    # refuses a shortfall instead of reporting a p90 it cannot support.
    shortfall = None
    if count < minimum:
        shortfall = ("only %d calibration batch(es) completed, below the "
                     "minimum of %d; raise --calibration-ms or lower "
                     "--calibration-repeats" % (count, minimum))
    return {
        "cpu": cpu,
        "batches": count,
        "minimum_batches": minimum,
        "shortfall": shortfall,
        "context_switches": {
            "voluntary": switches_after.get("voluntary", 0) - switches_before.get("voluntary", 0),
            "nonvoluntary": switches_after.get("nonvoluntary", 0) - switches_before.get("nonvoluntary", 0),
        },
        "window_s": round(window, 4),
        "cpu_busy_fraction": busy,
        "own_cpu_share": round(own_share, 4) if own_share is not None else None,
        "foreign_cpu_fraction": foreign,
        "wall_over_cpu_median": round(statistics.median(ordered), 4),
        "wall_over_cpu_p90": round(ordered[min(count - 1, int(round(0.90 * (count - 1))))], 4),
        "wall_over_cpu_max": round(ordered[-1], 4),
        "wall_median_ms": round(statistics.median(walls) * 1000.0, 3),
        "ratios": [round(value, 4) for value in ratios],
    }


def choose_cpu(allowed, busy, topology):
    """Picks a fast core first, then the least busy one.

    A hybrid part runs its fast and slow cores at different clocks, so a row
    measured on a slow core is not comparable with one measured on a fast core.
    Load only breaks ties inside a class: silently measuring on a slow core
    because it happened to be idle would make two runs of the same row
    incomparable. If every fast core is contended the gate refuses the run, and
    the operator can pass `--cpu` to choose deliberately.
    """
    def rank(cpu):
        name = "cpu%d" % cpu
        load = busy.get(name)
        load = 1.0 if load is None else load
        record = topology.get(cpu, {})
        fast = 1 if record.get("class") == "fast" else 0
        return (-fast, round(load, 3), cpu)

    return sorted(allowed, key=rank)[0]


def contention_report(allowed, selected, topology, busy, calibration, psi,
                      thermal, gates):
    """The measured contention state, plus the verdict on each gate.

    The gates are on what the calibration measured on the selected CPU, not on
    a machine-wide average: a core that is busy because this process is running
    on it is not contended, and a core that is idle while another process
    deschedules us is.
    """
    record = topology.get(selected, {})
    verdict = {
        "foreign_cpu_fraction": calibration["foreign_cpu_fraction"] if calibration else None,
        "foreign_cpu_limit": gates["max_foreign_cpu"],
        "jitter_p90": calibration["wall_over_cpu_p90"] if calibration else None,
        "jitter_limit": gates["max_jitter"],
        "psi_some_avg10": (psi or {}).get("some", {}).get("avg10"),
        "psi_limit": gates["max_psi"],
        "context_switches": (calibration or {}).get("context_switches"),
        "batches": (calibration or {}).get("batches"),
        "minimum_batches": (calibration or {}).get("minimum_batches"),
    }
    failures = []
    if calibration is None:
        failures.append("the calibration produced no batches")
    elif calibration.get("shortfall"):
        failures.append(calibration["shortfall"])
    elif verdict["jitter_p90"] > gates["max_jitter"]:
        failures.append("wall/cpu p90 is %.3f, above the %.3f limit"
                        % (verdict["jitter_p90"], gates["max_jitter"]))
    if verdict["foreign_cpu_fraction"] is None:
        failures.append("the selected CPU's foreign share is unavailable")
    elif verdict["foreign_cpu_fraction"] > gates["max_foreign_cpu"]:
        failures.append("cpu%d spent %.1f%% of the calibration window on other "
                        "processes, above the %.1f%% limit"
                        % (selected, 100 * verdict["foreign_cpu_fraction"],
                           100 * gates["max_foreign_cpu"]))
    if verdict["psi_some_avg10"] is not None and \
            verdict["psi_some_avg10"] > gates["max_psi"]:
        failures.append("CPU pressure some-avg10 is %.2f, above the %.2f limit"
                        % (verdict["psi_some_avg10"], gates["max_psi"]))
    return {
        "protocol": "effective",
        "selected_cpu": selected,
        "selected_cpu_class": record.get("class"),
        "selected_cpu_max_freq_khz": record.get("max_freq_khz"),
        "selected_cpu_thread_siblings": record.get("thread_siblings"),
        "selected_cpu_frequency": cpu_frequency(selected),
        "allowed_cpus": allowed,
        "selection_window_busy": busy,
        "calibration": calibration,
        "psi": psi,
        "thermal": thermal,
        "gates": gates,
        "verdict": verdict,
        "failures": failures,
        "ok": not failures,
    }


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
        "cpu_topology": cpu_topology(),
        "cpu_frequency": cpu_frequency(affinity_cpu) if affinity_cpu is not None else None,
        "per_cpu_stat": per_cpu_stat(),
        "psi_cpu": psi_cpu(),
        "thermal": thermal_celsius(),
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
    """Observations that the run itself changes are not build provenance.

    Load average, per-CPU jiffies, pressure-stall averages, thermal readings and
    the current frequency all move because the benchmark is running, so
    comparing them would reject an uncontended run. They stay in both snapshots
    as observations while every compiler, source, binary, dependency and
    platform identity field is compared.
    """
    observations = {"load_average", "per_cpu_stat", "psi_cpu", "thermal",
                    "cpu_frequency"}
    return {key: value for key, value in snapshot.items()
            if key not in observations}


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
                        help="absolute protocol only: refuse to start when the "
                             "1-minute load average is above this")
    parser.add_argument("--protocol", choices=("effective", "absolute"),
                        default="effective",
                        help="effective (default) gates on the pinned CPU's "
                             "measured descheduling; absolute is the previous "
                             "1-minute load-average gate, kept for continuity")
    parser.add_argument("--max-foreign-cpu", type=float, default=0.05,
                        help="effective protocol: refuse when other processes "
                             "used more than this fraction of the selected CPU "
                             "during the calibration window")
    parser.add_argument("--max-jitter", type=float, default=1.15,
                        help="effective protocol: refuse when the p90 of "
                             "wall/process-CPU time on the selected CPU is "
                             "above this")
    parser.add_argument("--max-psi", type=float, default=5.0,
                        help="effective protocol: refuse when CPU pressure "
                             "some-avg10 is above this")
    parser.add_argument("--sample-window", type=float, default=0.5,
                        help="seconds of /proc/stat used to measure per-CPU load")
    parser.add_argument("--cpu", type=int, default=None,
                        help="pin to this CPU instead of choosing one; a "
                             "controlled before/after comparison must pin the "
                             "same CPU in both runs")
    parser.add_argument("--calibration-ms", type=int, default=400,
                        help="effective protocol: calibration budget in ms")
    parser.add_argument("--calibration-repeats", type=int, default=15,
                        help="effective protocol: calibration batches to keep")
    parser.add_argument("--min-calibration-batches", type=int, default=10,
                        help="effective protocol: refuse when fewer batches "
                             "than this completed; a quantile over one or two "
                             "observations is not a quantile")
    parser.add_argument("--lock-file", default=None,
                        help="serialise against other CPU work by taking an "
                             "exclusive flock on this path for the whole run")
    parser.add_argument("--lock-timeout", type=float, default=3600.0,
                        help="seconds to wait for --lock-file")
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

    lock_handle = None
    if arguments.lock_file:
        import fcntl
        import time
        lock_handle = open(arguments.lock_file, "a+")
        deadline = time.monotonic() + arguments.lock_timeout
        while True:
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SystemExit("could not take %s within %.0fs; another "
                                     "CPU job holds it"
                                     % (arguments.lock_file,
                                        arguments.lock_timeout))
                time.sleep(0.5)
        print("holding %s for the whole run" % arguments.lock_file)

    goose_path = pathlib.Path(arguments.goose)
    if not goose_path.is_absolute():
        goose_path = ROOT / goose_path
    goose_path = goose_path.resolve()

    topology = cpu_topology()
    try:
        affinity_allowed = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity_allowed = None

    contention = None
    if arguments.protocol == "absolute":
        load = load_average()[0]
        if load > arguments.max_load:
            raise SystemExit("load average %.2f is above --max-load %.2f; "
                             "run this alone" % (load, arguments.max_load))
        contention = {"protocol": "absolute", "load_average_1m": load,
                      "limit": arguments.max_load, "ok": True, "failures": []}
        affinity_cpu = affinity_allowed[0] if affinity_allowed else None
    else:
        if not affinity_allowed:
            raise SystemExit("cannot read the CPU affinity mask")
        import time
        start = per_cpu_stat()
        time.sleep(arguments.sample_window)
        busy = cpu_busy_fraction(start, per_cpu_stat())
        if arguments.cpu is not None:
            if arguments.cpu not in affinity_allowed:
                raise SystemExit("cpu%d is not in the allowed set %s"
                                 % (arguments.cpu, affinity_allowed))
            selected = arguments.cpu
        else:
            selected = choose_cpu(affinity_allowed, busy, topology)
        affinity_cpu = selected
        try:
            os.sched_setaffinity(0, {selected})
        except OSError as failure:
            raise SystemExit("cannot pin to cpu%d: %s" % (selected, failure))
        calibration = calibrate(selected, arguments.calibration_ms,
                                arguments.calibration_repeats,
                                arguments.min_calibration_batches)
        contention = contention_report(
            affinity_allowed, selected, topology, busy, calibration, psi_cpu(),
            thermal_celsius(),
            {"max_foreign_cpu": arguments.max_foreign_cpu,
             "max_jitter": arguments.max_jitter,
             "max_psi": arguments.max_psi})
        print("cpu%d (%s core, max %s kHz): foreign %s, wall/cpu p90 %s, "
              "psi some-avg10 %s"
              % (selected, contention["selected_cpu_class"],
                 contention["selected_cpu_max_freq_khz"],
                 contention["verdict"]["foreign_cpu_fraction"],
                 contention["verdict"]["jitter_p90"],
                 contention["verdict"]["psi_some_avg10"]))
        if not contention["ok"]:
            for line in contention["failures"]:
                print("  contention: " + line)
            # A refused run is evidence too: keep the observation so a later
            # reader can see what the machine looked like, rather than only
            # that the gate said no.
            rejected = pathlib.Path(arguments.output).with_suffix(
                ".rejected.json")
            rejected.write_text(json.dumps(
                {"refused": True, "contention": contention,
                 "protocol": arguments.protocol,
                 "gates": {"max_foreign_cpu": arguments.max_foreign_cpu,
                           "max_jitter": arguments.max_jitter,
                           "max_psi": arguments.max_psi,
                           "min_calibration_batches":
                               arguments.min_calibration_batches}},
                indent=2, sort_keys=True) + "\n")
            print("wrote %s" % rejected)
            raise SystemExit("the effective contention gate refused this run; "
                             "wait for the CPU to be free, or pass "
                             "--protocol absolute to use the old gate")

    if affinity_cpu is not None:
        try:
            os.sched_setaffinity(0, {affinity_cpu})
        except OSError:
            pass

    before = snapshot(goose_path, arguments.cc, affinity_allowed, affinity_cpu)
    rows = []
    # Per-round observation of the sample loop itself. The endpoint checks
    # bracket the run; this is what says whether a transient landed inside it.
    round_seconds = []
    round_switches = []
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
            import time
            round_start = time.monotonic()
            switches_start = context_switches()
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
            if round_index >= arguments.warmup:
                round_seconds.append(time.monotonic() - round_start)
                switches_end = context_switches()
                round_switches.append(
                    switches_end.get("nonvoluntary", 0) -
                    switches_start.get("nonvoluntary", 0))
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

    # A round that took much longer than the median round is the signature of a
    # transient during sampling. The samples are kept either way; this records
    # which rounds are suspect so a reader can drop them deliberately.
    sample_phase = None
    if round_seconds:
        ordered_rounds = sorted(round_seconds)
        median_round = statistics.median(ordered_rounds)
        outliers = [index for index, value in enumerate(round_seconds)
                    if median_round > 0 and value > 1.5 * median_round]
        sample_phase = {
            "rounds": len(round_seconds),
            "median_round_s": round(median_round, 4),
            "max_round_s": round(ordered_rounds[-1], 4),
            "min_round_s": round(ordered_rounds[0], 4),
            "outlier_rounds": outliers,
            "nonvoluntary_switches_per_round": round_switches,
            "nonvoluntary_switches_total": sum(round_switches),
            "clean": not outliers,
        }
        if outliers:
            print("WARNING: %d sample round(s) took more than 1.5x the median "
                  "round: %s" % (len(outliers), outliers))

    # Re-measure the contention state after the run. A machine that became busy
    # halfway through is visible here even though the samples were accepted.
    contention_after = None
    if arguments.protocol == "effective" and affinity_cpu is not None:
        import time
        start = per_cpu_stat()
        time.sleep(arguments.sample_window)
        busy = cpu_busy_fraction(start, per_cpu_stat())
        calibration = calibrate(affinity_cpu, arguments.calibration_ms,
                                arguments.calibration_repeats,
                                arguments.min_calibration_batches)
        contention_after = contention_report(
            affinity_allowed, affinity_cpu, topology, busy, calibration,
            psi_cpu(), thermal_celsius(),
            {"max_foreign_cpu": arguments.max_foreign_cpu,
             "max_jitter": arguments.max_jitter,
             "max_psi": arguments.max_psi})
        if not contention_after["ok"]:
            print("WARNING: the CPU became contended during the run:")
            for line in contention_after["failures"]:
                print("  " + line)

    report = {
        "provenance": measure.provenance(),
        "before": before,
        "after": after,
        "stable": stable_snapshot(before) == stable_snapshot(after),
        "contention": {
            "before": contention,
            "after": contention_after,
            # Two endpoint checks bracket the run; they do not prove the
            # machine was quiet in between. A transient load during the sample
            # loop is caught by `sample_phase`, not by this flag.
            "endpoint_checks_passed": bool(contention and contention.get("ok")
                                           and (contention_after is None
                                                or contention_after.get("ok"))),
            "sample_phase": sample_phase,
        },
        "method": {
            "samples": arguments.samples,
            "warmup_rounds": arguments.warmup,
            "order": "alternating: each round starts one harness later",
            "timing": "CLOCK_MONOTONIC around the call loop, one warm-up call outside it",
            "statistic": "median with min/max/stdev/MAD/p90/p99/IQR over the raw samples",
            "verification": "every sample must report the expected output length and FNV-1a",
            "memory": "peak RSS read from /proc/self/status VmHWM by the harness",
            "affinity": "all measured child processes inherit one selected allowed CPU",
            "contention": "effective protocol: the least busy allowed CPU is "
                          "selected, then gated on its measured busy fraction, "
                          "on the p90 of wall/process-CPU time for a fixed "
                          "calibration on that CPU, and on CPU pressure "
                          "some-avg10; re-measured after the run",
            "load_average": "recorded before and after; excluded from identity "
                            "comparison because the run changes it",
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
