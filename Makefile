GOOSE ?= .toolchain/goose/build/goose
CC ?= cc
SANITIZER_CC ?= clang
BENCH_CC ?= cc
CASES ?= 100000
SEED ?= 20261107

NATIVE = native/compress.c
CFLAGS = -std=c11 -Wall -Wextra -Werror
# Real instrumentation needs the flags on the compiler that builds the Goose
# translation unit. `goose build --cc clang --debug` does not do that, so the
# sanitize target emits standalone C and compiles it here, together with the
# native adapter, through scripts/sanitizer-cc.sh.
SANITIZER = SANITIZER_CC=$(SANITIZER_CC) ./scripts/sanitizer-cc.sh
SANITIZE_FLAGS = -O1 -g -Inative
SANITIZE_LIBS = -lz -lpthread -lm -ldl
SANITIZE_ENV = ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 UBSAN_OPTIONS=halt_on_error=1

PROGRAMS = tests/spec.goose tests/boundaries.goose tests/api.goose \
           tests/contract.goose tests/differential.goose \
           examples/artifact_payload.goose

.PHONY: setup format check fixtures corpus native concurrency concurrency-tsan \
        test sanitize sanitize-negative \
        bench bench-build bench-build-ng bench-competitors bench-zlib14 \
        bench-parity bench-plan bench-run native-ng bench-zlib-ng-compat \
        bench-zlib-ng-bounded baseline baseline-verify clean

setup:
	./scripts/setup-goose.sh

format:
	python3 scripts/native-manifest.py
	"$(GOOSE)" format compress.goose examples tests bench

check:
	python3 scripts/native-manifest.py --check
	"$(GOOSE)" format --check compress.goose examples tests bench
	"$(GOOSE)" check tests/spec.goose
	"$(GOOSE)" check tests/boundaries.goose
	"$(GOOSE)" check tests/api.goose
	"$(GOOSE)" check tests/contract.goose
	"$(GOOSE)" check tests/differential.goose
	"$(GOOSE)" check examples/artifact_payload.goose
	"$(GOOSE)" check bench/bench.goose
	"$(GOOSE)" check bench/compare/goose_probe.goose

fixtures:
	python3 scripts/fixtures.py

corpus:
	python3 scripts/corpus.py --cases $(CASES) --seed $(SEED)

.build/driver: $(NATIVE) native/compress.h tests/driver.c
	mkdir -p .build
	$(CC) $(CFLAGS) -O2 -Inative tests/driver.c $(NATIVE) -lz -lpthread -o $@

native: .build/driver
	./.build/driver --selftest

# The close-race check needs one real inflate held in flight, which the linker
# wrap provides in this binary only. The timeout is the backstop for a test that
# deadlocks: a hang must fail the run, not stall it.
CONCURRENCY_TIMEOUT ?= 120
# The close-race check wraps the inflate entry point the adapter actually calls.
# Against zlib-ng that symbol is `zng_inflate`, so the wrap target follows the
# backend instead of being hard-coded.
WRAP_INFLATE ?= inflate
CONCURRENCY_WRAP = -DGC_TEST_WRAP_INFLATE -Wl,--wrap=$(WRAP_INFLATE)

.build/concurrency: $(NATIVE) native/compress.h tests/concurrency.c
	mkdir -p .build
	$(CC) $(CFLAGS) -O2 -Inative tests/concurrency.c $(NATIVE) -lz -lpthread \
		$(CONCURRENCY_WRAP) -o $@

concurrency: .build/concurrency
	timeout $(CONCURRENCY_TIMEOUT) ./.build/concurrency --selftest

# ThreadSanitizer is the only tool that sees the registry refcount and the
# per-decoder lock. It is a separate target because it needs its own runtime.
concurrency-tsan: $(NATIVE) native/compress.h tests/concurrency.c
	mkdir -p .build
	$(CC) $(CFLAGS) -O1 -g -fsanitize=thread -Inative tests/concurrency.c \
		$(NATIVE) -lz -lpthread $(CONCURRENCY_WRAP) -o .build/concurrency-tsan
	TSAN_OPTIONS=halt_on_error=1 timeout $(CONCURRENCY_TIMEOUT) \
		./.build/concurrency-tsan --selftest

test: fixtures corpus native concurrency
	"$(GOOSE)" run tests/spec.goose
	"$(GOOSE)" run tests/boundaries.goose
	"$(GOOSE)" run tests/api.goose
	"$(GOOSE)" run tests/contract.goose
	"$(GOOSE)" run tests/differential.goose
	"$(GOOSE)" build tests/api.goose --cc $(CC) -O2 -o .build/api
	./.build/api
	"$(GOOSE)" build tests/api.goose --cc $(CC) --debug -o .build/api-debug
	./.build/api-debug
	python3 -m unittest discover -s tests -v
	python3 scripts/example_check.py "$(GOOSE)"

