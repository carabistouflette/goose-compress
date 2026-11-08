#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Builds, archives and verifies the baseline binaries under one identity.

The first freeze recorded hashes of binaries that lived in `.build/`, where a
later `make sanitize SANITIZER_CC=gcc` silently overwrote five of them. That
is a real defect in the freeze procedure, not a cosmetic one: a hash that
describes bytes no longer on disk cannot support a later claim.

This tool fixes the procedure rather than the record:

* every variant is compiled into its **own** directory, so no build can
  overwrite another variant's bytes;
* an archive directory is created once and never reused, so a second freeze
  gets a new identity instead of rewriting history;
* every archived file is re-hashed on verification, and the live `.build`
  binaries are compared separately and reported as drift rather than folded
  into a single "intact" verdict;
* the shared libraries a binary actually loads are recorded with `ldd`, so a
  row can be attributed to the zlib and libdeflate bytes that ran.

    python3 scripts/baseline-archive.py --goose /path/to/goose --label c99b049
    python3 scripts/baseline-archive.py --verify .build/baseline/archive/c99b049

Nothing here measures performance. It only establishes identity.
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
ARCHIVE_ROOT = ROOT / ".build" / "baseline" / "archive"
VARIANTS_ROOT = ROOT / ".build" / "baseline" / "variants"
WRAPPER = ROOT / "scripts" / "sanitizer-cc.sh"
NATIVE = ROOT / "native" / "compress.c"
HEADER = ROOT / "native" / "compress.h"
CFLAGS = ["-std=c11", "-Wall", "-Wextra", "-Werror"]
SANITIZE_FLAGS = ["-O1", "-g", "-Inative"]
SANITIZE_LIBS = ["-lz", "-lpthread", "-lm", "-ldl"]
SANITIZE_TARGETS = ["spec", "boundaries", "api", "differential"]

# variant -> (compiler, [(binary name, kind)])
VARIANTS = {
    "release": [
        ("driver", "driver"),
        ("api", "goose-release"),
        ("api-debug", "goose-debug"),
        ("bench", "goose-bench"),
        ("zlib-oneshot", "zlib-oneshot"),
        ("libdeflate-oneshot", "libdeflate-oneshot"),
    ],
    "sanitize-clang": [("driver-sanitize", "driver-sanitize")]
                      + [(f"{name}-sanitized", "goose-sanitize") for name in SANITIZE_TARGETS],
    "sanitize-gcc": [("driver-sanitize", "driver-sanitize")]
                    + [(f"{name}-sanitized", "goose-sanitize") for name in SANITIZE_TARGETS],
}

SANITIZER_CC = {"sanitize-clang": "clang", "sanitize-gcc": "gcc"}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def child_env(extra=None):
    """The pinned compiler needs the system libstdc++, not the harness one."""
    environment = dict(os.environ)
    environment.pop("LD_LIBRARY_PATH", None)
    if extra:
        environment.update(extra)
    return environment


def run(command, env=None, cwd=ROOT):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            check=False, env=env or child_env())
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(map(str, command))}\n"
                         f"{result.stdout}{result.stderr}")
    return result


def git(*arguments):
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True,
                            text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def first_line(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False,
                                env=child_env())
    except OSError:
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0].strip() if lines else "unavailable"


def build_variant(variant, goose, directory):
    """Compiles one variant into its own directory. Never overwrites."""
    directory.mkdir(parents=True, exist_ok=False)
    cc = SANITIZER_CC.get(variant)
    built = []
    for name, kind in VARIANTS[variant]:
        target = directory / name
        if target.exists():
            raise SystemExit(f"refusing to overwrite {target}")
        if kind == "driver":
            run(["cc", *CFLAGS, "-O2", "-Inative", "-o", str(target),
                 str(ROOT / "tests" / "driver.c"), str(NATIVE), "-lz", "-lpthread"])
        elif kind == "goose-release":
            run([str(goose), "build", "tests/api.goose", "--cc", "cc", "-O2",
                 "-o", str(target)])
        elif kind == "goose-debug":
            run([str(goose), "build", "tests/api.goose", "--cc", "cc", "--debug",
                 "-o", str(target)])
        elif kind == "goose-bench":
            run([str(goose), "build", "bench/bench.goose", "--cc", "cc", "-O2",
                 "-o", str(target)])
        elif kind == "zlib-oneshot":
            run(["cc", *CFLAGS, "-O2", "-o", str(target),
                 str(ROOT / "bench" / "compare" / "zlib_oneshot.c"), "-lz"])
        elif kind == "libdeflate-oneshot":
            run(["cc", *CFLAGS, "-O2", "-o", str(target),
                 str(ROOT / "bench" / "compare" / "libdeflate_oneshot.c"), "-ldeflate"])
        elif kind == "driver-sanitize":
            run([str(WRAPPER), *SANITIZE_FLAGS, "-o", str(target),
                 str(ROOT / "tests" / "driver.c"), str(NATIVE), *SANITIZE_LIBS],
                env=child_env({"SANITIZER_CC": cc}))
        elif kind == "goose-sanitize":
            stem = name.removesuffix("-sanitized")
            source = directory / f"{stem}-sanitized.c"
            run([str(goose), f"tests/{stem}.goose", "--standalone", "--debug",
                 "--no-bce", "-o", str(source)])
            run([str(WRAPPER), *SANITIZE_FLAGS, "-o", str(target), str(source),
                 str(NATIVE), *SANITIZE_LIBS], env=child_env({"SANITIZER_CC": cc}))
        else:
            raise SystemExit(f"unknown build kind {kind}")
        built.append(name)
    return built


