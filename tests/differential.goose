// SPDX-License-Identifier: Apache-2.0
// Runs the differential corpus: every case is decoded at the chunk size the
// corpus records, and the error and the exact output bytes are compared with
// the independent oracle's expectation.
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

fn run_chunked(format: compress::Format, limits: const compress::Limits&,
               dictionary: const u8[:], input: const u8[:], chunk: i64,
               out: u8[>..]&) -> compress::Error {
    var decoder = compress::Decoder { .. };
    var error = compress::open_with_dictionary(decoder, format, limits, dictionary);
    if error != compress::Error.None { return error; }
    var offset: i64 = 0;
    loop {
        if offset >= input.len { break; }
        let end = if chunk <= 0 { input.len } else { min(offset + chunk, input.len) };
        error = compress::feed(decoder, input[offset..end], out);
        if error != compress::Error.None { break; }
        offset = end;
    }
    if error == compress::Error.None { error = compress::finish(decoder); }
    let closed = compress::close(decoder);
    if error == compress::Error.None { error = closed; }
    return error;
}

fn main() {
    var corpus: u8[>..] = [];
    if !read_file(".build/corpus/corpus.bin", corpus) {
        write_stderr("differential: run `make corpus` first\n");
        exit(1);
    }
    if corpus.len < 12 || corpus[0] != 'G' || corpus[1] != 'C' || corpus[2] != 'C' ||
       corpus[3] != 'O' || corpus[4] != 'R' || corpus[5] != 'P' || corpus[6] != '0' ||
       corpus[7] != '1' {
        write_stderr("differential: bad corpus magic\n");
        exit(1);
    }
    let count = read_u32(corpus, 8);
    var position: i64 = 12;
    var failures: i64 = 0;
    var checked:  i64 = 0;
    var reported: i64 = 0;
    for case in count {
        let format = format_of(read_u8(corpus, position));
        let trailing = trailing_of(read_u8(corpus, position + 1));
        let has_dictionary = read_u8(corpus, position + 2);
        let expected_code = read_u8(corpus, position + 3);
        position += 4;
        let limits = compress::Limits {
            output_bytes: read_i64(corpus, position),
            input_bytes:  read_i64(corpus, position + 8),
            members:      read_i64(corpus, position + 16),
            trailing:     trailing,
        };
        let chunk = read_i64(corpus, position + 24);
        let dictionary_len = read_i64(corpus, position + 32);
        let input_len = read_i64(corpus, position + 40);
        let expected_len = read_i64(corpus, position + 48);
        position += 56;
        let dictionary = corpus[position..position + dictionary_len];
        position += dictionary_len;
        let input = corpus[position..position + input_len];
        position += input_len;
        let expected = corpus[position..position + expected_len];
        position += expected_len;
        if has_dictionary == 0 && dictionary_len != 0 {
            write_stderr(str("differential: case ", case, " has a dictionary it does not declare\n"));
            failures += 1;
            continue;
        }
        var output: u8[>..] = [];
        let error = run_chunked(format, limits, dictionary, input, chunk, output);
        checked += 1;
        if error != error_of(expected_code) || output != expected {
            failures += 1;
            if reported < 20 {
                reported += 1;
                write_stderr(str("differential: case ", case, " chunk ", chunk, ": expected ",
                                 compress::describe(error_of(expected_code)), "/",
                                 expected_len, " got ", compress::describe(error), "/",
                                 output.len, "\n"));
            }
        }
    }
    if position != corpus.len {
        write_stderr(str("differential: corpus has ", corpus.len - position,
                         " trailing bytes\n"));
        failures += 1;
    }
    if compress::abi().live != 0 {
        write_stderr(str("differential: ", compress::abi().live, " decoders left open\n"));
        failures += 1;
    }
    if failures != 0 {
        write_stderr(str("differential: ", failures, " of ", checked, " cases failed\n"));
        exit(1);
    }
    print("differential ok: ", checked, " cases\n");
}
