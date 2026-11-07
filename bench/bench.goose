// SPDX-License-Identifier: Apache-2.0
// The library's own benchmark harness. It reports nanoseconds per call and a
// digest of the output, so bench/measure.py can prove the work was done and
// compare it with the other harnesses at the same contract.
//
//   goose build bench/bench.goose -O2 -o .build/bench
//   .build/bench oneshot input.gz 200 16777216
//   .build/bench stream  input.gz 200 16777216
//   .build/bench invalid input.gz 200 16777216
import compress from "..";
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

fn main() {
    let argv = args();
    if argv.len != 5 {
        write_stderr("usage: bench <oneshot|stream|invalid> <input.gz> <iterations> <budget>\n");
        exit(2);
    }
    let mode = argv[1];
    let iterations = parse_i64(argv[3]);
    let budget = parse_i64(argv[4]);
    if iterations < 1 || budget < 0 {
        write_stderr("bench: bad iterations or budget\n");
        exit(2);
    }
    var input: u8[>..] = [];
    if !read_file(argv[2], input) {
        write_stderr(str("bench: cannot read ", argv[2], "\n"));
        exit(1);
    }
    let limits = compress::Limits {
        output_bytes: budget,
        input_bytes:  input.len,
        members:      1,
        trailing:     compress::Trailing.Reject,
    };

    // One warm-up call, outside the timed loop.
    var output: u8[>..] = [];
    var error = compress::Error.None;
    if mode == "stream" {
        error = run_stream(input[..], limits, output);
    } else {
        error = compress::decompress(input[..], compress::Format.Gzip, limits, output);
    }
    // `invalid` inputs must fail; the exact error is reported, not assumed.
    if mode == "invalid" {
        if error == compress::Error.None {
            write_stderr("bench: invalid input decoded without an error\n");
            exit(1);
        }
    } else if error != compress::Error.None {
        write_stderr(str("bench: warm-up gave ", compress::describe(error), "\n"));
        exit(1);
    }
    let expected_error = error;
    let digest = fnv1a(output[..]);
    let produced = output.len;

    let start = clock_ns();
    for _ in iterations {
        output.clear();
        if mode == "stream" {
            error = run_stream(input[..], limits, output);
        } else {
            error = compress::decompress(input[..], compress::Format.Gzip, limits, output);
        }
        if error != expected_error { break; }
    }
    let elapsed = clock_ns() - start;
    if error != expected_error {
        write_stderr(str("bench: timed run gave ", compress::describe(error), "\n"));
        exit(1);
    }
    print("mode=", mode, " input=", input.len, " output=", produced,
          " iterations=", iterations, " total_ns=", elapsed,
          " per_call_ns=", elapsed / iterations, " fnv1a=", digest,
          " error=", compress::describe(error), "\n");
}

// The streaming path: 64 KiB steps, the same budget, a fresh decoder each
// call so that allocation and cleanup are inside the measurement.
fn run_stream(input: const u8[:], limits: const compress::Limits&,
              out: u8[>..]&) -> compress::Error {
    var decoder = compress::Decoder { .. };
    var error = compress::open(decoder, compress::Format.Gzip, limits);
    if error != compress::Error.None { return error; }
    var offset: i64 = 0;
    while offset < input.len {
        let end = min(offset + 65536, input.len);
        error = compress::feed(decoder, input[offset..end], out);
        if error != compress::Error.None { break; }
        offset = end;
    }
    if error == compress::Error.None { error = compress::finish(decoder); }
    let closed = compress::close(decoder);
    if error == compress::Error.None { error = closed; }
    return error;
}
