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
           tests/differential.goose examples/artifact_payload.goose

.PHONY: setup format check fixtures corpus native test sanitize sanitize-negative \
        bench bench-build bench-run baseline baseline-verify clean

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
	"$(GOOSE)" check tests/differential.goose
	"$(GOOSE)" check examples/artifact_payload.goose
	"$(GOOSE)" check bench/bench.goose

fixtures:
	python3 scripts/fixtures.py

corpus:
	python3 scripts/corpus.py --cases $(CASES) --seed $(SEED)

.build/driver: $(NATIVE) native/compress.h tests/driver.c
	mkdir -p .build
	$(CC) $(CFLAGS) -O2 -Inative tests/driver.c $(NATIVE) -lz -lpthread -o $@

native: .build/driver
	./.build/driver --selftest

test: fixtures corpus native
	"$(GOOSE)" run tests/spec.goose
	"$(GOOSE)" run tests/boundaries.goose
	"$(GOOSE)" run tests/api.goose
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
	@for target in spec boundaries api differential; do \
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
bench: bench-build

bench-build: fixtures
	mkdir -p .build
	"$(GOOSE)" build bench/bench.goose --cc $(BENCH_CC) -O2 -o .build/bench
	$(BENCH_CC) $(CFLAGS) -O2 bench/compare/zlib_oneshot.c -lz -o .build/zlib-oneshot
	$(BENCH_CC) $(CFLAGS) -O2 bench/compare/libdeflate_oneshot.c -ldeflate \
		-o .build/libdeflate-oneshot

bench-run: bench-build
	python3 bench/measure.py --goose "$(GOOSE)" --cc $(BENCH_CC) \
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