sanitize: fixtures
	mkdir -p .build
	$(SANITIZER) $(SANITIZE_FLAGS) -o .build/driver-sanitize tests/driver.c $(NATIVE) $(SANITIZE_LIBS)
	$(SANITIZE_ENV) ./.build/driver-sanitize --selftest
	$(SANITIZER) $(SANITIZE_FLAGS) -o .build/concurrency-sanitize \
		tests/concurrency.c $(NATIVE) $(SANITIZE_LIBS) $(CONCURRENCY_WRAP)
	$(SANITIZE_ENV) timeout $(CONCURRENCY_TIMEOUT) \
		./.build/concurrency-sanitize --selftest
	@for target in spec boundaries api contract differential; do \
		echo "sanitize: $$target"; \
		"$(GOOSE)" tests/$$target.goose --standalone --debug --no-bce \
			-o .build/$$target-sanitized.c || exit $$?; \
		$(SANITIZER) $(SANITIZE_FLAGS) -o .build/$$target-sanitized \
			.build/$$target-sanitized.c $(NATIVE) $(SANITIZE_LIBS) || exit $$?; \
		$(SANITIZE_ENV) ./.build/$$target-sanitized || exit $$?; \
	done
	python3 scripts/sanitize_negative.py --goose "$(GOOSE)" --cc $(SANITIZER_CC)

# Proves the sanitizer runs are instrumented rather than merely debug builds.
sanitize-negative:
	python3 scripts/sanitize_negative.py --goose "$(GOOSE)" --cc $(SANITIZER_CC)

# Building the benchmark is not a measurement. `bench-run` is, and it is
# serialised: run it alone, on an idle machine.
#
# `bench-build` compiles only what the distribution provides: zlib and
# libdeflate. Competitor rows that need a library the distribution does not
# ship are separate opt-in targets (`bench-zlib14`, `bench-zlib-ng-bounded`,
# `bench-zlib-ng-compat`, `bench-competitors`), and the runner names their
# absence as a skipped row instead of dropping it. zlib-ng in particular has no
# ubuntu-24.04 package, so requiring a system `-lz-ng` here made `make bench`
# fail on a clean runner.
bench: bench-build

bench-build: fixtures
	mkdir -p .build
	"$(GOOSE)" build bench/bench.goose --cc $(BENCH_CC) -O2 -o .build/bench
	"$(GOOSE)" build bench/compare/goose_probe.goose --cc $(BENCH_CC) -O2 \
		-o .build/goose-probe
	$(BENCH_CC) $(CFLAGS) -O2 bench/compare/zlib_bounded.c -lz -o .build/zlib-bounded
	$(BENCH_CC) $(CFLAGS) -O2 bench/compare/zlib_oneshot.c -lz -o .build/zlib-oneshot
	$(BENCH_CC) $(CFLAGS) -O2 bench/compare/libdeflate_oneshot.c -ldeflate \
		-o .build/libdeflate-oneshot
	$(BENCH_CC) $(CFLAGS) -O2 -Inative bench/compare/goose_adapter.c $(NATIVE) \
		-lz -lpthread -o .build/goose-adapter

# The same adapter against zlib-ng. This is the only way to time the backend
# under our own API: `goose build -D` reaches the generated program, not the
# package's native C, so the Goose-level binary cannot be switched this way.
bench-build-ng:
	mkdir -p .build
	$(BENCH_CC) $(CFLAGS) -O2 -DGC_ZLIB_NG -Inative \
		bench/compare/goose_adapter.c $(NATIVE) -lz-ng -lz -lpthread \
		-o .build/goose-adapter-ng
	./.build/goose-adapter-ng .build/bench-inputs/1KiB-text.gz 10 16777216

# The adapter and the concurrency suite against zlib-ng, with the same guards
# and the same tests. The driver links both libraries because it builds its own
# fixtures with zlib's deflate.
native-ng:
	$(CC) $(CFLAGS) -O2 -DGC_ZLIB_NG -Inative tests/driver.c $(NATIVE) \
		-lz-ng -lz -lpthread -o .build/driver-ng
	./.build/driver-ng --selftest
	$(CC) $(CFLAGS) -O2 -DGC_ZLIB_NG -Inative tests/concurrency.c $(NATIVE) \
		-lz-ng -lz -lpthread $(CONCURRENCY_WRAP) -o .build/concurrency-ng
	timeout $(CONCURRENCY_TIMEOUT) ./.build/concurrency-ng --selftest

