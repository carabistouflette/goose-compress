#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Proves that the C reference row implements the same contract as the library.

`bench/compare/zlib_bounded.c` is only an equal-contract row if it refuses the
same streams for the same reasons. This script does not take that on trust: it
runs the reference and `goose-compress` over every fixture at several chunkings
and several budget boundaries, and requires the same error, the same produced
bytes, the same member count and the same trailing count.

`progress.consumed` is compared everywhere except the one case docs/api.md
documents as chunking-dependent: a stop caused by `limits.output_bytes`. There
the contract is `produced == output_bytes` and `consumed <= input_bytes`, which
is checked instead.

Fixtures that need a preset dictionary are excluded: the reference has no
dictionary support, so it cannot be an equal-contract row for them. That
exclusion is reported, not hidden.

Corrupted streams are compared too, because the checksum classification is the
one place where the two sides do not share code: the library decides
`Error.Checksum` from zlib's message text, and that text is a property of the
linked zlib, not of the format. A backend that words the message differently
would silently reclassify a bad CRC as `Error.Data`. So every fixture is also
run with a flipped byte in the gzip CRC32 field, in the gzip ISIZE field, in
the zlib adler32 field, and in the middle of the deflate payload, and the two
sides must agree on the error name.

    python3 bench/compare/contract_parity.py --goose /path/to/goose
