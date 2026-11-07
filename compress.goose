// SPDX-License-Identifier: Apache-2.0
// Bounded incremental gzip, zlib and raw DEFLATE decoding over system zlib.
// The state machine, every cumulative budget and every policy decision live
// here; native/compress.c only owns a z_stream and reports what zlib did.
namespace compress;
import std;

enum Format { Gzip, Zlib, Raw }
// What may follow the last member of a stream.
//   Reject  exactly one member; any later byte is TrailingData.
//   Allow   exactly one member; later bytes are counted in progress.trailing.
//   Members up to limits.members members; later bytes are TrailingData.
enum Trailing { Reject, Allow, Members }
enum State { Idle, Active, MemberEnd, Done, Failed }
enum Error {
    None,
    InvalidInput,
    LimitExceeded,
    Malformed,
    Truncated,
    Checksum,
    TrailingData,
    TooManyMembers,
    DictionaryRequired,
    DictionaryMismatch,
    SystemError,
}

// Cumulative budgets for one decoder. They are independent of how the caller
// chunks its input and of whether it clears its output builder between calls.
struct Limits {
    output_bytes: i64      = 16777216,
    input_bytes:  i64      = 33554432,
    members:      i64      = 1,
    trailing:     Trailing = Trailing.Reject,
}

struct Progress {
    state:    State = State.Idle,
    consumed: i64   = 0,
    produced: i64   = 0,
    members:  i64   = 0,
    trailing: i64   = 0,
}

struct Decoder {
    handle:   i64      = 0,
    format:   Format   = Format.Gzip,
    limits:   Limits   = Limits { .. },
    progress: Progress = Progress { .. },
    error:    Error    = Error.None,
}

struct Abi {
    zlib:         i64 = 0,
    stream_bytes: i64 = 0,
    live:         i64 = 0,
}

extern fn native_open(format: i64, meta: i64[:]) -> i64 = "gc_open";
extern fn native_dictionary(handle: i64, dictionary: const u8[:], meta: i64[:]) -> i64 = "gc_dictionary";
extern fn native_inflate(handle: i64, input: const u8[:], output: u8[:], meta: i64[:]) -> i64 = "gc_inflate";
extern fn native_reset(handle: i64, meta: i64[:]) -> i64 = "gc_reset";
extern fn native_close(handle: i64, meta: i64[:]) -> i64 = "gc_close";
extern fn native_abi(meta: i64[:]) -> i64 = "gc_abi";
extern fn native_version(out: u8[:], meta: i64[:]) -> i64 = "gc_version";

// The largest output a single inflate step may produce. It bounds the growth
// of the caller's builder per step, not the stream: the cumulative budget is
// limits.output_bytes.
fn chunk_bytes() -> i64 { return 65536; }
fn max_output_bytes() -> i64 { return 1073741824; }
fn max_input_bytes() -> i64 { return 1073741824; }
fn max_members() -> i64 { return 1024; }
fn max_dictionary_bytes() -> i64 { return 32768; }

fn format_code(format: Format) -> i64 {
    if format == Format.Gzip { return 0; }
    if format == Format.Zlib { return 1; }
    return 2;
}

fn to_error(code: i64) -> Error {
    if code == 0 { return Error.None; }
    if code == 1 { return Error.InvalidInput; }
    if code == 2 { return Error.LimitExceeded; }
    if code == 3 { return Error.Malformed; }
    if code == 4 { return Error.Checksum; }
    if code == 5 { return Error.TrailingData; }
    if code == 6 { return Error.TooManyMembers; }
    if code == 7 { return Error.DictionaryRequired; }
    if code == 8 { return Error.DictionaryMismatch; }
    if code == 9 { return Error.SystemError; }
    return Error.Truncated;
}

fn valid(limits: const Limits&) -> bool {
    if limits.output_bytes < 0 || limits.output_bytes > max_output_bytes() { return false; }
    if limits.input_bytes < 0 || limits.input_bytes > max_input_bytes() { return false; }
    if limits.members < 1 || limits.members > max_members() { return false; }
    if limits.trailing != Trailing.Members && limits.members != 1 { return false; }
    return true;
}

fn fail(decoder: Decoder&, error: Error) -> Error {
    decoder.error = error;
    decoder.progress.state = State.Failed;
    return error;
}

fn copy_limits(destination: Limits&, source: const Limits&) {
    destination.output_bytes = source.output_bytes;
    destination.input_bytes = source.input_bytes;
    destination.members = source.members;
    destination.trailing = source.trailing;
}

