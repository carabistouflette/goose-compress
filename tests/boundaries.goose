// SPDX-License-Identifier: Apache-2.0
// Exact bounds and chunking invariance.
//
// Every specification fixture is decoded again at seven chunk sizes and must
// report the same error and the same bytes each time. The explicit cases
// below pin the boundaries a caller can actually hit: a zero budget, a budget
// exactly the payload's size, a budget one byte short, an input budget exactly
// the stream's size, and trailing bytes that must stay inside that budget.
import compress from "..";
import os;
import std;

fn read_u8(data: const u8[:], position: i64) -> i64 { return data[position]; }
fn read_u32(data: const u8[:], position: i64) -> i64 {
    var value: i64 = 0;
    for i in 4 { value |= (data[position + i] as i64) << (i * 8); }
    return value;
}
fn read_i64(data: const u8[:], position: i64) -> i64 {
    var value: i64 = 0;
    for i in 8 { value |= (data[position + i] as i64) << (i * 8); }
    return value;
}
fn format_of(code: i64) -> compress::Format {
    if code == 0 { return compress::Format.Gzip; }
    if code == 1 { return compress::Format.Zlib; }
    return compress::Format.Raw;
}
fn trailing_of(code: i64) -> compress::Trailing {
    if code == 0 { return compress::Trailing.Reject; }
    if code == 1 { return compress::Trailing.Allow; }
    return compress::Trailing.Members;
}

// Feeds `input` in slices of `chunk` bytes, then finishes and closes.
fn run_chunked(format: compress::Format, limits: const compress::Limits&,
               dictionary: const u8[:], input: const u8[:], chunk: i64,
               out: u8[>..]&) -> compress::Error {
    var decoder = compress::Decoder { .. };
    var error = compress::open_with_dictionary(decoder, format, limits, dictionary);
    if error != compress::Error.None { return error; }
    var offset: i64 = 0;
    loop {
        if offset >= input.len { break; }
        let end = min(offset + chunk, input.len);
        error = compress::feed(decoder, input[offset..end], out);
        if error != compress::Error.None { break; }
        offset = end;
    }
    if error == compress::Error.None { error = compress::finish(decoder); }
    let closed = compress::close(decoder);
    if error == compress::Error.None { error = closed; }
    return error;
}

fn load(path: const u8[:]) -> u8[>..] {
    var bytes: u8[>..] = [];
    if !read_file(path, bytes) {
        write_stderr(str("boundaries: cannot read ", path, "\n"));
        exit(1);
    }
    return bytes;
}