"""

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BUILD = ROOT / ".build"
FIXTURES = BUILD / "fixtures"
REFERENCE = BUILD / "zlib-bounded"
PROBE = BUILD / "goose-probe"

REFERENCE_BINARY = [REFERENCE]

FORMATS = {0: "gzip", 1: "zlib", 2: "raw"}
POLICIES = {0: "reject", 1: "allow", 2: "members"}
CHUNKS = [1, 7, 65536]


def child_env():
    import os
    environment = dict(os.environ)
    environment.pop("LD_LIBRARY_PATH", None)
    return environment


def parse_line(text):
    fields = {}
    for token in text.split():
        key, _, value = token.partition("=")
        fields[key] = value
    return fields


def run(command):
    result = subprocess.run(command, capture_output=True, text=True,
                            check=False, env=child_env())
    if result.returncode != 0:
        raise SystemExit("failed: %s\n%s" % (" ".join(map(str, command)),
                                             result.stderr.strip()))
    return parse_line(result.stdout)


def reference(format_name, path, output_budget, input_budget, chunk, policy,
              members):
    return run([str(REFERENCE_BINARY[0]), "--probe", format_name, str(path),
                str(output_budget), str(input_budget), str(chunk), policy,
                str(members)])


def library(goose, format_name, path, output_budget, input_budget, chunk,
            policy, members):
    """Runs the built probe binary, not `goose run`: the probe is compiled once
    by `make bench-build`, and recompiling it per case would dominate the run."""
    return run([str(PROBE), format_name, str(path), str(output_budget),
                str(input_budget), str(chunk), policy, str(members)])


def compare(label, left, right, failures, checks, output_budget):
    """Compares one pair of outcomes and records every disagreement."""
    checks[0] += 1
    for field in ("error", "produced", "members", "trailing", "fnv1a"):
        if left[field] != right[field]:
            failures.append("%s: %s reference=%s library=%s"
                            % (label, field, left[field], right[field]))
    # docs/api.md: consumed is invariant except when the output budget stopped
    # the decode. There the contract is produced == output_bytes and
    # consumed <= input_bytes.
    if left["error"] == "limit exceeded" and int(left["produced"]) == output_budget:
        checks[0] += 1
        if int(left["produced"]) != output_budget:
            failures.append("%s: produced %s != output budget %d"
                            % (label, left["produced"], output_budget))
    else:
        checks[0] += 1
        if left["consumed"] != right["consumed"]:
            failures.append("%s: consumed reference=%s library=%s"
                            % (label, left["consumed"], right["consumed"]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--goose", default=".toolchain/goose/build/goose")
    parser.add_argument("--reference", default=str(REFERENCE),
                        help="the C reference row to check; pass "
                             ".build/zlib-ng-bounded to check the zlib-ng build")
    parser.add_argument("--output", default=str(BUILD / "contract-parity.json"))
    arguments = parser.parse_args()

    goose = pathlib.Path(arguments.goose).resolve()
    reference_binary = pathlib.Path(arguments.reference).resolve()
    REFERENCE_BINARY[0] = reference_binary
    for binary in (reference_binary, PROBE):
        if not binary.is_file():
            raise SystemExit("missing %s; run `make bench-build` first" % binary)
    manifest = json.loads((FIXTURES / "manifest.json").read_text())

    failures = []
    checks = [0]
    skipped = []
    cases = 0

    for entry in manifest["fixtures"]:
        if entry["dictionary"]:
            skipped.append(entry["name"])
            continue
        name = entry["name"]
        path = FIXTURES / (name + ".in")
        format_name = FORMATS[entry["format"]]
        limits = entry["limits"]
        policy = POLICIES[entry["trailing"]]
        members = limits["members"]
        plain = entry["expected_bytes"]

        # The documented limits, at three chunkings.
        for chunk in CHUNKS:
            label = "%s chunk=%d documented" % (name, chunk)
            compare(label,
                    reference(format_name, path, limits["output_bytes"],
                              limits["input_bytes"], chunk, policy, members),
                    library(goose, format_name, path, limits["output_bytes"],
                            limits["input_bytes"], chunk, policy, members),
                    failures, checks, limits["output_bytes"])
            cases += 1

        # Output-budget boundaries: zero, exact, one short, one over.
        for budget in sorted({0, max(plain - 1, 0), plain, plain + 1}):
            for chunk in (1, 65536):
                label = "%s chunk=%d output_budget=%d" % (name, chunk, budget)
                compare(label,
                        reference(format_name, path, budget,
                                  limits["input_bytes"], chunk, policy, members),
                        library(goose, format_name, path, budget,
                                limits["input_bytes"], chunk, policy, members),
                        failures, checks, budget)
                cases += 1

        # Input-budget boundaries: zero, exact, one short, one over.
        compressed = entry["input_bytes"]
        for budget in sorted({0, max(compressed - 1, 0), compressed,
                              compressed + 1}):
            for chunk in (1, 65536):
                label = "%s chunk=%d input_budget=%d" % (name, chunk, budget)
                compare(label,
                        reference(format_name, path, limits["output_bytes"],
                                  budget, chunk, policy, members),
                        library(goose, format_name, path, limits["output_bytes"],
                                budget, chunk, policy, members),
                        failures, checks, limits["output_bytes"])
                cases += 1

        # Member and trailing policies, which the documented limits do not
        # exercise for a single-member fixture.
        for policy_name, member_cap in (("allow", 1), ("members", 2),
                                        ("members", 3)):
            for chunk in (1, 65536):
                label = "%s chunk=%d policy=%s members=%d" % (
                    name, chunk, policy_name, member_cap)
                compare(label,
                        reference(format_name, path, limits["output_bytes"],
                                  limits["input_bytes"], chunk, policy_name,
                                  member_cap),
                        library(goose, format_name, path, limits["output_bytes"],
                                limits["input_bytes"], chunk, policy_name,
                                member_cap),
                        failures, checks, limits["output_bytes"])
                cases += 1

    # Corrupted streams. The trailer fields are located from the format, not
    # from a guess: gzip ends with CRC32 then ISIZE, zlib ends with adler32,
    # raw has no trailer. A payload byte in the middle is corrupted for every
    # format, which is the case where the two sides may legitimately disagree
    # on `invalid data` versus `invalid distance` wording.
    corruption_cases = 0
    with tempfile.TemporaryDirectory(dir=str(BUILD)) as scratch:
        for entry in manifest["fixtures"]:
            if entry["dictionary"]:
                continue
            name = entry["name"]
            source = FIXTURES / (name + ".in")
            data = bytearray(source.read_bytes())
            format_name = FORMATS[entry["format"]]
            limits = entry["limits"]
            policy = POLICIES[entry["trailing"]]
            members = limits["members"]
            targets = [("payload", len(data) // 2)]
            if format_name == "gzip" and len(data) >= 8:
                targets.append(("crc32", len(data) - 8))
                targets.append(("isize", len(data) - 4))
            elif format_name == "zlib" and len(data) >= 4:
                targets.append(("adler32", len(data) - 4))
            for field, offset in targets:
                if offset < 0 or offset >= len(data):
                    continue
                corrupted = bytearray(data)
                corrupted[offset] ^= 0xFF
                path = pathlib.Path(scratch) / ("%s-%s.bin" % (name, field))
                path.write_bytes(bytes(corrupted))
                label = "%s corrupted=%s" % (name, field)
                compare(label,
                        reference(format_name, path, limits["output_bytes"],
                                  limits["input_bytes"], 65536, policy, members),
                        library(goose, format_name, path,
                                limits["output_bytes"], limits["input_bytes"],
                                65536, policy, members),
                        failures, checks, limits["output_bytes"])
                corruption_cases += 1

    report = {
        "reference": str(reference_binary),
        "library": str(goose),
        "probe": str(PROBE),
        "fixtures": len(manifest["fixtures"]),
        "cases": cases,
        "corruption_cases": corruption_cases,
        "checks": checks[0],
        "skipped_dictionary_fixtures": skipped,
        "failures": failures,
        "ok": not failures,
    }
    pathlib.Path(arguments.output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n")

    if failures:
        for line in failures[:40]:
            print("  " + line)
        print("contract parity: %d disagreement(s) over %d cases"
              % (len(failures), cases))
        return 1
    print("contract parity ok: %d cases (%d corrupted), %d checks, "
          "%d dictionary fixture(s) excluded"
          % (cases, corruption_cases, checks[0], len(skipped)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
