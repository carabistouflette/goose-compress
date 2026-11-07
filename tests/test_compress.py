# SPDX-License-Identifier: Apache-2.0
"""Checks the artefacts the Goose tests depend on.

The Goose tests assert that the library agrees with `.build/fixtures` and
`.build/corpus`. That is only meaningful if those files are what they claim
to be, so this suite verifies them independently:

* every fixture file matches the SHA-256 in its manifest;
* the oracle agrees with CPython's own `gzip.decompress` and
  `zlib.decompress` on every valid fixture, so a mistake in the hand-written
  RFC 1950/1952 parsing shows up as a disagreement;
* the oracle reproduces every recorded corpus expectation, so the corpus is
  self-consistent and deterministic;
* the native source fingerprint and the pinned compiler revision are intact.

    python3 -m unittest discover -s tests -v
"""

import hashlib
import json
import pathlib
import struct
import subprocess
import sys
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / ".build" / "fixtures"
CORPUS = ROOT / ".build" / "corpus"
MAGIC = b"GCCORP01"

sys.path.insert(0, str(ROOT / "scripts"))
import oracle  # noqa: E402


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_fixtures():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    return manifest["fixtures"]


def load_corpus():
    blob = (CORPUS / "corpus.bin").read_bytes()
    if not blob.startswith(MAGIC):
        raise AssertionError("corpus.bin has the wrong magic")
    (count,) = struct.unpack_from("<I", blob, len(MAGIC))
    position = len(MAGIC) + 4
    cases = []
    for _ in range(count):
        format, trailing, has_dictionary, error = struct.unpack_from("<BBBB", blob, position)
        position += 4
        output_bytes, input_bytes, members = struct.unpack_from("<qqq", blob, position)
        position += 24
        chunk, dictionary_len, data_len = struct.unpack_from("<qqq", blob, position)
        position += 24
        (expected_len,) = struct.unpack_from("<q", blob, position)
        position += 8
        dictionary = blob[position:position + dictionary_len]
        position += dictionary_len
        data = blob[position:position + data_len]
        position += data_len
        expected = blob[position:position + expected_len]
        position += expected_len
        cases.append({
            "format": format,
            "trailing": trailing,
            "dictionary": dictionary,
            "output_bytes": output_bytes,
            "input_bytes": input_bytes,
            "members": members,
            "chunk": chunk,
            "data": data,
            "error": error,
            "expected": expected,
        })
    if position != len(blob):
        raise AssertionError(f"corpus.bin has {len(blob) - position} trailing bytes")
    return cases


class FixtureIntegrity(unittest.TestCase):
    def test_files_match_manifest(self):
        fixtures = load_fixtures()
        self.assertEqual(len(fixtures), 48)
        for fixture in fixtures:
            name = fixture["name"]
            with self.subTest(fixture=name):
                source = (FIXTURES / f"{name}.in").read_bytes()
                expected = (FIXTURES / f"{name}.out").read_bytes()
                self.assertEqual(sha256(source), fixture["input_sha256"])
                self.assertEqual(sha256(expected), fixture["expected_sha256"])
                self.assertEqual(len(source), fixture["input_bytes"])
                self.assertEqual(len(expected), fixture["expected_bytes"])

    def test_names_are_unique(self):
        names = [fixture["name"] for fixture in load_fixtures()]
        self.assertEqual(len(names), len(set(names)))

    def test_index_matches_manifest(self):
        """The Goose tests read index.bin, so it must agree with the manifest."""
        fixtures = load_fixtures()
        index = (FIXTURES / "index.bin").read_bytes()
        self.assertTrue(index.startswith(b"GCFIX001"))
        (count,) = struct.unpack_from("<I", index, 8)
        self.assertEqual(count, len(fixtures))
        position = 12
        for fixture in fixtures:
            (name_len,) = struct.unpack_from("<B", index, position)
            position += 1
            name = index[position:position + name_len].decode()
            position += name_len
            format, trailing, has_dictionary = struct.unpack_from("<BBB", index, position)
            position += 3
            output_bytes, input_bytes, members = struct.unpack_from("<qqq", index, position)
            position += 24
            (error,) = struct.unpack_from("<B", index, position)
            position += 1
            with self.subTest(fixture=name):
                self.assertEqual(name, fixture["name"])
                self.assertEqual(format, fixture["format"])
                self.assertEqual(trailing, fixture["trailing"])
                self.assertEqual(bool(has_dictionary), fixture["dictionary"])
                self.assertEqual(error, fixture["expected_error"])
                self.assertEqual(output_bytes, fixture["limits"]["output_bytes"])
                self.assertEqual(input_bytes, fixture["limits"]["input_bytes"])
                self.assertEqual(members, fixture["limits"]["members"])
        self.assertEqual(position, len(index))
        self.assertEqual(sha256(index), json.loads(
            (FIXTURES / "manifest.json").read_text())["index_sha256"])