// Opens a decoder for one stream. The decoder must be closed, or Idle.
fn open(decoder: Decoder&, format: Format, limits: const Limits&) -> Error {
    return open_with_dictionary(decoder, format, limits, "");
}

// Opens a decoder with an explicit preset dictionary. Raw streams take it
// before the first byte; zlib streams take it when the stream asks for it and
// its DICTID matches. gzip has no dictionary and rejects one.
fn open_with_dictionary(decoder: Decoder&, format: Format, limits: const Limits&,
                        dictionary: const u8[:]) -> Error {
    if decoder.handle != 0 { return Error.InvalidInput; }
    if !valid(limits) { return Error.InvalidInput; }
    if dictionary.len > max_dictionary_bytes() { return Error.InvalidInput; }
    if format == Format.Gzip && dictionary.len > 0 { return Error.InvalidInput; }
    var meta: i64[2] = [0, 0];
    let handle = native_open(format_code(format), meta[..]);
    if handle == 0 { return to_error(meta[0]); }
    decoder.handle = handle;
    decoder.format = format;
    copy_limits(decoder.limits, limits);
    decoder.progress = Progress { state: State.Active };
    decoder.error = Error.None;
    if dictionary.len > 0 {
        var dict_meta: i64[1] = [0];
        let code = native_dictionary(handle, dictionary, dict_meta[..]);
        if code != 0 {
            var close_meta: i64[1] = [0];
            native_close(handle, close_meta[..]);
            decoder.handle = 0;
            decoder.progress.state = State.Idle;
            return to_error(code);
        }
    }
    return Error.None;
}

// Consumes all of `input`, appending every byte it decodes to `out`. It
// returns Error.None when the input was consumed, including when the stream
// ended before the input did (the trailing policy then decides). A returned
// error is terminal: the decoder is Failed and must be closed.
fn feed(decoder: Decoder&, input: const u8[:], out: u8[>..]&) -> Error {
    if decoder.handle == 0 { return Error.InvalidInput; }
    if decoder.progress.state == State.Failed { return decoder.error; }
    if decoder.progress.state == State.Done { return Error.InvalidInput; }
    // The input budget is a running cap on accepted bytes, including bytes
    // after the last member. A chunk that would cross it is cut at the
    // boundary and the call then fails, so the bytes decoded before the cap
    // do not depend on how the caller chunked its input.
    let allowed = decoder.limits.input_bytes - decoder.progress.consumed;
    if allowed < 0 { return fail(decoder, Error.LimitExceeded); }
    let usable = min(input.len, allowed);
    let over = usable < input.len;
    let data = input[..usable];
    if decoder.progress.state == State.MemberEnd {
        if data.len == 0 {
            if over { return fail(decoder, Error.LimitExceeded); }
            return Error.None;
        }
        if decoder.limits.trailing == Trailing.Allow {
            decoder.progress.consumed += data.len;
            decoder.progress.trailing += data.len;
            if over { return fail(decoder, Error.LimitExceeded); }
            return Error.None;
        }
        if decoder.limits.trailing == Trailing.Members &&
           decoder.progress.members < decoder.limits.members {
            var reset_meta: i64[1] = [0];
            if native_reset(decoder.handle, reset_meta[..]) != 0 {
                return fail(decoder, to_error(reset_meta[0]));
            }
            decoder.progress.state = State.Active;
        } else if decoder.limits.trailing == Trailing.Members {
            return fail(decoder, Error.TooManyMembers);
        } else {
            return fail(decoder, Error.TrailingData);
        }
    }
    var offset: i64 = 0;
    var pending = false;
    loop {
        if offset >= data.len && !pending { break; }
        // A zero budget still needs one byte of room so that a stream which
        // produces nothing can end; a byte that arrives is then over budget.
        let remaining = decoder.limits.output_bytes - decoder.progress.produced;
        let reserve = min(max(remaining, 1), chunk_bytes());
        out.resize(out.len + reserve, 0);
        let base = out.len - reserve;
        var meta: i64[6] = [0, 0, 0, 0, 0, 0];
        let code = native_inflate(decoder.handle, data[offset..], out[base..], meta[..]);
        let consumed = meta[0];
        let produced = meta[1];
        let zlib_code = meta[2];
        let flags = meta[3];
        offset += consumed;
        decoder.progress.consumed += consumed;
        // Bytes zlib decoded before it failed are still the caller's: a
        // checksum failure reports the payload it could not vouch for, and a
        // malformed stream reports the prefix it managed to decode.
        if produced > remaining {
            out.resize(base + remaining);
            decoder.progress.produced += remaining;
            return fail(decoder, Error.LimitExceeded);
        }
        out.resize(base + produced);
        decoder.progress.produced += produced;
        if code != 0 { return fail(decoder, to_error(code)); }
        pending = produced == reserve;
        if flags == 1 {
            decoder.progress.members += 1;
            if offset < data.len {
                if decoder.limits.trailing == Trailing.Members &&
                   decoder.progress.members < decoder.limits.members {
                    var reset_meta: i64[1] = [0];
                    if native_reset(decoder.handle, reset_meta[..]) != 0 {
                        return fail(decoder, to_error(reset_meta[0]));
                    }
                    pending = false;
                    continue;
                }
                if decoder.limits.trailing == Trailing.Allow {
                    // Later bytes are still accepted input, so they stay in
                    // the same budget and the same MemberEnd state that a
                    // later chunk would reach.
                    decoder.progress.consumed += data.len - offset;
                    decoder.progress.trailing += data.len - offset;
                    offset = data.len;
                    decoder.progress.state = State.MemberEnd;
                    pending = false;
                    break;
                }
                if decoder.limits.trailing == Trailing.Members {
                    return fail(decoder, Error.TooManyMembers);
                }
                return fail(decoder, Error.TrailingData);
            }
            decoder.progress.state = State.MemberEnd;
            pending = false;
            break;
        }
        // Z_BUF_ERROR with no progress means zlib needs more input.
        if zlib_code == -5 && consumed == 0 && produced == 0 { break; }
    }
    if over { return fail(decoder, Error.LimitExceeded); }
    return Error.None;
}

