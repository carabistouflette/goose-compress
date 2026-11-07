// SPDX-License-Identifier: Apache-2.0
// Runs every specification fixture and compares the exact error and the
// exact output bytes against the independent oracle's expectation.
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
fn error_of(code: i64) -> compress::Error {
    if code == 0 { return compress::Error.None; }
    if code == 1 { return compress::Error.InvalidInput; }
    if code == 2 { return compress::Error.LimitExceeded; }
    if code == 3 { return compress::Error.Malformed; }
    if code == 4 { return compress::Error.Truncated; }
    if code == 5 { return compress::Error.Checksum; }
    if code == 6 { return compress::Error.TrailingData; }
    if code == 7 { return compress::Error.TooManyMembers; }
    if code == 8 { return compress::Error.DictionaryRequired; }
    if code == 9 { return compress::Error.DictionaryMismatch; }
    return compress::Error.SystemError;
}

fn main() {
    var index: u8[>..] = [];
    if !read_file(".build/fixtures/index.bin", index) {
        write_stderr("spec: run `make fixtures` first\n");
        exit(1);
    }
    if index.len < 12 || index[0] != 'G' || index[1] != 'C' || index[2] != 'F' ||
       index[3] != 'I' || index[4] != 'X' || index[5] != '0' || index[6] != '0' ||
       index[7] != '1' {
        write_stderr("spec: bad index magic\n");
        exit(1);
    }
    let count = read_u32(index, 8);
    var position: i64 = 12;
    var failures: i64 = 0;
    var checked:  i64 = 0;
    for _ in count {
        let name_length = read_u8(index, position);
        position += 1;
        let name = index[position..position + name_length];
        position += name_length;
        let format = read_u8(index, position);
        let trailing = read_u8(index, position + 1);
        let has_dictionary = read_u8(index, position + 2);
        position += 3;
        let output_bytes = read_i64(index, position);
        let input_bytes = read_i64(index, position + 8);
        let members = read_i64(index, position + 16);
        position += 24;
        let expected_code = read_u8(index, position);
        position += 1;

        var input: u8[>..] = [];
        if !read_file(str(".build/fixtures/", name, ".in"), input) {
            write_stderr(str("spec: missing input for ", name, "\n"));
            failures += 1;
            continue;
        }
        var expected: u8[>..] = [];
        if !read_file(str(".build/fixtures/", name, ".out"), expected) {
            write_stderr(str("spec: missing expectation for ", name, "\n"));
            failures += 1;
            continue;
        }
        var dictionary: u8[>..] = [];
        if has_dictionary == 1 {
            if !read_file(str(".build/fixtures/", name, ".dict"), dictionary) {
                write_stderr(str("spec: missing dictionary for ", name, "\n"));
                failures += 1;
                continue;
            }
        }
        let limits = compress::Limits {
            output_bytes: output_bytes,
            input_bytes:  input_bytes,
            members:      members,
            trailing:     trailing_of(trailing),
        };
        var decoder = compress::Decoder { .. };
        var error = compress::open_with_dictionary(decoder, format_of(format), limits,
                                                   dictionary[..]);
        var output: u8[>..] = [];
        if error == compress::Error.None {
            error = compress::feed(decoder, input[..], output);
        }
        if error == compress::Error.None { error = compress::finish(decoder); }
        let closed = compress::close(decoder);
        if error == compress::Error.None && closed != compress::Error.None {
            error = closed;
        }
        checked += 1;
        if error != error_of(expected_code) {
            write_stderr(str("spec: ", name, ": expected ",
                             compress::describe(error_of(expected_code)), ", got ",
                             compress::describe(error), "\n"));
            failures += 1;
            continue;
        }
        if output != expected {
            write_stderr(str("spec: ", name, ": expected ", expected.len, " bytes, got ",
                             output.len, "\n"));
            failures += 1;
        }
    }
    if position != index.len {
        write_stderr(str("spec: index has ", index.len - position, " trailing bytes\n"));
        failures += 1;
    }
    if compress::abi().live != 0 {
        write_stderr(str("spec: ", compress::abi().live, " decoders left open\n"));
        failures += 1;
    }
    if failures != 0 {
        write_stderr(str("spec: ", failures, " of ", checked, " fixtures failed\n"));
        exit(1);
    }
    print("spec ok: ", checked, " fixtures\n");
}