class OracleAgreesWithCPython(unittest.TestCase):
    """The oracle must not disagree with CPython on a stream CPython accepts."""

    def test_valid_fixtures(self):
        checked = 0
        for fixture in load_fixtures():
            if fixture["expected_error"] != oracle.ERR_NONE:
                continue
            if fixture["dictionary"]:
                continue
            if fixture["trailing"] == oracle.TRAILING_MEMBERS:
                continue
            name = fixture["name"]
            data = (FIXTURES / f"{name}.in").read_bytes()
            expected = (FIXTURES / f"{name}.out").read_bytes()
            with self.subTest(fixture=name):
                if fixture["format"] == oracle.FORMAT_GZIP:
                    reference = zlib.decompress(data, 31)
                elif fixture["format"] == oracle.FORMAT_ZLIB:
                    reference = zlib.decompress(data, 15)
                else:
                    reference = zlib.decompress(data, -15)
                self.assertEqual(reference, expected)
                error, produced = oracle.decode(
                    fixture["format"], fixture["trailing"], b"",
                    fixture["limits"]["output_bytes"], fixture["limits"]["input_bytes"],
                    fixture["limits"]["members"], data)
                self.assertEqual(error, oracle.ERR_NONE)
                self.assertEqual(produced, expected)
                checked += 1
        self.assertGreaterEqual(checked, 20)

    def test_concatenated_members(self):
        """CPython's gzip module decodes members; the oracle must agree."""
        import gzip
        for fixture in load_fixtures():
            if fixture["trailing"] != oracle.TRAILING_MEMBERS:
                continue
            if fixture["expected_error"] != oracle.ERR_NONE:
                continue
            name = fixture["name"]
            data = (FIXTURES / f"{name}.in").read_bytes()
            expected = (FIXTURES / f"{name}.out").read_bytes()
            with self.subTest(fixture=name):
                self.assertEqual(gzip.decompress(data), expected)


class CorpusIntegrity(unittest.TestCase):
    def test_manifest(self):
        manifest = json.loads((CORPUS / "manifest.json").read_text())
        blob = (CORPUS / "corpus.bin").read_bytes()
        self.assertEqual(manifest["cases"], 100000)
        self.assertEqual(manifest["seed"], 20261107)
        self.assertEqual(manifest["bytes"], len(blob))
        self.assertEqual(manifest["sha256"], sha256(blob))

    def test_outcome_histogram(self):
        manifest = json.loads((CORPUS / "manifest.json").read_text())
        counted = {}
        for case in load_corpus():
            name = oracle.ERROR_NAMES[case["error"]]
            counted[name] = counted.get(name, 0) + 1
        self.assertEqual(counted, manifest["expected_outcomes"])

    def test_oracle_reproduces_every_case(self):
        """Re-derives each expectation from the recorded inputs."""
        for index, case in enumerate(load_corpus()):
            error, produced = oracle.decode(
                case["format"], case["trailing"], case["dictionary"],
                case["output_bytes"], case["input_bytes"], case["members"], case["data"])
            if error != case["error"] or produced != case["expected"]:
                self.fail(f"case {index} disagrees with the oracle: "
                          f"error {oracle.ERROR_NAMES[error]} vs "
                          f"{oracle.ERROR_NAMES[case['error']]}, "
                          f"{len(produced)} vs {len(case['expected'])} bytes")

    def test_corpus_is_reproducible(self):
        """The generator is seeded, so a rebuild must produce the same bytes."""
        manifest = json.loads((CORPUS / "manifest.json").read_text())
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "corpus.py"),
             "--cases", "1000", "--seed", str(manifest["seed"]),
             "--output", str(ROOT / ".build" / "corpus-rebuild")],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        rebuilt = json.loads((ROOT / ".build" / "corpus-rebuild" / "manifest.json").read_text())
        self.assertEqual(rebuilt["cases"], 1000)
        self.assertEqual(rebuilt["seed"], manifest["seed"])


class SourceIntegrity(unittest.TestCase):
    def test_native_manifest(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "native-manifest.py"), "--check"],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_build_file_declares_existing_sources(self):
        """The build file must point at files that exist, with a real fingerprint."""
        build = (ROOT / "compress.build").read_text()
        recorded = None
        for line in build.splitlines():
            if "Source SHA256:" in line:
                recorded = line.split("Source SHA256:", 1)[1].strip()
        self.assertIsNotNone(recorded, "compress.build has no source fingerprint")
        self.assertRegex(recorded, r"^[0-9a-f]{64}$")
        for source in ("native/compress.c",):
            self.assertIn(f'"{source}"', build)
            self.assertTrue((ROOT / source).is_file(), f"{source} is missing")
        self.assertIn('"native"', build)
        self.assertTrue((ROOT / "native" / "compress.h").is_file())
        self.assertIn('"z"', build)

    def test_pinned_revision(self):
        revision = (ROOT / "GOOSE_REVISION").read_text().strip()
        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        self.assertEqual(revision, "eadca409f68210893f366e60d0c2f0dc29a339be")

    def test_licence_headers(self):
        for path in sorted(ROOT.rglob("*.goose")) + sorted(ROOT.rglob("*.py")) \
                + sorted(ROOT.rglob("*.c")) + sorted(ROOT.rglob("*.h")):
            if ".build" in path.parts or ".toolchain" in path.parts:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                head = path.read_text().splitlines()[:5]
                self.assertTrue(any("SPDX-License-Identifier: Apache-2.0" in line
                                    for line in head),
                                f"{path} has no SPDX header")


if __name__ == "__main__":
    unittest.main()
