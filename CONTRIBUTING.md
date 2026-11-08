# Contributing

Keep the public API small, explicit and bounded. Applications own file and
network I/O and trust decisions. Native code stays private and minimal: it owns
a `z_stream` and reports what zlib did, nothing more. Policy, budgets and the
state machine belong in `compress.goose`.

Preserve the documented chunking rules. Error and produced bytes must stay
independent of input splits. `progress.consumed` is invariant except at an
output-budget stop, where it describes the input available to the failing step.
See `docs/api.md` and `tests/boundaries.goose`; extend those checks rather than
weakening them.

Use focused branches (`feat/...`, `fix/...`, `docs/...`) and Conventional
Commits: `feat: add ...`, `fix: reject ...`, `test: cover ...`,
`docs: clarify ...`, `chore: update ...`. Use imperative subjects and one
coherent change per commit. No stability guarantee before 1.0. Release only
after review.

## Before you send a change

```sh
make format check test
git diff --check
make sanitize SANITIZER_CC=clang
make sanitize SANITIZER_CC=gcc
```

`make sanitize` emits standalone C with `--standalone --debug --no-bce` and
compiles it through `scripts/sanitizer-cc.sh`, together with `native/compress.c`.
`goose build --cc clang --debug` alone does **not** instrument the generated
code, so do not use it as sanitizer evidence. The target ends with
`scripts/sanitize_negative.py`, which proves the instrumentation is live by
injecting a heap overflow and requiring the process to abort. If that check
fails, the sanitizer run is meaningless.

`make format` refreshes the native source fingerprint in `compress.build`;
`make check` fails when it is stale. Run Make serially per checkout because
native cache paths are shared.

Add a public-API regression for every confirmed defect. The three defects found
during the first review are the model: an empty stream with a zero budget, a
trailer arriving in the next chunk, and trailing bytes bypassing the input
budget. Each has a fixture and a boundary assertion.

## Changing the native adapter

`native/compress.c` is the only C in the library. A change there needs:

- `make native` (130 direct C checks when the optional large mapping succeeds) and `make sanitize` with both compilers,
  including the negative control that proves the instrumentation is live;
- the 100,000-case differential corpus, which runs under the sanitizers too;
- a note in `docs/design.md` if the change alters a documented decision.

Do not add a second decompressor. The project exists to bound a maintained
zlib, not to reimplement DEFLATE.

## Changing the oracle

`scripts/oracle.py` is a second implementation of `docs/api.md`, written from
that document. If you change the contract, change the document first, then the
oracle, then the library. `tests/test_compress.py` cross-checks the oracle
against CPython's `gzip.decompress` and `zlib.decompress`, so a wrapper-parsing
mistake shows up as a disagreement rather than as a silently wrong expectation.

Regenerating fixtures or the corpus changes their recorded SHA-256. Update
`docs/validation.md` with the new values in the same commit.

## Benchmarks

Profile before optimizing. Benchmark complete public calls as well as relevant
kernels; preserve all checks. Keep raw samples, source revision, flags,
dependency versions and platform provenance. Report regressions and limits. CI
builds the benchmark and checks that it runs; it does not assert a timing
threshold. No universal speed rankings.

Run `make bench-run` alone on an idle machine. Do not run it while a build or a
test suite is running.

## Compiler changes

A compiler change requires a reviewed full upstream commit in `GOOSE_REVISION`
and complete validation. Do not point `GOOSE_REVISION` at a branch or a tag.

## Reporting

Report vulnerabilities privately using the guidance in
[SECURITY.md](SECURITY.md). Never commit compiler output, credentials or private
workspace notes.
