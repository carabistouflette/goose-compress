// SPDX-License-Identifier: Apache-2.0
// A libFuzzer target that drives the real public API.
//
// It does not reimplement decoding and it does not call the C adapter
// directly: every input goes through compress::open_with_dictionary, feed,
// finish and close, so the state machine, the cumulative budgets, the
// dictionary handling and the native adapter are exercised together under
// ASan and UBSan.
//
// The assertions are limited to properties docs/api.md promises for *every*
// input, so a failure is a real defect rather than a fuzzer artifact. The
// chunking guarantee is checked by decoding the same bytes twice at two
// different chunk sizes and requiring the same answer.
//
//   python3 scripts/fuzz.py --goose /path/to/goose --seconds 120 --runs 100000
import compress from "..";
import std;

struct Outcome {
    error:    compress::Error = compress::Error.None,
    consumed: i64             = 0,
    produced: i64             = 0,
    members:  i64             = 0,
    trailing: i64             = 0,
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

fn error_code(error: compress::Error) -> i64 {
    if error == compress::Error.None { return 0; }
    if error == compress::Error.InvalidInput { return 1; }
    if error == compress::Error.LimitExceeded { return 2; }
    if error == compress::Error.Malformed { return 3; }
    if error == compress::Error.Truncated { return 4; }
    if error == compress::Error.Checksum { return 5; }
    if error == compress::Error.TrailingData { return 6; }
    if error == compress::Error.TooManyMembers { return 7; }
    if error == compress::Error.DictionaryRequired { return 8; }
    if error == compress::Error.DictionaryMismatch { return 9; }
    return 10;
}

// One full open/feed/finish/close cycle at a fixed chunk size.
fn decode_once(format: compress::Format, limits: const compress::Limits&,
               dictionary: const u8[:], input: const u8[:], chunk: i64,
               out: u8[>..]&) -> Outcome {
    var outcome = Outcome { .. };
    var decoder = compress::Decoder { .. };
    outcome.error = compress::open_with_dictionary(decoder, format, limits, dictionary);
    if outcome.error != compress::Error.None {
        // A refused open must not retain native state.
        assert(decoder.handle == 0);
        assert(decoder.progress.state == compress::State.Idle);
        return outcome;
    }
    assert(decoder.progress.state == compress::State.Active);
    var offset: i64 = 0;
    loop {
        if offset >= input.len { break; }
        let end = min(offset + chunk, input.len);
        outcome.error = compress::feed(decoder, input[offset..end], out);
        if outcome.error != compress::Error.None { break; }
        offset = end;
    }
    if outcome.error == compress::Error.None {
        outcome.error = compress::finish(decoder);
    }
    outcome.consumed = decoder.progress.consumed;
    outcome.produced = decoder.progress.produced;
    outcome.members = decoder.progress.members;
    outcome.trailing = decoder.progress.trailing;
    if outcome.error != compress::Error.None {
        // A terminal error is sticky: feed repeats it, and finish reports the
        // original error rather than Truncated.
        assert(decoder.progress.state == compress::State.Failed);
        assert(compress::feed(decoder, "", out) == outcome.error);
        assert(compress::finish(decoder) == outcome.error);
    }
    assert(compress::close(decoder) == compress::Error.None);
    assert(decoder.handle == 0);
    assert(decoder.progress.state == compress::State.Idle);
    return outcome;
}

// The fuzz entry point. Returns the error code so the work cannot be elided.
export fn fuzz_decode(data: const u8[:]) -> i64 {
    if data.len < 8 { return -1; }
    let format = format_of((data[0] as i64) % 3);
    let trailing = trailing_of((data[1] as i64) % 3);
    let members = if trailing == compress::Trailing.Members {
        1 + (data[2] as i64) % 4
    } else {
        1
    };
    // Budgets are drawn from a set that straddles the boundaries a caller can
    // actually hit: zero, one, small, and large enough to finish.
    let output_bytes = [0, 1, 4, 16, 64, 256, 1024, 1048576][(data[3] as i64) % 8];
    let input_bytes = [0, 1, 4, 16, 64, 256, data.len, data.len + 1][(data[4] as i64) % 8];

    var dictionary: u8[>..] = [];
    var body_start: i64     = 8;
    if (data[5] as i64) % 2 == 1 && format != compress::Format.Gzip && data.len > 12 {
        let dictionary_len = min((data.len - 8) / 4, 64);
        dictionary.append(data[8..8 + dictionary_len]);
        body_start = 8 + dictionary_len;
    }
    let body = data[body_start..];

    // Chunk 1 is where boundary defects live, so small inputs always get it.
    let chunk_a = if data.len <= 128 { 1 } else { 4 + (data[6] as i64) % 12 };
    let chunk_b = 16 + (data[7] as i64) % 112;

    let limits = compress::Limits {
        output_bytes: output_bytes,
        input_bytes:  input_bytes,
        members:      members,
        trailing:     trailing,
    };

    let live_before = compress::abi().live;
    var first: u8[>..] = [];
    let a = decode_once(format, limits, dictionary[..], body, chunk_a, first);
    assert(compress::abi().live == live_before);

    // Progress accounting: produced counts bytes appended to the caller's
    // builder, and neither cumulative budget may be exceeded.
    assert(a.produced == first.len);
    assert(a.consumed <= limits.input_bytes);
    assert(a.produced <= limits.output_bytes);
    assert(a.members <= limits.members);

    var second: u8[>..] = [];
    let b = decode_once(format, limits, dictionary[..], body, chunk_b, second);
    assert(compress::abi().live == live_before);
    assert(b.produced == second.len);

    // The chunking guarantee. progress.consumed is deliberately excluded: it
    // is not invariant when the output budget is exceeded, which is a
    // recorded open gate in tests/contract.goose, not something to assert.
    assert(a.error == b.error);
    assert(first == second);
    assert(a.produced == b.produced);
    assert(a.members == b.members);
    assert(a.trailing == b.trailing);
    return error_code(a.error);
}

// Used by the driver's self-test to prove a Goose assertion failure reaches
// libFuzzer as a crash with the triggering input retained.
export fn fuzz_assertion_probe(value: i64) { assert(value != 0); }

fn main() {}