def ldd_record(binary):
    """The shared objects the loader actually resolves for this binary."""
    result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True,
                            check=False, env=child_env())
    lines = []
    for line in (result.stdout or result.stderr).splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line)
    return lines


def loaded_libraries(ldd_lines):
    """Maps soname -> resolved path, ignoring the loader's own entries."""
    mapping = {}
    for line in ldd_lines:
        if "=>" not in line:
            continue
        left, right = line.split("=>", 1)
        soname = left.strip()
        path = right.strip().split(" (")[0].strip()
        if path and path != "not found":
            mapping[soname] = path
    return mapping


def sanitizer_runtime(binary, loaded):
    """Proves the sanitizer runtime is linked in, for either linkage mode.

    clang links the ASan/UBSan runtimes statically, so `ldd` cannot show them
    and only the defined symbols can. gcc links them dynamically, so the
    symbols are undefined imports and only `ldd` can show them. Checking one
    mode alone would report an instrumented gcc binary as uninstrumented,
    which is the same class of mistake as trusting a debug build.
    """
    result = subprocess.run(["nm", str(binary)], capture_output=True, text=True,
                            check=False, env=child_env())
    defined = set()
    undefined = set()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-2] in ("T", "t", "D", "d", "B", "b", "W", "w"):
                defined.add(parts[-1])
            elif len(parts) == 2 and parts[0] == "U":
                undefined.add(parts[1])
    dynamic = {soname for soname in loaded if soname.startswith(("libasan", "libubsan"))}
    asan = "__asan_init" in defined or "__asan_init" in undefined or \
        any(name.startswith("libasan") for name in dynamic)
    ubsan = any(name.startswith("__ubsan_handle") for name in defined | undefined) or \
        any(name.startswith("libubsan") for name in dynamic)
    return {
        "asan": asan,
        "ubsan": ubsan,
        "linkage": "static" if "__asan_init" in defined else
                   "dynamic" if dynamic else "none",
        "nm": "ok" if result.returncode == 0 else "failed",
    }


def source_snapshot(destination):
    """Copies every tracked file plus the untracked ones that exist."""
    names = []
    listing = git("ls-files", "-z")
    if listing:
        names += [name for name in listing.split("\0") if name]
    listing = git("ls-files", "--others", "--exclude-standard", "-z")
    if listing:
        names += [name for name in listing.split("\0") if name]
    records = []
    for name in sorted(set(names)):
        source = ROOT / name
        if not source.is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        records.append({"path": name, "bytes": target.stat().st_size,
                        "sha256": sha256_file(target)})
    return records


