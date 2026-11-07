// SPDX-License-Identifier: Apache-2.0
// Prints the full outcome of one stream, in the same shape as
// `zlib_bounded --probe`, so `bench/compare/contract_parity.py` can require
// that the C reference row and this library agree field by field.
//
//   goose_probe <gzip|zlib|raw> <input> <output_budget> <input_budget> \
//               <chunk> <reject|allow|members> <members>
//
// It prints one line:
//
//   error=<name> produced=<n> consumed=<n> members=<n> trailing=<n> fnv1a=<h>
//
// `consumed` is the one field the contract does not make chunking-invariant
// when the output budget stops the decode; the parity check compares it only
// where docs/api.md promises it is stable.
import compress from "../..";
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

fn parse_format(text: const u8[:]) -> compress::Format {
    if text == "zlib" { return compress::Format.Zlib; }
    if text == "raw" { return compress::Format.Raw; }
    return compress::Format.Gzip;
}

fn parse_trailing(text: const u8[:]) -> compress::Trailing {
    if text == "allow" { return compress::Trailing.Allow; }
    if text == "members" { return compress::Trailing.Members; }
    return compress::Trailing.Reject;
}

// `describe` returns a human name with spaces; the probe line needs one
// whitespace-separated token per field, so spaces become underscores.
fn token(error: compress::Error) -> u8[>..] {
    let name = compress::describe(error);
    var out: u8[>..] = [];
    for i in name.len {
        if name[i] == ' ' { out.push('_'); } else { out.push(name[i]); }
    }
    return out;
}

fn main() {
    let argv = args();
    if argv.len != 8 {
        write_stderr("usage: goose_probe <gzip|zlib|raw> <input> <output_budget> <input_budget> <chunk> <reject|allow|members> <members>\n");
        exit(2);
    }
    let format = parse_format(argv[1]);
    let output_budget = parse_i64(argv[3]);
    let input_budget = parse_i64(argv[4]);
    let chunk = parse_i64(argv[5]);
    let trailing = parse_trailing(argv[6]);
    let members = parse_i64(argv[7]);
    if chunk < 1 {
        write_stderr("goose_probe: chunk must be positive\n");
        exit(2);
    }
    var input: u8[>..] = [];
    if !read_file(argv[2], input) {
        write_stderr(str("goose_probe: cannot read ", argv[2], "\n"));
        exit(1);
    }
    let limits = compress::Limits {
        output_bytes: output_budget,
        input_bytes:  input_budget,
        members:      members,
        trailing:     trailing,
    };
    var out: u8[>..] = [];
    var decoder = compress::Decoder { .. };
    var error = compress::open(decoder, format, limits);
    if error == compress::Error.None {
        var offset: i64 = 0;
        while offset < input.len {
            let end = min(offset + chunk, input.len);
            error = compress::feed(decoder, input[offset..end], out);
            if error != compress::Error.None { break; }
            offset = end;
        }
        if error == compress::Error.None { error = compress::finish(decoder); }
    }
    let progress = decoder.progress;
    compress::close(decoder);
    print("error=", token(error),
          " produced=", out.len,
          " consumed=", progress.consumed,
          " members=", progress.members,
          " trailing=", progress.trailing,
          " fnv1a=", fnv1a(out[..]), "\n");
}
