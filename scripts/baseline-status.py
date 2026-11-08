#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Derives the baseline status from the files that actually exist.

The first freeze wrote `.build/baseline/manifest.json` and then a later
`make sanitize SANITIZER_CC=gcc` overwrote five of the binaries it had hashed.
The manifest is kept exactly as written: it is the record of what was true at
that moment, and editing it would destroy the evidence that the procedure was
wrong.

This script states the consequence instead. For every binary the original
manifest recorded it reports whether those bytes are still on disk, and it
lists the archive identities that were created afterwards with the reason each
one was superseded. Nothing here is hand-written: every hash is recomputed.

    python3 scripts/baseline-status.py
    python3 scripts/baseline-status.py --check   # fail if the status is stale
"""

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".build" / "baseline"
ORIGINAL = BASELINE / "manifest.json"
STATUS = BASELINE / "STATUS.json"

# Why each archive identity was superseded. The authoritative one is the last
# entry with no reason, and it is verified by `baseline-archive.py --verify`.
LINEAGE = [
    ("c99b049-v2", "incomplete: the tool built the variants but never copied "
                   "them into the archive, so it held no binaries"),
    ("c99b049-v3", "superseded: the sanitizer proof used nm only, which reports "
                   "a dynamically linked gcc runtime as uninstrumented"),
    ("c99b049-v4", "superseded: same nm-only proof, and the loaded shared "
                   "objects were not hashed"),
    ("c99b049-v5", "superseded: shutil.copyfile dropped the executable bit, so "
                   "the archived binaries could not be run"),
    ("c99b049-v6", None),
]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def derive():
    if not ORIGINAL.is_file():
        raise SystemExit(f"no original manifest at {ORIGINAL}")
    original = json.loads(ORIGINAL.read_text())

    binaries = []
    for entry in original["binaries"]:
        path = ROOT / entry["path"]
        recorded = entry.get("sha256")
        current = sha256_file(path) if path.is_file() else None
        binaries.append({
            "path": entry["path"],
            "recorded_sha256": recorded,
            "current_sha256": current,
            "retained": current is not None and current == recorded,
            "reason": None if current == recorded else
                      ("overwritten by a later build after the manifest was "
                       "frozen; the recorded bytes are not recoverable from "
                       ".build and are not claimed as retained"),
        })

    archives = []
    for label, reason in LINEAGE:
        directory = BASELINE / "archive" / label
        record = {"label": label, "present": directory.is_dir(),
                  "superseded_because": reason}
        manifest = directory / "manifest.json"
        if manifest.is_file():
            record["manifest_sha256"] = sha256_file(manifest)
            data = json.loads(manifest.read_text())
            record["binaries"] = sum(len(v) for v in data["variants"].values())
            record["sources"] = len(data["sources"])
        archives.append(record)

    authoritative = next((entry["label"] for entry in archives
                          if entry["superseded_because"] is None), None)
    return {
        "schema": 1,
        "original_manifest": {
            "path": str(ORIGINAL.relative_to(ROOT)),
            "sha256": sha256_file(ORIGINAL),
            "git_head": original["git"]["head"],
            "note": "kept verbatim; not rewritten to hide the drift",
        },
        "binaries": binaries,
        "retained": sum(1 for entry in binaries if entry["retained"]),
        "not_retained": sum(1 for entry in binaries if not entry["retained"]),
        "archives": archives,
        "authoritative_archive": authoritative,
        "environment": {
            "ld_library_path_must_be_unset": (
                "the pinned compiler and the gcc sanitizer runtime both need "
                "the system libstdc++; run every command under "
                "`env -u LD_LIBRARY_PATH`"),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()

    status = derive()
    text = json.dumps(status, indent=2, sort_keys=True) + "\n"

    if arguments.check:
        if not STATUS.is_file():
            raise SystemExit(f"no status file at {STATUS}")
        if STATUS.read_text() != text:
            raise SystemExit("baseline status is stale; re-run without --check")
        print(f"baseline status current: {status['retained']} retained, "
              f"{status['not_retained']} not retained, "
              f"authoritative archive {status['authoritative_archive']}")
        return 0

    STATUS.write_text(text)
    print(f"wrote {STATUS}")
    print(f"  original manifest {status['original_manifest']['sha256']}")
    print(f"  {status['retained']} binaries retained, "
          f"{status['not_retained']} not retained")
    for entry in status["binaries"]:
        if not entry["retained"]:
            print(f"    NOT RETAINED {entry['path']}")
    print(f"  authoritative archive: {status['authoritative_archive']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