fn main() {
    var index = load(".build/fixtures/index.bin");
    let count = read_u32(index, 8);
    var position: i64 = 12;
    var failures: i64 = 0;
    var runs:     i64 = 0;
    let chunks = [1, 2, 3, 7, 64, 1024, 65536];
    for _ in count {
        let name_length = read_u8(index, position);
        position += 1;
        let name = index[position..position + name_length];
        position += name_length;
        let format = format_of(read_u8(index, position));
        let trailing = trailing_of(read_u8(index, position + 1));
        let has_dictionary = read_u8(index, position + 2);
        position += 3;
        let limits = compress::Limits {
            output_bytes: read_i64(index, position),
            input_bytes:  read_i64(index, position + 8),
            members:      read_i64(index, position + 16),
            trailing:     trailing,
        };
        position += 24;
        position += 1;  // the expected code is spec.goose's business

        let input = load(str(".build/fixtures/", name, ".in"));
        var dictionary: u8[>..] = [];
        if has_dictionary == 1 {
            dictionary = load(str(".build/fixtures/", name, ".dict"));
        }
        var reference: u8[>..] = [];
        let reference_error = run_chunked(format, limits, dictionary[..], input[..],
                                          65536, reference);
        for chunk in chunks {
            var output: u8[>..] = [];
            let error = run_chunked(format, limits, dictionary[..], input[..], chunk, output);
            runs += 1;
            if error != reference_error || output != reference {
                write_stderr(str("boundaries: ", name, " at chunk ", chunk, ": ",
                                 compress::describe(reference_error), "/", reference.len,
                                 " became ", compress::describe(error), "/", output.len,
                                 "\n"));
                failures += 1;
            }
        }
    }

    // A zero output budget succeeds on a stream that produces nothing.
    let empty = load(".build/fixtures/gzip-empty.in");
    var out: u8[>..] = [];
    let zero = compress::Limits { output_bytes: 0 };
    if run_chunked(compress::Format.Gzip, zero, "", empty[..], 65536, out) != compress::Error.None ||
       out.len != 0 {
        write_stderr("boundaries: empty stream with a zero budget must succeed\n");
        failures += 1;
    }

    // A zero output budget fails on a stream that produces one byte, and
    // appends nothing.
    let one = load(".build/fixtures/gzip-one-byte.in");
    out.clear();
    if run_chunked(compress::Format.Gzip, zero, "", one[..], 65536, out) !=
       compress::Error.LimitExceeded || out.len != 0 {
        write_stderr("boundaries: one byte with a zero budget must exceed it\n");
        failures += 1;
    }

    // A budget of exactly one byte, with the trailer in a later chunk, must
    // consume the trailer without producing anything more.
    let exact_one = compress::Limits { output_bytes: 1 };
    var decoder = compress::Decoder { .. };
    if compress::open(decoder, compress::Format.Gzip, exact_one) != compress::Error.None {
        write_stderr("boundaries: open failed\n");
        failures += 1;
    }
    var body: u8[>..] = [];
    let body_end = one.len - 8;
    let first = compress::feed(decoder, one[..body_end], body);
    let second = compress::feed(decoder, one[body_end..], body);
    let finished = compress::finish(decoder);
    let closed = compress::close(decoder);
    if first != compress::Error.None || second != compress::Error.None ||
       finished != compress::Error.None || closed != compress::Error.None ||
       body.len != 1 || decoder.progress.members != 1 {
        write_stderr(str("boundaries: split trailer: ", compress::describe(first), "/",
                         compress::describe(second), "/", compress::describe(finished),
                         " bytes ", body.len, "\n"));
        failures += 1;
    }

    // Trailing bytes are accepted input: they stay inside the input budget,
    // and the answer does not depend on where the chunk boundary falls.
    let exact_input = compress::Limits {
        trailing:    compress::Trailing.Allow,
        input_bytes: empty.len,
    };
    var split = compress::Decoder { .. };
    if compress::open(split, compress::Format.Gzip, exact_input) != compress::Error.None {
        write_stderr("boundaries: open failed\n");
        failures += 1;
    }
    var sink: u8[>..] = [];
    let member = compress::feed(split, empty[..], sink);
    let overflow = compress::feed(split, "unbudgeted trailing bytes", sink);
    compress::close(split);
    if member != compress::Error.None || overflow != compress::Error.LimitExceeded {
        write_stderr(str("boundaries: trailing bytes escaped the input budget: ",
                         compress::describe(member), "/", compress::describe(overflow), "\n"));
        failures += 1;
    }
    var over_budget: u8[>..] = [];
    over_budget.append(empty);
    over_budget.append("unbudgeted trailing bytes");
    var whole = compress::Decoder { .. };
    if compress::open(whole, compress::Format.Gzip, exact_input) != compress::Error.None {
        write_stderr("boundaries: open failed\n");
        failures += 1;
    }
    let one_chunk = compress::feed(whole, over_budget[..], sink);
    compress::close(whole);
    if one_chunk != compress::Error.LimitExceeded {
        write_stderr(str("boundaries: one chunk gave ", compress::describe(one_chunk), "\n"));
        failures += 1;
    }

    // Within the budget, trailing bytes are counted the same way either way.
    let roomy = compress::Limits {
        trailing:    compress::Trailing.Allow,
        input_bytes: 4096,
    };
    var a = compress::Decoder { .. };
    var b = compress::Decoder { .. };
    if compress::open(a, compress::Format.Gzip, roomy) != compress::Error.None ||
       compress::open(b, compress::Format.Gzip, roomy) != compress::Error.None {
        write_stderr("boundaries: open failed\n");
        failures += 1;
    }
    var within_budget: u8[>..] = [];
    within_budget.append(empty);
    within_budget.append("tail");
    compress::feed(a, empty[..], sink);
    compress::feed(a, "tail", sink);
    compress::feed(b, within_budget[..], sink);
    let a_done = compress::finish(a);
    let b_done = compress::finish(b);
    if a.progress.trailing != b.progress.trailing ||
       a.progress.consumed != b.progress.consumed ||
       a_done != compress::Error.None || b_done != compress::Error.None {
        write_stderr(str("boundaries: trailing accounting differs: ", a.progress.trailing,
                         "/", b.progress.trailing, " consumed ", a.progress.consumed, "/",
                         b.progress.consumed, "\n"));
        failures += 1;
    }
    compress::close(a);
    compress::close(b);

    // Feeding after finish is a caller error, and closing twice is not.
    var done = compress::Decoder { .. };
    let defaults = compress::Limits { .. };
    if compress::open(done, compress::Format.Gzip, defaults) !=
       compress::Error.None {
        write_stderr("boundaries: open failed\n");
        failures += 1;
    }
    var scratch: u8[>..] = [];
    compress::feed(done, empty[..], scratch);
    compress::finish(done);
    if compress::feed(done, empty[..], scratch) != compress::Error.InvalidInput {
        write_stderr("boundaries: feed after finish must be invalid input\n");
        failures += 1;
    }
    if compress::close(done) != compress::Error.None ||
       compress::close(done) != compress::Error.None {
        write_stderr("boundaries: close must be idempotent\n");
        failures += 1;
    }

    if compress::abi().live != 0 {
        write_stderr(str("boundaries: ", compress::abi().live, " decoders left open\n"));
        failures += 1;
    }
    if failures != 0 {
        write_stderr(str("boundaries: ", failures, " failures over ", runs, " chunked runs\n"));
        exit(1);
    }
    print("boundaries ok: ", runs, " chunked runs\n");
}