# The only other Goose decoder in the catalog, fetched at its pinned commit
# rather than vendored. Network access is required, so this is opt-in and the
# row is optional in the runner.
GOOSE_DEFLATE_REVISION ?= 3b1d0d38be908dbb9bc2e26411bfd7449c570147
bench-competitors:
	mkdir -p .build/competitors
	if [ ! -d .build/competitors/goose-deflate ]; then \
		git clone -q https://github.com/Macho0x/goose-deflate.git \
			.build/competitors/goose-deflate && \
		git -C .build/competitors/goose-deflate checkout -q \
			"$(GOOSE_DEFLATE_REVISION)"; \
	fi
	"$(GOOSE)" build bench/compare/goose_deflate_bench.goose --cc $(BENCH_CC) -O2 \
		-o .build/goose-deflate-bench

# zlib 1.4 (4 Nov 2026) is newer than the distribution's 1.3.2, and it changes
# inflate behaviour this package depends on: raw deflate that completes exactly
# at the end of the output buffer now returns Z_STREAM_END instead of Z_OK, and
# it fixes total_in after Z_NEED_DICT plus bad deflateSetDictionary() calls.
# It is built static into .build/ so the system library is never replaced.
ZLIB14_VERSION ?= 1.4
ZLIB14_SHA256 ?= 02dd159783677a749c04adbdd02b55b8a2bcc7e054f51b76b549199a916ad584
ZLIB14_TARBALL ?= .build/competitors/zlib-$(ZLIB14_VERSION).tar.gz
ZLIB14_DIR ?= .build/competitors/zlib-$(ZLIB14_VERSION)
bench-zlib14:
	mkdir -p .build/competitors
	if [ ! -f "$(ZLIB14_TARBALL)" ]; then \
		curl -fsSL -o "$(ZLIB14_TARBALL)" \
			https://zlib.net/zlib-$(ZLIB14_VERSION).tar.gz; \
	fi
	echo "$(ZLIB14_SHA256)  $(ZLIB14_TARBALL)" | sha256sum -c -
	rm -rf "$(ZLIB14_DIR)"
	mkdir -p "$(ZLIB14_DIR)"
	tar xzf "$(ZLIB14_TARBALL)" -C "$(ZLIB14_DIR)" --strip-components=1
	cd "$(ZLIB14_DIR)" && CFLAGS="-O2 -fPIC" ./configure --static >/dev/null \
		&& $(MAKE) -j4 libz.a >/dev/null
	$(BENCH_CC) $(CFLAGS) -O2 -I"$(ZLIB14_DIR)" bench/compare/zlib_bounded.c \
		"$(ZLIB14_DIR)/libz.a" -o .build/zlib14-bounded
	./.build/zlib14-bounded --clamp-selftest

# zlib-ng built with -DZLIB_COMPAT: it installs a libz.so.1 with zlib's ABI, so
# the unchanged Goose binary can be run on it through LD_LIBRARY_PATH. Nothing
# on the system is replaced; the build is isolated under .build/competitors.
ZLIB_NG_VERSION ?= 2.3.3
ZLIB_NG_TARBALL ?= .build/competitors/zlib-ng-$(ZLIB_NG_VERSION).tar.gz
ZLIB_NG_SHA256 ?= f9c65aa9c852eb8255b636fd9f07ce1c406f061ec19a2e7d508b318ca0c907d1
ZLIB_NG_DIR ?= .build/competitors/zlib-ng-$(ZLIB_NG_VERSION)
ZLIB_NG_COMPAT ?= .build/competitors/zlib-ng-compat
ZLIB_NG_NATIVE_DIR ?= .build/competitors/zlib-ng-$(ZLIB_NG_VERSION)-native
ZLIB_NG_NATIVE ?= .build/competitors/zlib-ng-native

