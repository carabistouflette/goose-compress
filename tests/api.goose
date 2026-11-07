// SPDX-License-Identifier: Apache-2.0
// The public API's own contract: lifecycle, ownership, invalid arguments,
// progress accounting and cleanup. Fixture behaviour lives in spec.goose.
import compress from "..";
import os;
import std;

fn load(path: const u8[:]) -> u8[>..] {
    var bytes: u8[>..] = [];
    if !read_file(path, bytes) {
        write_stderr(str("api: cannot read ", path, "\n"));
        exit(1);
    }
    return bytes;
}

fn main() {
    var failures: i64 = 0;
    let defaults = compress::Limits { .. };
    let text = load(".build/fixtures/gzip-text.in");
    let expected = load(".build/fixtures/gzip-text.out");

    // A decoder starts Idle and refuses work until it is open.
    var decoder = compress::Decoder { .. };
    var out: u8[>..] = [];
    if decoder.progress.state != compress::State.Idle ||
       compress::feed(decoder, text[..], out) != compress::Error.InvalidInput ||
       compress::finish(decoder) != compress::Error.InvalidInput {
        write_stderr("api: an unopened decoder must refuse work\n");
        failures += 1;
    }
    if compress::close(decoder) != compress::Error.None {
        write_stderr("api: closing an unopened decoder must succeed\n");
        failures += 1;
    }

    // Opening twice on one decoder is a caller error, not a leak.
    if compress::open(decoder, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("api: open failed\n");
        failures += 1;
    }
    if compress::open(decoder, compress::Format.Gzip, defaults) != compress::Error.InvalidInput {
        write_stderr("api: opening an open decoder must be invalid input\n");
        failures += 1;
    }
    if decoder.progress.state != compress::State.Active {
        write_stderr("api: an open decoder must be Active\n");
        failures += 1;
    }

    // Output is owned by the caller and survives the input being overwritten.
    var error = compress::feed(decoder, text[..], out);
    if error == compress::Error.None { error = compress::finish(decoder); }
    if error != compress::Error.None || out != expected {
        write_stderr(str("api: decode failed: ", compress::describe(error), "\n"));
        failures += 1;
    }
    for i in text.len { text[i] = 0xAA; }
    if out != expected {
        write_stderr("api: output must not alias the input\n");
        failures += 1;
    }
    // Reload: the bytes above were deliberately destroyed.
    let stream = load(".build/fixtures/gzip-text.in");
    if decoder.progress.state != compress::State.Done ||
       decoder.progress.members != 1 ||
       decoder.progress.consumed != text.len ||
       decoder.progress.produced != expected.len ||
       decoder.progress.trailing != 0 {
        write_stderr(str("api: progress after a clean stream: state ",
                         compress::state_name(decoder.progress.state), " members ",
                         decoder.progress.members, " consumed ", decoder.progress.consumed,
                         " produced ", decoder.progress.produced, "\n"));
        failures += 1;
    }
    if compress::close(decoder) != compress::Error.None || decoder.handle != 0 ||
       decoder.progress.state != compress::State.Idle {
        write_stderr("api: close must free and reset the decoder\n");
        failures += 1;
    }
    if compress::close(decoder) != compress::Error.None {
        write_stderr("api: close must be idempotent\n");
        failures += 1;
    }

    // Every limit field is validated before any native state is created.
    let bad_output = compress::Limits { output_bytes: -1 };
    let bad_output_high = compress::Limits { output_bytes: 1073741825 };
    let bad_input = compress::Limits { input_bytes: -1 };
    let bad_members_low = compress::Limits { members: 0 };
    let bad_members_high = compress::Limits { members: 1025 };
    let bad_members_pair = compress::Limits { members: 2 };
    var probe = compress::Decoder { .. };
    if compress::open(probe, compress::Format.Gzip, bad_output) != compress::Error.InvalidInput ||
       compress::open(probe, compress::Format.Gzip, bad_output_high) != compress::Error.InvalidInput ||
       compress::open(probe, compress::Format.Gzip, bad_input) != compress::Error.InvalidInput ||
       compress::open(probe, compress::Format.Gzip, bad_members_low) != compress::Error.InvalidInput ||
       compress::open(probe, compress::Format.Gzip, bad_members_high) != compress::Error.InvalidInput ||
       compress::open(probe, compress::Format.Gzip, bad_members_pair) != compress::Error.InvalidInput {
        write_stderr("api: every invalid limit must be rejected\n");
        failures += 1;
    }
    if probe.handle != 0 {
        write_stderr("api: a rejected open must not leave a handle\n");
        failures += 1;
    }

    // gzip has no preset dictionary, and a dictionary over 32 KiB is refused.
    var big: u8[>..] = [];
    big.resize(32769, 0);
    if compress::open_with_dictionary(probe, compress::Format.Gzip, defaults, "d") !=
       compress::Error.InvalidInput {
        write_stderr("api: gzip must reject a dictionary\n");
        failures += 1;
    }
    if compress::open_with_dictionary(probe, compress::Format.Raw, defaults, big[..]) !=
       compress::Error.InvalidInput {
        write_stderr("api: a dictionary over 32 KiB must be refused\n");
        failures += 1;
    }
    if probe.handle != 0 {
        write_stderr("api: a rejected dictionary must not leave a handle\n");
        failures += 1;
    }

    // A failed stream is terminal: the same error comes back, and the
    // decoder still has to be closed.
    var failing = compress::Decoder { .. };
    let tight = compress::Limits { output_bytes: 4 };
    if compress::open(failing, compress::Format.Gzip, tight) != compress::Error.None {
        write_stderr("api: open failed\n");
        failures += 1;
    }
    var small: u8[>..] = [];
    let first = compress::feed(failing, stream[..], small);
    let second = compress::feed(failing, stream[..], small);
    if first != compress::Error.LimitExceeded || second != compress::Error.LimitExceeded ||
       failing.progress.state != compress::State.Failed || small.len != 4 {
        write_stderr(str("api: a failed stream must stay failed: ",
                         compress::describe(first), "/", compress::describe(second),
                         " bytes ", small.len, "\n"));
        failures += 1;
    }
    if compress::close(failing) != compress::Error.None {
        write_stderr("api: a failed decoder must still close\n");
        failures += 1;
    }

    // The one-shot helper is the same state machine, bounded the same way.
    var one_shot: u8[>..] = [];
    if compress::decompress(stream[..], compress::Format.Gzip, defaults, one_shot) !=
       compress::Error.None || one_shot != expected {
        write_stderr("api: decompress must match the streaming path\n");
        failures += 1;
    }
    one_shot.clear();
    if compress::decompress(stream[..], compress::Format.Gzip, tight, one_shot) !=
       compress::Error.LimitExceeded || one_shot.len != 4 {
        write_stderr("api: decompress must honour the output budget\n");
        failures += 1;
    }

    // Every error and state has a name, and the ABI report is populated.
    let names = [
        compress::Error.None, compress::Error.InvalidInput, compress::Error.LimitExceeded,
        compress::Error.Malformed, compress::Error.Truncated, compress::Error.Checksum,
        compress::Error.TrailingData, compress::Error.TooManyMembers,
        compress::Error.DictionaryRequired, compress::Error.DictionaryMismatch,
        compress::Error.SystemError,
    ];
    for name in names {
        if compress::describe(name).len == 0 {
            write_stderr("api: an error has no description\n");
            failures += 1;
        }
    }
    let states = [compress::State.Idle, compress::State.Active, compress::State.MemberEnd,
                  compress::State.Done, compress::State.Failed];
    for state in states {
        if compress::state_name(state).len == 0 {
            write_stderr("api: a state has no name\n");
            failures += 1;
        }
    }
    let abi = compress::abi();
    if abi.zlib < 10200 || abi.stream_bytes <= 0 || abi.live != 0 {
        write_stderr(str("api: unexpected ABI report: zlib ", abi.zlib, " stream ",
                         abi.stream_bytes, " live ", abi.live, "\n"));
        failures += 1;
    }
    if compress::zlib_version().len == 0 {
        write_stderr("api: the zlib version string is empty\n");
        failures += 1;
    }

    if failures != 0 {
        write_stderr(str("api: ", failures, " failures\n"));
        exit(1);
    }
    print("api ok\n");
}
