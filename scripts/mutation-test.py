#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Runs authentic mutants of the documented guarantees against the test suite.

Each mutant is a single semantic edit to `compress.goose` or
`native/compress.c` that breaks a guarantee the documentation promises. None of
them is a syntax error, a compile failure or an unconditional abort: every one
compiles and runs, and only a test that actually checks the guarantee can tell
it apart from the pristine tree.

The pristine tree must be green: every functional program exits zero before a
single mutant is applied. A mutant counts as killed only when it makes a
program fail or adds a diagnostic line the pristine run does not have. If the
pristine run is not green the harness stops instead of scoring mutants against
a red suite, because a mutant "killed" by an already-failing check proves
nothing about the guarantee it breaks.

Every mutant is applied to its own copy of the tree under `.build/mutation/`;
the working tree is never touched. Per mutant the report keeps the diff, the
mutated file's SHA256, the exact command, the exit code and the diagnostic.

    python3 scripts/mutation-test.py --goose /path/to/goose
    python3 scripts/mutation-test.py --goose /path/to/goose --only output-budget
"""

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / ".build" / "mutation"
SKIP = {".build", ".git", ".toolchain"}

# Each entry breaks one documented guarantee. `old` must occur exactly once.
MUTANTS = [
    {
        "name": "output-budget",
        "file": "compress.goose",
        "guarantee": "produced never exceeds limits.output_bytes",
        "old": "        if produced > remaining {\n",
        "new": "        if produced > remaining + 1 {\n",
        "why": "lets one byte past the output budget before failing",
    },
    {
        "name": "input-budget-cut",
        "file": "compress.goose",
        "guarantee": "a chunk crossing input_bytes is cut at the boundary",
        "old": "    let usable = min(input.len, allowed);\n",
        "new": "    let usable = input.len;\n",
        "why": "decodes the whole chunk, so the result depends on chunking",
    },
    {
        "name": "trailing-accepted",
        "file": "compress.goose",
        "guarantee": "Trailing.Reject refuses bytes after the last member",
        "old": "        } else {\n            return fail(decoder, Error.TrailingData);\n        }\n",
        "new": "        } else {\n            return Error.None;\n        }\n",
        "why": "silently accepts trailing data under Trailing.Reject",
    },
    {
        "name": "member-limit-off-by-one",
        "file": "compress.goose",
        "guarantee": "at most limits.members members are decoded",
        "old": "           decoder.progress.members < decoder.limits.members {\n            var reset_meta: i64[1] = [0];\n            if native_reset(decoder.handle, reset_meta[..]) != 0 {\n                return fail(decoder, to_error(reset_meta[0]));\n            }\n            decoder.progress.state = State.Active;\n",
        "new": "           decoder.progress.members <= decoder.limits.members {\n            var reset_meta: i64[1] = [0];\n            if native_reset(decoder.handle, reset_meta[..]) != 0 {\n                return fail(decoder, to_error(reset_meta[0]));\n            }\n            decoder.progress.state = State.Active;\n",
        "why": "admits one member past the documented maximum",
    },
    {
        "name": "sticky-error-broken",
        "file": "compress.goose",
        "guarantee": "a terminal error is sticky and repeated by later calls",
        "old": "    if decoder.progress.state == State.Failed { return decoder.error; }\n    if decoder.progress.state == State.Done { return Error.InvalidInput; }\n",
        "new": "    if decoder.progress.state == State.Failed { return Error.None; }\n    if decoder.progress.state == State.Done { return Error.InvalidInput; }\n",
        "why": "reports success after a terminal failure",
    },
    {
        "name": "consumed-not-accumulated",
        "file": "compress.goose",
        "guarantee": "progress.consumed counts the input the decoder accepted",
        "old": "        offset += consumed;\n        decoder.progress.consumed += consumed;\n",
        "new": "        offset += consumed;\n",
        "why": "leaves progress.consumed at zero for the whole decode",
    },
    {
        "name": "stale-handle-accepted",
        "file": "native/compress.c",
        "guarantee": "a handle from a closed decoder is rejected, not dereferenced",
        "old": "    if (!slot->decoder || slot->generation != generation) return NULL;\n",
        "new": "    if (!slot->decoder) return NULL;\n",
        "why": "drops the generation check, so a stale handle resolves",
    },
    {
        "name": "dictionary-limit-off-by-one",
        "file": "compress.goose",
        "guarantee": "a dictionary longer than 32768 bytes is refused",
        "old": "fn max_dictionary_bytes() -> i64 { return 32768; }\n",
        "new": "fn max_dictionary_bytes() -> i64 { return 32769; }\n",
        "why": "admits a 32769-byte dictionary past the documented maximum",
    },
    {
        # The adapter repeats the 32768 check, but `open_with_dictionary`
        # rejects an oversized dictionary before it calls `gc_dictionary`, so
        # no public call reaches the native branch. This mutant is equivalent
        # rather than killed: it is kept to record that the duplicate exists
        # and is unreachable, not to claim coverage of it.
        "name": "dictionary-native-duplicate",
        "file": "native/compress.c",
        "guarantee": "the adapter's own 32768-byte dictionary check",
        "old": "    if (dictionary.len > 32768) {\n",
        "new": "    if (dictionary.len > 32769) {\n",
        "why": "unreachable through the public API; defence in depth only",
        "equivalent": True,
    },
]

PROGRAMS = ["contract", "spec", "boundaries", "api"]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def child_env():
    """The pinned compiler needs the system libstdc++ that the harness
    LD_LIBRARY_PATH shadows."""
    environment = dict(os.environ)
    environment.pop("LD_LIBRARY_PATH", None)
    return environment


def copy_tree(destination):
    """Copies the tree, then the generated fixtures and corpus the tests read.

    `.build` is skipped so a mutant cannot inherit a stale binary, but the
    fixture and corpus data are inputs rather than build products, so they are
    carried over explicitly.
    """
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(ROOT, destination,
                    ignore=lambda _d, names: [n for n in names if n in SKIP],
                    symlinks=True)
    for name in ("fixtures", "corpus"):
        source = ROOT / ".build" / name
        if not source.is_dir():
            raise SystemExit("run `make fixtures corpus` first: no " + str(source))
        shutil.copytree(source, destination / ".build" / name)


def run_suite(goose, tree):
    """Runs the functional programs and returns their failure signature."""
    signature = {}
    for program in PROGRAMS:
        command = [str(goose), "run", "tests/" + program + ".goose"]
        result = subprocess.run(command, cwd=tree, capture_output=True,
                                text=True, timeout=600, env=child_env())
        lines = [line for line in result.stderr.splitlines()
                 if line.strip() and not line.startswith("building the package")]
        signature[program] = {
            "exit": result.returncode,
            "lines": lines,
            "stdout_tail": result.stdout.splitlines()[-3:],
        }
    return signature


def failure_lines(signature):
    """The set of diagnostic lines, tagged by the program that printed them."""
    return {(program, line)
            for program, run in signature.items()
            for line in run["lines"]}


def nonzero(signature):
    return {program for program, run in signature.items() if run["exit"] != 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--only", default=None,
                        help="run a single mutant by name")
    parser.add_argument("--output", default=str(WORK / "report.json"))
    arguments = parser.parse_args()

    goose = pathlib.Path(arguments.goose).resolve()
    if not goose.is_file():
        raise SystemExit("no compiler at " + str(goose))
    WORK.mkdir(parents=True, exist_ok=True)

    selected = MUTANTS
    if arguments.only:
        selected = [m for m in MUTANTS if m["name"] == arguments.only]
        if not selected:
            raise SystemExit("no mutant named " + arguments.only)

    print("pristine baseline")
    pristine_tree = WORK / "pristine"
    copy_tree(pristine_tree)
    pristine = run_suite(goose, pristine_tree)
    pristine_lines = failure_lines(pristine)
    pristine_nonzero = nonzero(pristine)
    print("  exit codes: " + ", ".join(
        "%s=%d" % (p, r["exit"]) for p, r in pristine.items()))
    print("  %d diagnostic line(s) already present" % len(pristine_lines))
    if pristine_nonzero or pristine_lines:
        # Scoring mutants against a red suite would count a mutant as killed
        # by a check that was already failing, which proves nothing about the
        # guarantee the mutant breaks. Refuse instead.
        for program, run in sorted(pristine.items()):
            for line in run["lines"]:
                print("  pristine %s: %s" % (program, line))
        raise SystemExit("the pristine tree is not green; fix it before "
                         "scoring mutants")

    report = {
        "goose": str(goose),
        "goose_sha256": sha256_file(goose),
        "runtime": {name: sha256_file(ROOT / name)
                    for name in ("compress.goose", "native/compress.c")},
        "programs": PROGRAMS,
        "pristine": {
            "green": not pristine_nonzero and not pristine_lines,
            "exit": {p: r["exit"] for p, r in pristine.items()},
            "lines": sorted("%s: %s" % pair for pair in pristine_lines),
        },
        "mutants": [],
    }

    killed = 0
    for mutant in selected:
        name = mutant["name"]
        print("mutant " + name)
        tree = WORK / name
        copy_tree(tree)
        target = tree / mutant["file"]
        text = target.read_text()
        occurrences = text.count(mutant["old"])
        if occurrences != 1:
            raise SystemExit("%s: the anchor occurs %d times in %s"
                             % (name, occurrences, mutant["file"]))
        target.write_text(text.replace(mutant["old"], mutant["new"], 1))
        mutated_sha = sha256_file(target)

        signature = run_suite(goose, tree)
        lines = failure_lines(signature)
        added = sorted("%s: %s" % pair for pair in lines - pristine_lines)
        new_nonzero = sorted(nonzero(signature) - pristine_nonzero)
        is_killed = bool(added) or bool(new_nonzero)

        diff = subprocess.run(
            ["diff", "-u", str(ROOT / mutant["file"]), str(target)],
            capture_output=True, text=True).stdout

        entry = {
            "name": name,
            "equivalent": bool(mutant.get("equivalent")),
            "file": mutant["file"],
            "guarantee": mutant["guarantee"],
            "why": mutant["why"],
            "mutated_sha256": mutated_sha,
            "command": "goose run tests/<program>.goose in " + str(tree),
            "exit": {p: r["exit"] for p, r in signature.items()},
            "added_failures": added,
            "new_nonzero": new_nonzero,
            "killed": is_killed,
            "diff": diff,
        }
        report["mutants"].append(entry)
        if mutant.get("equivalent"):
            print("  equivalent mutant: " + mutant["why"])
        elif is_killed:
            killed += 1
            print("  killed: " + (added[0] if added else
                                  "exit " + ", ".join(new_nonzero)))
        else:
            print("  SURVIVED -- the suite does not check this guarantee")

    scored = [m for m in selected if not m.get("equivalent")]
    report["killed"] = killed
    report["total"] = len(scored)
    report["all_killed"] = killed == len(scored)
    pathlib.Path(arguments.output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("%d/%d mutants killed; report at %s"
          % (killed, len(scored), arguments.output))
    return 0 if report["all_killed"] else 1


if __name__ == "__main__":
    sys.exit(main())