def freeze(goose, label):
    archive = ARCHIVE_ROOT / label
    if archive.exists():
        raise SystemExit(f"refusing to reuse archive identity {archive}; "
                         "choose a new --label")
    archive.mkdir(parents=True)

    variants = {}
    for variant in VARIANTS:
        directory = VARIANTS_ROOT / label / variant
        if directory.exists():
            raise SystemExit(f"refusing to reuse variant directory {directory}")
        names = build_variant(variant, goose, directory)
        entries = []
        for name in names:
            binary = directory / name
            # The archive must hold the bytes, not a pointer to a build tree
            # that a later `make` can overwrite. Copy first, hash the copy.
            archived = archive / "variants" / variant / name
            archived.parent.mkdir(parents=True, exist_ok=True)
            if archived.exists():
                raise SystemExit(f"refusing to overwrite archived binary {archived}")
            # copy2 keeps the mode: an archived binary that cannot be executed
            # is not a usable baseline, and a hash alone would not show that.
            shutil.copy2(binary, archived)
            lines = ldd_record(binary)
            (archive / "ldd").mkdir(exist_ok=True)
            (archive / "ldd" / f"{variant}--{name}.txt").write_text("\n".join(lines) + "\n")
            entries.append({
                "name": name,
                "variant": variant,
                "bytes": archived.stat().st_size,
                "sha256": sha256_file(archived),
                "live_sha256": sha256_file(binary),
                "mode": oct(archived.stat().st_mode & 0o777),
                "executable": os.access(archived, os.X_OK),
                "ldd": lines,
                "loaded": loaded_libraries(lines),
                "sanitizer": sanitizer_runtime(binary, loaded_libraries(lines))
                              if variant.startswith("sanitize") else None,
            })
        variants[variant] = entries

    # The bytes that actually ran: hash every shared object the loader resolved.
    libraries = {}
    for entries in variants.values():
        for entry in entries:
            for soname, path in entry["loaded"].items():
                if soname in libraries:
                    continue
                record = {"soname": soname, "path": path}
                if pathlib.Path(path).is_file():
                    record["sha256"] = sha256_file(path)
                    record["bytes"] = pathlib.Path(path).stat().st_size
                libraries[soname] = record

    sources = source_snapshot(archive / "sources")

    manifest = {
        "schema": 2,
        "label": label,
        "git": {
            "head": git("rev-parse", "HEAD"),
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "status_porcelain": git("status", "--porcelain=v1", "-uall"),
        },
        "compiler": {
            "sha256": sha256_file(goose),
            "bytes": goose.stat().st_size,
            "version": first_line([str(goose), "--version"]),
        },
        "tools": {
            "cc": first_line(["cc", "--version"]),
            "clang": first_line(["clang", "--version"]),
            "gcc": first_line(["gcc", "--version"]),
            "python": platform.python_version(),
            "zlib_pkg_config": first_line(["pkg-config", "--modversion", "zlib"]),
            "libdeflate_pkg_config": first_line(["pkg-config", "--modversion",
                                                 "libdeflate"]),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
        },
        "sources": sources,
        "variants": variants,
        "libraries": libraries,
    }
    (archive / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    lines = []
    for record in sources:
        lines.append(f"{record['sha256']}  sources/{record['path']}")
    for variant, entries in variants.items():
        for entry in entries:
            lines.append(f"{entry['sha256']}  variants/{variant}/{entry['name']}")
    (archive / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return archive, manifest


def verify(archive):
    """Re-hashes the archive, then reports live drift separately."""
    manifest_path = archive / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"no manifest in {archive}")
    manifest = json.loads(manifest_path.read_text())
    problems = []

    for record in manifest["sources"]:
        path = archive / "sources" / record["path"]
        if not path.is_file():
            problems.append(f"missing archived source: {record['path']}")
        elif sha256_file(path) != record["sha256"]:
            problems.append(f"archived source changed: {record['path']}")

    for variant, entries in manifest["variants"].items():
        for entry in entries:
            path = archive / "variants" / variant / entry["name"]
            if not path.is_file():
                problems.append(f"missing archived binary: {variant}/{entry['name']}")
            elif sha256_file(path) != entry["sha256"]:
                problems.append(f"archived binary changed: {variant}/{entry['name']}")
            elif not os.access(path, os.X_OK):
                problems.append(f"archived binary is not executable: {variant}/{entry['name']}")

    # The live tree is a different question from the archive, and is reported
    # as such: a rebuilt binary is drift, not a corrupt archive.
    live = []
    for variant, entries in manifest["variants"].items():
        for entry in entries:
            path = VARIANTS_ROOT / manifest["label"] / variant / entry["name"]
            expected = entry.get("live_sha256", entry["sha256"])
            if not path.is_file():
                live.append((f"{variant}/{entry['name']}", expected, "ABSENT"))
            elif sha256_file(path) != expected:
                live.append((f"{variant}/{entry['name']}", expected,
                             sha256_file(path)))
    return manifest, problems, live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--label", help="archive identity; must not already exist")
    parser.add_argument("--verify", type=pathlib.Path,
                        help="verify an existing archive directory")
    arguments = parser.parse_args()

    if arguments.verify:
        manifest, problems, live = verify(arguments.verify)
        print(f"archive {manifest['label']} at git {manifest['git']['head']}")
        print(f"  {len(manifest['sources'])} archived sources, "
              f"{sum(len(v) for v in manifest['variants'].values())} archived binaries")
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        if live:
            print("live variant drift (rebuilt bytes, archive unaffected):")
            for name, recorded, current in live:
                print(f"  DRIFT {name}: recorded {recorded[:16]} current {current[:16]}")
        else:
            print("live variants match the archive")
        if problems:
            raise SystemExit(f"archive verification failed: {len(problems)} problems")
        print("archive verified")
        return 0

    if not arguments.label:
        parser.error("--label is required when freezing")
    goose = pathlib.Path(arguments.goose)
    if not goose.is_absolute():
        goose = (ROOT / goose).resolve()
    if not goose.is_file():
        raise SystemExit(f"no goose binary at {goose}")

    archive, manifest = freeze(goose, arguments.label)
    print(f"archived {manifest['label']} to {archive}")
    print(f"  git {manifest['git']['head']} on {manifest['git']['branch']}")
    print(f"  compiler {manifest['compiler']['sha256']} ({manifest['compiler']['version']})")
    print(f"  {len(manifest['sources'])} sources")
    for variant, entries in manifest["variants"].items():
        print(f"  {variant}: {len(entries)} binaries")
        for entry in entries:
            zlib = entry["loaded"].get("libz.so.1", "unresolved")
            runtime = entry.get("sanitizer")
            proof = ""
            if runtime:
                proof = (f"  asan={runtime['asan']} ubsan={runtime['ubsan']}"
                         f" ({runtime['linkage']})")
            print(f"    {entry['name']:26s} {entry['sha256'][:16]}  zlib={zlib}{proof}")
    print("  loaded libraries:")
    for soname, record in sorted(manifest["libraries"].items()):
        print(f"    {soname:22s} {record.get('sha256', 'unhashed')}  {record['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
