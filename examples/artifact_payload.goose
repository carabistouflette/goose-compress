// SPDX-License-Identifier: Apache-2.0
// A bounded gzip payload reader, shaped like the step a downloader needs
// after it has fetched a `.gz` artifact: decode it incrementally, never let
// the decompressed size escape a budget, and report exactly why it stopped.
//
// This is a candidate consumer contract, not an adoption. Nothing in
// goose-artifact or goose-archive calls this library yet.
//
//   goose run examples/artifact_payload.goose -- payload.gz out.bin 1048576
//
// Contract:
//   * the compressed file is read whole and refused above COMPRESSED_CAP;
//   * the decompressed payload is decoded in CHUNK-byte steps and refused
//     above the budget given on the command line;
//   * exactly one gzip member is accepted, and any byte after it is an error;
//   * the CRC-32 and ISIZE trailer must match, or nothing is written;
//   * the output file is written only when the whole stream verified.
import compress from "..";
import os;
import std;

fn compressed_cap() -> i64 { return 8388608; }
fn chunk_bytes() -> i64 { return 4096; }

fn main() {
    let argv = args();
    if argv.len != 4 {
        write_stderr("usage: artifact_payload <payload.gz> <output> <budget-bytes>\n");
        exit(2);
    }
    let budget = parse_i64(argv[3]);
    if budget < 0 || budget > 1073741824 {
        write_stderr("artifact_payload: the budget must be 0..1073741824\n");
        exit(2);
    }
    var compressed: u8[>..] = [];
    if !read_file(argv[1], compressed) {
        write_stderr(str("artifact_payload: cannot read ", argv[1], "\n"));
        exit(1);
    }
    if compressed.len > compressed_cap() {
        write_stderr(str("artifact_payload: compressed input is ", compressed.len,
                         " bytes, over the ", compressed_cap(), " byte cap\n"));
        exit(1);
    }

    let limits = compress::Limits {
        output_bytes: budget,
        input_bytes:  compressed.len,
        members:      1,
        trailing:     compress::Trailing.Reject,
    };
    var decoder = compress::Decoder { .. };
    var error = compress::open(decoder, compress::Format.Gzip, limits);
    var payload: u8[>..] = [];
    var offset:  i64     = 0;
    while error == compress::Error.None && offset < compressed.len {
        let end = min(offset + chunk_bytes(), compressed.len);
        // The step buffer is cleared every time, so the only growing buffer
        // is the payload, and the budget bounds that.
        var step: u8[>..] = [];
        error = compress::feed(decoder, compressed[offset..end], step);
        if error == compress::Error.None { payload.append(step[..]); }
        offset = end;
    }
    if error == compress::Error.None { error = compress::finish(decoder); }
    let closed = compress::close(decoder);
    if error == compress::Error.None { error = closed; }

    print("outcome: ", compress::describe(error), "\n");
    print("compressed: ", compressed.len, " bytes\n");
    print("decompressed: ", payload.len, " bytes\n");
    print("members: ", decoder.progress.members, "\n");
    print("trailing: ", decoder.progress.trailing, " bytes\n");
    if error != compress::Error.None {
        write_stderr(str("artifact_payload: refusing to write: ",
                         compress::describe(error), "\n"));
        exit(1);
    }
    if !write_file(argv[2], payload[..]) {
        write_stderr(str("artifact_payload: cannot write ", argv[2], "\n"));
        exit(1);
    }
    print("wrote: ", argv[2], "\n");
}

// A strict decimal parser: no sign, no whitespace, no overflow.
fn parse_i64(text: const u8[:]) -> i64 {
    if text.len == 0 || text.len > 10 { return -1; }
    var value: i64 = 0;
    for i in text.len {
        if text[i] < '0' || text[i] > '9' { return -1; }
        value = value * 10 + (text[i] - '0');
    }
    return value;
}