# The `zng_` comparator row, built from the same pinned tarball as the compat
# backend instead of from a system `-lz-ng`. zlib-ng ships no ubuntu-24.04
# package, so a system dependency here is not reproducible; the tarball is
# hash-verified and the install prefix stays under .build/competitors. This is
# opt-in, and the runner reports the row as skipped when the binary is absent.
bench-zlib-ng-bounded:
	mkdir -p .build/competitors
	if [ ! -f "$(ZLIB_NG_TARBALL)" ]; then \
		curl -fsSL -o "$(ZLIB_NG_TARBALL)" \
			https://github.com/zlib-ng/zlib-ng/archive/refs/tags/$(ZLIB_NG_VERSION).tar.gz; \
	fi
	echo "$(ZLIB_NG_SHA256)  $(ZLIB_NG_TARBALL)" | sha256sum -c -
	rm -rf "$(ZLIB_NG_NATIVE_DIR)" "$(ZLIB_NG_NATIVE)"
	mkdir -p "$(ZLIB_NG_NATIVE_DIR)"
	tar xzf "$(ZLIB_NG_TARBALL)" -C "$(ZLIB_NG_NATIVE_DIR)" --strip-components=1
	cd "$(ZLIB_NG_NATIVE_DIR)" && ./configure \
		--prefix="$(CURDIR)/$(ZLIB_NG_NATIVE)" >/dev/null && $(MAKE) -j4 >/dev/null \
		&& $(MAKE) install >/dev/null
	$(BENCH_CC) $(CFLAGS) -O2 -DZB_ZLIB_NG -I"$(ZLIB_NG_NATIVE)/include" \
		bench/compare/zlib_bounded.c -L"$(ZLIB_NG_NATIVE)/lib" -lz-ng \
		-Wl,-rpath,"$(CURDIR)/$(ZLIB_NG_NATIVE)/lib" -o .build/zlib-ng-bounded
	./.build/zlib-ng-bounded --clamp-selftest
	@echo "zng_ backend: $(CURDIR)/$(ZLIB_NG_NATIVE)/lib/libz-ng.so"

bench-zlib-ng-compat:
	mkdir -p .build/competitors
	if [ ! -f "$(ZLIB_NG_TARBALL)" ]; then \
		curl -fsSL -o "$(ZLIB_NG_TARBALL)" \
			https://github.com/zlib-ng/zlib-ng/archive/refs/tags/$(ZLIB_NG_VERSION).tar.gz; \
	fi
	echo "$(ZLIB_NG_SHA256)  $(ZLIB_NG_TARBALL)" | sha256sum -c -
	rm -rf "$(ZLIB_NG_DIR)" "$(ZLIB_NG_COMPAT)"
	mkdir -p "$(ZLIB_NG_DIR)"
	tar xzf "$(ZLIB_NG_TARBALL)" -C "$(ZLIB_NG_DIR)" --strip-components=1
	cd "$(ZLIB_NG_DIR)" && ./configure --zlib-compat \
		--prefix="$(CURDIR)/$(ZLIB_NG_COMPAT)" >/dev/null && $(MAKE) -j4 >/dev/null \
		&& $(MAKE) install >/dev/null
	$(BENCH_CC) $(CFLAGS) -O2 -I"$(ZLIB_NG_COMPAT)/include" \
		bench/compare/zlib_bounded.c -L"$(ZLIB_NG_COMPAT)/lib" -lz \
		-Wl,-rpath,"$(CURDIR)/$(ZLIB_NG_COMPAT)/lib" \
		-o .build/zlib-ng-compat-bounded
	LD_LIBRARY_PATH="$(CURDIR)/$(ZLIB_NG_COMPAT)/lib" \
		./.build/zlib-ng-compat-bounded --clamp-selftest
	@echo "compat backend: $(CURDIR)/$(ZLIB_NG_COMPAT)/lib/libz.so.1"

# Proves the C reference row implements the same contract as the library before
# any number from it is used to rank anything.
bench-parity: bench-build
	python3 bench/compare/contract_parity.py --goose "$(GOOSE)"
	./.build/zlib-bounded --clamp-selftest

# Prints the exact commands, the input set and the call count. Times nothing.
bench-plan: bench-build
	python3 bench/runner.py --plan

# `bench-run` always takes an exclusive lock, because a measurement is only
# meaningful if no other CPU job is running. Override BENCH_LOCK to share one
# lock file with the other jobs on the machine; set BENCH_LOCK= to opt out.
BENCH_LOCK ?= /tmp/goose-compress-bench.lock
bench-run: bench-build
	python3 bench/runner.py --goose "$(GOOSE)" --cc $(BENCH_CC) \
		$(if $(BENCH_LOCK),--lock-file "$(BENCH_LOCK)",) \
		--output .build/benchmark.json

# Freezes the identity of the tree before the runtime is touched. Each variant
# is compiled into its own directory and archived once, so a later build cannot
# overwrite the bytes a hash describes. BASELINE_LABEL must be new every time.
BASELINE_LABEL ?= baseline
baseline:
	python3 scripts/baseline-manifest.py --goose "$(GOOSE)"
	python3 scripts/baseline-archive.py --goose "$(GOOSE)" --label "$(BASELINE_LABEL)"
	python3 scripts/baseline-status.py

baseline-verify:
	python3 scripts/baseline-manifest.py --goose "$(GOOSE)" --check
	python3 scripts/baseline-archive.py --verify \
		".build/baseline/archive/$$(python3 -c 'import json;print(json.load(open(".build/baseline/STATUS.json"))["authoritative_archive"])')"
	python3 scripts/baseline-status.py --check

clean:
	rm -rf .build
