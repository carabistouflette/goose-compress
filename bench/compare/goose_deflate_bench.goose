// SPDX-License-Identifier: Apache-2.0
// A harness for Macho0x/goose-deflate 0.2.3, commit
// 3b1d0d38be908dbb9bc2e26411bfd7449c570147, at its own scope.
//
// That package decodes a whole gzip member from a buffer the caller already
// holds and returns `(bytes, ok)`. It has no output budget, no input budget,
// no member policy, no trailing policy, no incremental state and no error
// taxonomy, so this row is NOT comparable to goose-compress on contract. It is
// measured because it is the only other Goose decoder in the catalog, and a
// comparison that omits it would be a comparison against nothing.
//
// The fourth argument is accepted and ignored: there is no budget to apply.
// The row is labelled `comparable: false` in bench/runner.py for that reason.
//
//   goose build bench/compare/goose_deflate_bench.goose -O2 \
//       -o .build/goose-deflate-bench
//   .build/goose-deflate-bench input.gz 200 16777216
//
// The package is fetched at its pinned commit into .build/competitors/ by
// `make bench-competitors`; it is not vendored into this repository.
import deflate from "../../.build/competitors/goose-deflate";
import deflate.gzip;
import os;
import std;

fn fnv1a(data: const u8[:]) -> u64 {
    var hash: u64 = 14695981039346656037;
    for i in data.len {
        hash = hash ^ data[i];
        hash = hash * 1099511628211;
    }
    return hash;
}

fn parse_i64(text: const u8[:]) -> i64 {
    if text.len == 0 || text.len > 18 { return -1; }
    var value: i64 = 0;
    for i in text.len {
        if text[i] < '0' || text[i] > '9' { return -1; }
        value = value * 10 + (text[i] - '0');
    }
    return value;
}

fn peak_rss_kb() -> i64 {
    var status: u8[>..] = [];
    if !read_file("/proc/self/status", status) { return -1; }
    let needle = "VmHWM:";
    var i: i64 = 0;
    while i + needle.len <= status.len {
        var matched = true;
        for j in needle.len {
            if status[i + j] != needle[j] {
                matched = false;
                break;
            }
        }
        if matched {
            var k = i + needle.len;
            while k < status.len && (status[k] == ' ' || status[k] == '\t') { k += 1; }
            var value: i64 = 0;
            while k < status.len && status[k] >= '0' && status[k] <= '9' {
                value = value * 10 + (status[k] - '0');
                k += 1;
            }
            return value;
        }
        i += 1;
    }
    return -1;
}

fn main() {
    let argv = args();
    if argv.len != 4 {
        write_stderr("usage: goose-deflate-bench <input.gz> <iterations> <budget>\n");
        exit(2);
    }
    let iterations = parse_i64(argv[2]);
    if iterations < 1 {
        write_stderr("goose-deflate-bench: bad iteration count\n");
        exit(2);
    }
    var input: u8[>..] = [];
    if !read_file(argv[1], input) {
        write_stderr(str("goose-deflate-bench: cannot read ", argv[1], "\n"));
        exit(1);
    }

    // One warm-up call, outside the timed loop.
    let warm, warm_ok = gzip::decompress(input[..]);
    if !warm_ok {
        write_stderr("goose-deflate-bench: warm-up rejected the stream\n");
        exit(1);
    }
    let digest = fnv1a(warm[..]);
    let produced = warm.len;

    let start = clock_ns();
    var error = false;
    for _ in iterations {
        let out, ok = gzip::decompress(input[..]);
        if !ok || out.len != produced { error = true; break; }
    }
    let elapsed = clock_ns() - start;
    if error {
        write_stderr("goose-deflate-bench: timed run diverged\n");
        exit(1);
    }
    print("mode=whole input=", input.len, " output=", produced,
          " iterations=", iterations, " total_ns=", elapsed,
          " per_call_ns=", elapsed / iterations, " fnv1a=", digest,
          " error=none", " peak_rss_kb=", peak_rss_kb(), "\n");
}
