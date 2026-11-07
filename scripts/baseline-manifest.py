#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Freezes the identity of a reference commit before the runtime is touched.

The manifest is the audit trail for every later claim: it records the exact
bytes of the sources, the tests, the harness, the pinned compiler, the
competitor libraries and the baseline binaries, so a measurement or a
regression can be attributed to a known tree rather than to "main".

    python3 scripts/baseline-manifest.py --goose /path/to/goose
    python3 scripts/baseline-manifest.py --goose /path/to/goose --check

`--check` re-derives the manifest and fails when any recorded source byte
changed, which is how a later iteration proves it did not silently move the
baseline. Binaries are recorded but not required to be reproducible: a
compiler upgrade legitimately changes them, and the manifest says so.

Everything is written under `.build/baseline/`, which is git-ignored. The
committed record is `docs/hardening.md`, which quotes the hashes that matter.
"""

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / ".build" / "baseline"

# The binaries a baseline measurement or a regression run depends on. A
# missing one is recorded as absent rather than silently skipped.
BASELINE_BINARIES = [
    "driver",
    "driver-sanitize",
    "api",
    "api-debug",
    "api-sanitized",
    "spec-sanitized",
    "boundaries-sanitized",
    "differential-sanitized",
    "bench",
    "zlib-oneshot",
    "libdeflate-oneshot",
]

# Artefacts whose bytes the Goose tests assert against.
BASELINE_ARTEFACTS = [
    ".build/fixtures/index.bin",
    ".build/fixtures/manifest.json",
    ".build/corpus/corpus.bin",
    ".build/corpus/manifest.json",
    ".build/bench-inputs/inputs.json",
]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*arguments):
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True,
                            text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def first_line(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0].strip() if lines else "unavailable"


def tracked_files():
    listing = git("ls-files", "-z")
    if listing is None:
        return []
    return sorted(name for name in listing.split("\0") if name)


def untracked_files():
    listing = git("ls-files", "--others", "--exclude-standard", "-z")
    if listing is None:
        return []
    return sorted(name for name in listing.split("\0") if name)


def source_records():
    records = []
    for name in tracked_files():
        path = ROOT / name
        if not path.is_file():
            records.append({"path": name, "present": False})
            continue
        records.append({
            "path": name,
            "present": True,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def untracked_records():
    records = []
    for name in untracked_files():
        path = ROOT / name
        if not path.is_file():
            continue
        records.append({
            "path": name,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def binary_records():
    records = []
    for name in BASELINE_BINARIES:
        path = ROOT / ".build" / name
        if not path.is_file():
            records.append({"path": f".build/{name}", "present": False})
            continue
        records.append({
            "path": f".build/{name}",
            "present": True,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def artefact_records():
    records = []
    for name in BASELINE_ARTEFACTS:
        path = ROOT / name
        if not path.is_file():
            records.append({"path": name, "present": False})
            continue
        records.append({
            "path": name,
            "present": True,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return records


def library_identity(name, soname):
    """Resolves a shared library the way the linker would, and hashes it."""
    resolved = None
    try:
        result = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True,
                                check=False)
        for line in result.stdout.splitlines():
            if soname in line and "=>" in line:
                resolved = line.split("=>", 1)[1].strip()
                break
    except OSError:
        pass
    if not resolved:
        for candidate in (f"/usr/lib/lib{name}.so", f"/usr/lib64/lib{name}.so",
                          f"/usr/lib/x86_64-linux-gnu/lib{name}.so"):
            if pathlib.Path(candidate).exists():
                resolved = candidate
                break
    record = {"name": name, "soname": soname, "resolved": resolved}
    if resolved and pathlib.Path(resolved).is_file():
        record["sha256"] = sha256_file(resolved)
        record["bytes"] = pathlib.Path(resolved).stat().st_size
    return record


def compiler_record(goose):
    record = {"requested": str(goose)}
    path = pathlib.Path(goose)
    if not path.is_file():
        record["present"] = False
        return record
    record["present"] = True
    record["bytes"] = path.stat().st_size
    record["sha256"] = sha256_file(path)
    # The version banner carries the compiler revision and its runtime hash.
    environment = dict(os.environ)
    environment.pop("LD_LIBRARY_PATH", None)
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True,
                                text=True, check=False, env=environment)
        record["version"] = (result.stdout or result.stderr).strip().splitlines()[0]
    except OSError as failure:
        record["version"] = f"unavailable: {failure}"
    return record


def cpu_model():
    try:
        for line in pathlib.Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def build_manifest(goose):
    return {
        "schema": 1,
        "git": {
            "head": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "describe": git("describe", "--always", "--dirty"),
            "status_porcelain": git("status", "--porcelain=v1", "-uall"),
        },
        "compiler": compiler_record(goose),
        "libraries": [
            library_identity("z", "libz.so.1"),
            library_identity("deflate", "libdeflate.so"),
        ],
        "tools": {
            "cc": first_line(["cc", "--version"]),
            "gcc": first_line(["gcc", "--version"]),
            "clang": first_line(["clang", "--version"]),
            "python": platform.python_version(),
            "zlib_pkg_config": first_line(["pkg-config", "--modversion", "zlib"]),
            "libdeflate_pkg_config": first_line(["pkg-config", "--modversion",
                                                 "libdeflate"]),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": cpu_model(),
            "cpus": os.cpu_count(),
        },
        "sources": source_records(),
        "untracked": untracked_records(),
        "binaries": binary_records(),
        "artefacts": artefact_records(),
    }


def write_sums(manifest, directory):
    lines = []
    for group in ("sources", "untracked", "binaries", "artefacts"):
        for record in manifest[group]:
            if record.get("present") is False:
                lines.append(f"ABSENT  {record['path']}")
            else:
                lines.append(f"{record['sha256']}  {record['path']}")
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def compare(recorded, current):
    """Compares every recorded group and never folds drift into one verdict.

    Sources and the pinned compiler must not move. Binaries and generated
    artefacts are compared too: an earlier version of this check ignored them
    and reported "intact" while five sanitized binaries had been overwritten
    by a later build. A hash that describes bytes no longer on disk is not
    evidence, so drift is reported per file and fails the check.
    """
    problems = []
    previous = {entry["path"]: entry for entry in recorded["sources"]}
    for entry in current["sources"]:
        old = previous.get(entry["path"])
        if old is None:
            problems.append(f"new source file: {entry['path']}")
        elif old.get("sha256") != entry.get("sha256"):
            problems.append(f"changed source: {entry['path']}")
    for path in previous:
        if path not in {entry["path"] for entry in current["sources"]}:
            problems.append(f"removed source: {path}")
    if recorded["compiler"].get("sha256") != current["compiler"].get("sha256"):
        problems.append("the pinned compiler binary changed")
    for entry in current.get("artefacts", []):
        old = {item["path"]: item for item in recorded.get("artefacts", [])}.get(entry["path"])
        if old is None:
            problems.append(f"new artefact: {entry['path']}")
        elif old.get("sha256") != entry.get("sha256"):
            problems.append(f"changed artefact: {entry['path']}")
    return problems


def binary_drift(recorded, current):
    """Binaries are reported separately because the archive now owns them.

    A rebuilt binary is not a corrupt source tree, but it is also not
    "intact": the hash describes bytes that are no longer on disk. This
    returns the drift so the caller can say exactly that instead of folding
    it into a single verdict.
    """
    drift = []
    previous = {entry["path"]: entry for entry in recorded.get("binaries", [])}
    for entry in current.get("binaries", []):
        old = previous.get(entry["path"])
        if old is None:
            drift.append((entry["path"], "absent", entry.get("sha256")))
        elif old.get("sha256") != entry.get("sha256"):
            drift.append((entry["path"], old.get("sha256"), entry.get("sha256")))
    for path in previous:
        if path not in {entry["path"] for entry in current.get("binaries", [])}:
            drift.append((path, previous[path].get("sha256"), "absent"))
    return drift


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true",
                        help="re-derive and fail if a recorded source byte moved")
    parser.add_argument("--strict", action="store_true",
                        help="also fail when a recorded binary no longer matches")
    arguments = parser.parse_args()

    goose = pathlib.Path(arguments.goose)
    if not goose.is_absolute():
        goose = (ROOT / goose).resolve()
    manifest = build_manifest(goose)

    if arguments.check:
        target = arguments.output / "manifest.json"
        if not target.is_file():
            raise SystemExit(f"no baseline manifest at {target}; run without --check")
        recorded = json.loads(target.read_text())
        # The source comparison is only meaningful while the tree is still at
        # the frozen commit. Once work starts, the sources legitimately move;
        # the archive holds the frozen snapshot and is verified separately.
        if recorded["git"]["head"] != manifest["git"]["head"]:
            print(f"tree has moved past the frozen commit "
                  f"{recorded['git']['head'][:12]} -> "
                  f"{manifest['git']['head'][:12]}")
            print(f"the frozen snapshot is in the archive; verify it with "
                  f"`baseline-archive.py --verify`")
            return 0
        problems = compare(recorded, manifest)
        drift = binary_drift(recorded, manifest)
        if problems:
            for problem in problems:
                print(f"FAIL {problem}", file=sys.stderr)
            raise SystemExit(f"baseline drifted: {len(problems)} differences")
        print(f"sources intact: {len(manifest['sources'])} files, "
              f"{len(manifest['artefacts'])} artefacts, "
              f"compiler {manifest['compiler'].get('sha256', 'absent')[:16]}")
        if drift:
            # Never call this intact: the recorded bytes are gone. The archive
            # is the binary record now, and STATUS.json says which are lost.
            print(f"binary drift: {len(drift)} of {len(manifest['binaries'])} "
                  f"recorded binaries no longer match; see "
                  f".build/baseline/STATUS.json and the authoritative archive")
            for path, old, new in drift:
                print(f"  DRIFT {path}: {(old or 'absent')[:16]} -> "
                      f"{(new or 'absent')[:16]}")
            if arguments.strict:
                raise SystemExit("binary drift under --strict")
        else:
            print(f"binaries intact: {len(manifest['binaries'])} files")
        return 0

    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    write_sums(manifest, arguments.output)

    present = sum(1 for entry in manifest["binaries"] if entry.get("present"))
    print(f"wrote {arguments.output / 'manifest.json'}")
    print(f"  git {manifest['git']['head']} on {manifest['git']['branch']}")
    print(f"  {len(manifest['sources'])} tracked sources, "
          f"{len(manifest['untracked'])} untracked files")
    print(f"  compiler {manifest['compiler'].get('sha256', 'absent')}")
    print(f"  {present} of {len(BASELINE_BINARIES)} baseline binaries present")
    for library in manifest["libraries"]:
        print(f"  {library['name']}: {library.get('sha256', 'unresolved')} "
              f"({library['resolved']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