// Declares that no more input will arrive. A stream that has not ended is
// Truncated; a completed one becomes Done.
fn finish(decoder: Decoder&) -> Error {
    if decoder.handle == 0 { return Error.InvalidInput; }
    if decoder.progress.state == State.Failed { return decoder.error; }
    if decoder.progress.state == State.Done { return Error.None; }
    if decoder.progress.state == State.MemberEnd {
        decoder.progress.state = State.Done;
        return Error.None;
    }
    return fail(decoder, Error.Truncated);
}

// Frees the decoder. Closing an already closed decoder is not an error.
fn close(decoder: Decoder&) -> Error {
    if decoder.handle == 0 { return Error.None; }
    var meta: i64[1] = [0];
    let code = native_close(decoder.handle, meta[..]);
    decoder.handle = 0;
    decoder.progress.state = State.Idle;
    if code != 0 { return to_error(code); }
    return Error.None;
}

// One bounded call: open, feed, finish, close. `out` receives at most
// limits.output_bytes bytes.
fn decompress(input: const u8[:], format: Format, limits: const Limits&,
              out: u8[>..]&) -> Error {
    var decoder = Decoder { .. };
    var error = open(decoder, format, limits);
    if error != Error.None { return error; }
    error = feed(decoder, input, out);
    if error == Error.None { error = finish(decoder); }
    let closed = close(decoder);
    if error != Error.None { return error; }
    return closed;
}

fn abi() -> Abi {
    var meta: i64[3] = [0, 0, 0];
    if native_abi(meta[..]) != 0 { return Abi { .. }; }
    return Abi { zlib: meta[0], stream_bytes: meta[1], live: meta[2] };
}

// The zlib runtime version this build links, for provenance records.
fn zlib_version() -> u8[>..] {
    var text:    u8[>..] = [];
    var scratch: u8[>..] = [];
    scratch.resize(32, 0);
    var meta: i64[1] = [0];
    if native_version(scratch[..], meta[..]) == 0 {
        for i in scratch.len {
            if scratch[i] == 0 { break; }
            text.push(scratch[i]);
        }
    }
    return text;
}

fn describe(error: Error) -> const u8[:] {
    if error == Error.None { return "none"; }
    if error == Error.InvalidInput { return "invalid input"; }
    if error == Error.LimitExceeded { return "limit exceeded"; }
    if error == Error.Malformed { return "malformed stream"; }
    if error == Error.Truncated { return "truncated stream"; }
    if error == Error.Checksum { return "checksum mismatch"; }
    if error == Error.TrailingData { return "trailing data"; }
    if error == Error.TooManyMembers { return "too many members"; }
    if error == Error.DictionaryRequired { return "dictionary required"; }
    if error == Error.DictionaryMismatch { return "dictionary mismatch"; }
    return "system error";
}

fn state_name(state: State) -> const u8[:] {
    if state == State.Idle { return "idle"; }
    if state == State.Active { return "active"; }
    if state == State.MemberEnd { return "member end"; }
    if state == State.Done { return "done"; }
    return "failed";
}
