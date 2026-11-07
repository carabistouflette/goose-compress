// SPDX-License-Identifier: Apache-2.0
// The documented contract, asserted point by point.
//
// `spec.goose` checks fixtures, `boundaries.goose` checks chunking, and
// `api.goose` checks the lifecycle. This file covers what those three leave
// out: the exact limit boundaries in both directions, the progress accounting
// the API reference promises, the state transitions after a terminal error,
// and the resource limits at their documented maximum.
//
// Every assertion here is in the direction the documentation promises. There
// is no "known gap" registry: a contract point the runtime does not meet is a
// failing check with a nonzero exit, not a printed note. The one such point
// today is the 33rd `open`, which must return `Error.SystemError`.
import compress from "..";
import os;
import std;

struct Outcome {
    error:    compress::Error = compress::Error.None,
    consumed: i64             = 0,
    produced: i64             = 0,
    members:  i64             = 0,
    trailing: i64             = 0,
}

fn load(path: const u8[:]) -> u8[>..] {
    var bytes: u8[>..] = [];
    if !read_file(path, bytes) {
        write_stderr(str("contract: cannot read ", path, "\n"));
        exit(1);
    }
    return bytes;
}

// Feeds `input` in slices of `chunk` bytes and reports the full progress, so
// that every documented invariant can be compared across chunkings.
fn run_chunked(format: compress::Format, limits: const compress::Limits&,
               dictionary: const u8[:], input: const u8[:], chunk: i64,
               out: u8[>..]&) -> Outcome {
    var outcome = Outcome { .. };
    var decoder = compress::Decoder { .. };
    outcome.error = compress::open_with_dictionary(decoder, format, limits, dictionary);
    if outcome.error != compress::Error.None { return outcome; }
    var offset: i64 = 0;
    loop {
        if offset >= input.len { break; }
        let end = min(offset + chunk, input.len);
        outcome.error = compress::feed(decoder, input[offset..end], out);
        if outcome.error != compress::Error.None { break; }
        offset = end;
    }
    if outcome.error == compress::Error.None { outcome.error = compress::finish(decoder); }
    outcome.consumed = decoder.progress.consumed;
    outcome.produced = decoder.progress.produced;
    outcome.members = decoder.progress.members;
    outcome.trailing = decoder.progress.trailing;
    let closed = compress::close(decoder);
    if outcome.error == compress::Error.None { outcome.error = closed; }
    return outcome;
}

// Compares the progress record across nine chunkings.
//
// docs/api.md scopes the chunking guarantee: the error, the appended bytes,
// produced, members and trailing are invariant; `consumed` is invariant too
// except when the call stops because `output_bytes` was reached, where it
// reports the input the failing step had available. That exception is
// asserted with its own exact rule rather than skipped.
fn check_chunking(name: const u8[:], format: compress::Format,
                  limits: const compress::Limits&, dictionary: const u8[:],
                  input: const u8[:]) -> i64 {
    let chunks = [1, 2, 3, 5, 7, 13, 64, 1024, 65536];
    var failures:  i64     = 0;
    var reference: u8[>..] = [];
    let base = run_chunked(format, limits, dictionary, input, 65536, reference);
    // The one documented exception: the output budget stopped the decode.
    let output_stopped = base.error == compress::Error.LimitExceeded &&
                         base.produced == limits.output_bytes;
    for chunk in chunks {
        var output: u8[>..] = [];
        let run = run_chunked(format, limits, dictionary, input, chunk, output);
        if run.error != base.error || output != reference ||
           run.produced != base.produced || run.members != base.members ||
           run.trailing != base.trailing {
            write_stderr(str("contract: ", name, " at chunk ", chunk, ": ",
                             compress::describe(base.error), "/", base.produced,
                             "/", base.members, "/", base.trailing, " became ",
                             compress::describe(run.error), "/", run.produced,
                             "/", run.members, "/", run.trailing, "\n"));
            failures += 1;
        }
        if output_stopped {
            // Exact rule for the exception: the budget is what stopped it, and
            // consumed stays inside both the budget and the supplied input.
            if run.produced != limits.output_bytes ||
               run.consumed > limits.input_bytes || run.consumed > input.len {
                write_stderr(str("contract: ", name, " at chunk ", chunk,
                                 ": output-budget stop broke its rule: produced ",
                                 run.produced, " of ", limits.output_bytes,
                                 " consumed ", run.consumed, " of ",
                                 limits.input_bytes, "\n"));
                failures += 1;
            }
        } else if run.consumed != base.consumed {
            write_stderr(str("contract: ", name, " at chunk ", chunk,
                             ": consumed ", base.consumed, " became ",
                             run.consumed, "\n"));
            failures += 1;
        }
    }
    return failures;
}

fn check_error_name(error: compress::Error, expected: const u8[:]) -> i64 {
    if compress::describe(error) != expected {
        write_stderr(str("contract: describe(", expected, ") is wrong\n"));
        return 1;
    }
    return 0;
}

fn check_state_name(state: compress::State, expected: const u8[:]) -> i64 {
    if compress::state_name(state) != expected {
        write_stderr(str("contract: state_name(", expected, ") is wrong\n"));
        return 1;
    }
    return 0;
}

fn main() {
    var failures: i64 = 0;
    var checks:   i64 = 0;
    let defaults = compress::Limits { .. };

    let text_in = load(".build/fixtures/gzip-text.in");
    let text_out = load(".build/fixtures/gzip-text.out");
    let empty_in = load(".build/fixtures/gzip-empty.in");
    let one_in = load(".build/fixtures/gzip-one-byte.in");
    let raw_dict_in = load(".build/fixtures/raw-dict.in");
    let raw_dict_out = load(".build/fixtures/raw-dict.out");
    let raw_dict = load(".build/fixtures/raw-dict.dict");

    // ---- Exact output budget, both sides of the boundary -------------------
    // The payload is `text_out.len` bytes. One byte short must fail and keep
    // exactly that many bytes; exact and one over must both succeed.
    let payload = text_out.len;
    let short = compress::Limits { output_bytes: payload - 1 };
    let exact = compress::Limits { output_bytes: payload };
    let over = compress::Limits { output_bytes: payload + 1 };
    var out: u8[>..] = [];
    let short_run = run_chunked(compress::Format.Gzip, short, "", text_in[..], 65536, out);
    checks += 1;
    if short_run.error != compress::Error.LimitExceeded || out.len != payload - 1 ||
       short_run.produced != payload - 1 {
        write_stderr(str("contract: output budget -1: ", compress::describe(short_run.error),
                         " produced ", short_run.produced, " bytes ", out.len, "\n"));
        failures += 1;
    }
    out.clear();
    let exact_run = run_chunked(compress::Format.Gzip, exact, "", text_in[..], 65536, out);
    checks += 1;
    if exact_run.error != compress::Error.None || out != text_out ||
       exact_run.produced != payload {
        write_stderr(str("contract: output budget exact: ",
                         compress::describe(exact_run.error), " produced ",
                         exact_run.produced, "\n"));
        failures += 1;
    }
    out.clear();
    let over_run = run_chunked(compress::Format.Gzip, over, "", text_in[..], 65536, out);
    checks += 1;
    if over_run.error != compress::Error.None || out != text_out ||
       over_run.produced != payload {
        write_stderr(str("contract: output budget +1: ",
                         compress::describe(over_run.error), "\n"));
        failures += 1;
    }

    // ---- Exact input budget, both sides of the boundary --------------------
    let stream = text_in.len;
    let input_short = compress::Limits { input_bytes: stream - 1 };
    let input_exact = compress::Limits { input_bytes: stream };
    let input_over = compress::Limits { input_bytes: stream + 1 };
    out.clear();
    let ishort = run_chunked(compress::Format.Gzip, input_short, "", text_in[..], 65536, out);
    checks += 1;
    if ishort.error != compress::Error.LimitExceeded || ishort.consumed != stream - 1 {
        write_stderr(str("contract: input budget -1: ", compress::describe(ishort.error),
                         " consumed ", ishort.consumed, "\n"));
        failures += 1;
    }
    out.clear();
    let iexact = run_chunked(compress::Format.Gzip, input_exact, "", text_in[..], 65536, out);
    checks += 1;
    if iexact.error != compress::Error.None || iexact.consumed != stream {
        write_stderr(str("contract: input budget exact: ", compress::describe(iexact.error),
                         " consumed ", iexact.consumed, "\n"));
        failures += 1;
    }
    out.clear();
    let iover = run_chunked(compress::Format.Gzip, input_over, "", text_in[..], 65536, out);
    checks += 1;
    if iover.error != compress::Error.None || iover.consumed != stream {
        write_stderr(str("contract: input budget +1: ", compress::describe(iover.error),
                         " consumed ", iover.consumed, "\n"));
        failures += 1;
    }

    // ---- Progress accounting ----------------------------------------------
    // `produced` counts bytes appended to the caller's builder, so it must
    // equal the growth of `out` exactly, and neither budget may be exceeded.
    out.clear();
    let clean = run_chunked(compress::Format.Gzip, defaults, "", text_in[..], 65536, out);
    checks += 1;
    if clean.produced != out.len {
        write_stderr(str("contract: produced ", clean.produced, " but out grew by ",
                         out.len, "\n"));
        failures += 1;
    }
    checks += 1;
    if clean.consumed > defaults.input_bytes || clean.produced > defaults.output_bytes {
        write_stderr("contract: progress exceeded a budget\n");
        failures += 1;
    }
    checks += 1;
    if clean.members != 1 || clean.trailing != 0 {
        write_stderr(str("contract: members ", clean.members, " trailing ",
                         clean.trailing, "\n"));
        failures += 1;
    }

    // ---- Chunking invariance of the whole progress record -----------------
    let allow = compress::Limits { trailing: compress::Trailing.Allow };
    let two = compress::Limits { trailing: compress::Trailing.Members, members: 2 };
    let out_short = compress::Limits { output_bytes: payload - 1 };
    let in_short = compress::Limits { input_bytes: stream - 1 };
    let trailing_allow_in = load(".build/fixtures/gzip-trailing-allow.in");
    let two_members_in = load(".build/fixtures/gzip-two-members.in");
    failures += check_chunking("gzip-text", compress::Format.Gzip, defaults, "",
                               text_in[..]);
    failures += check_chunking("gzip-empty", compress::Format.Gzip, defaults, "",
                               empty_in[..]);
    failures += check_chunking("gzip-one-byte", compress::Format.Gzip, defaults, "",
                               one_in[..]);
    failures += check_chunking("gzip-trailing-allow", compress::Format.Gzip, allow, "",
                               trailing_allow_in[..]);
    failures += check_chunking("gzip-two-members", compress::Format.Gzip, two, "",
                               two_members_in[..]);
    failures += check_chunking("gzip-output-short", compress::Format.Gzip, out_short, "",
                               text_in[..]);
    failures += check_chunking("gzip-input-short", compress::Format.Gzip, in_short, "",
                               text_in[..]);
    checks += 7 * 9;

    // ---- State machine after a terminal error -----------------------------
    var failed = compress::Decoder { .. };
    let tight = compress::Limits { output_bytes: 4 };
    if compress::open(failed, compress::Format.Gzip, tight) != compress::Error.None {
        write_stderr("contract: open failed\n");
        failures += 1;
    }
    var sink: u8[>..] = [];
    let first = compress::feed(failed, text_in[..], sink);
    checks += 1;
    if first != compress::Error.LimitExceeded ||
       failed.progress.state != compress::State.Failed {
        write_stderr(str("contract: expected a terminal failure, got ",
                         compress::describe(first), "\n"));
        failures += 1;
    }
    // A second feed repeats the original error rather than doing more work.
    checks += 1;
    if compress::feed(failed, text_in[..], sink) != first {
        write_stderr("contract: a failed decoder must repeat its error\n");
        failures += 1;
    }
    // finish on a failed decoder reports the original error, not Truncated.
    checks += 1;
    if compress::finish(failed) != first {
        write_stderr("contract: finish on a failed decoder must repeat the error\n");
        failures += 1;
    }
    // close still works and returns the decoder to Idle.
    checks += 1;
    if compress::close(failed) != compress::Error.None ||
       failed.progress.state != compress::State.Idle || failed.handle != 0 {
        write_stderr("contract: a failed decoder must still close cleanly\n");
        failures += 1;
    }

    // ---- Done is terminal for feed, idempotent for finish -----------------
    var done = compress::Decoder { .. };
    if compress::open(done, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("contract: open failed\n");
        failures += 1;
    }
    var body: u8[>..] = [];
    compress::feed(done, text_in[..], body);
    checks += 1;
    if compress::finish(done) != compress::Error.None ||
       done.progress.state != compress::State.Done {
        write_stderr("contract: finish must reach Done\n");
        failures += 1;
    }
    checks += 1;
    if compress::finish(done) != compress::Error.None {
        write_stderr("contract: finish on Done must be idempotent\n");
        failures += 1;
    }
    checks += 1;
    if compress::feed(done, text_in[..], body) != compress::Error.InvalidInput {
        write_stderr("contract: feed after Done must be invalid input\n");
        failures += 1;
    }
    compress::close(done);

    // ---- A closed decoder is reusable -------------------------------------
    var reused = compress::Decoder { .. };
    if compress::open(reused, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("contract: open failed\n");
        failures += 1;
    }
    compress::close(reused);
    checks += 1;
    if compress::open(reused, compress::Format.Gzip, defaults) != compress::Error.None ||
       reused.progress.consumed != 0 || reused.progress.produced != 0 ||
       reused.progress.members != 0 || reused.progress.trailing != 0 {
        write_stderr("contract: reopening a closed decoder must reset progress\n");
        failures += 1;
    }
    compress::close(reused);

    // ---- An empty feed in Active is a no-op, not an error -----------------
    var idle_feed = compress::Decoder { .. };
    if compress::open(idle_feed, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("contract: open failed\n");
        failures += 1;
    }
    var nothing: u8[>..] = [];
    checks += 1;
    if compress::feed(idle_feed, "", nothing) != compress::Error.None ||
       idle_feed.progress.state != compress::State.Active ||
       idle_feed.progress.consumed != 0 {
        write_stderr("contract: an empty feed must be a no-op\n");
        failures += 1;
    }
    compress::close(idle_feed);

    // ---- Documented maxima are accepted, one past is refused --------------
    let max_output = compress::Limits { output_bytes: 1073741824 };
    let max_input = compress::Limits { input_bytes: 1073741824 };
    let max_members = compress::Limits { trailing: compress::Trailing.Members,
                                         members: 1024 };
    var probe = compress::Decoder { .. };
    checks += 1;
    if compress::open(probe, compress::Format.Gzip, max_output) != compress::Error.None {
        write_stderr("contract: output_bytes = 1 GiB must be accepted\n");
        failures += 1;
    }
    compress::close(probe);
    checks += 1;
    if compress::open(probe, compress::Format.Gzip, max_input) != compress::Error.None {
        write_stderr("contract: input_bytes = 1 GiB must be accepted\n");
        failures += 1;
    }
    compress::close(probe);
    checks += 1;
    if compress::open(probe, compress::Format.Gzip, max_members) != compress::Error.None {
        write_stderr("contract: members = 1024 must be accepted\n");
        failures += 1;
    }
    compress::close(probe);

    // ---- Dictionary boundary: 32768 accepted, 32769 refused ---------------
    var dictionary: u8[>..] = [];
    dictionary.resize(32768, 0x41);
    checks += 1;
    if compress::open_with_dictionary(probe, compress::Format.Raw, defaults,
                                      dictionary[..]) != compress::Error.None {
        write_stderr("contract: a 32768-byte dictionary must be accepted\n");
        failures += 1;
    }
    compress::close(probe);
    dictionary.resize(32769, 0x41);
    checks += 1;
    if compress::open_with_dictionary(probe, compress::Format.Raw, defaults,
                                      dictionary[..]) != compress::Error.InvalidInput ||
       probe.handle != 0 {
        write_stderr("contract: a 32769-byte dictionary must be refused\n");
        failures += 1;
    }

    // ---- A stale handle is rejected, not dereferenced ----------------------
    // `close` zeroes the handle it is handed, but a caller can copy the struct
    // first. The copy then holds a handle whose slot has been retired. The
    // adapter must reject it by generation rather than dereference freed
    // memory -- and the generation is the only thing that can tell a retired
    // handle from the decoder that later took over its slot.
    //
    // Each case needs its own copy: a rejected `feed` marks the decoder
    // Failed, and a Failed decoder short-circuits before reaching the adapter,
    // so reusing one copy would test the sticky-error path instead.
    var original = compress::Decoder { .. };
    checks += 1;
    if compress::open(original, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("contract: could not open a decoder for the stale-handle check\n");
        failures += 1;
    }
    var stale = original;
    compress::close(original);
    var stale_out: u8[>..] = [];
    checks += 1;
    if compress::feed(stale, text_in[..], stale_out) != compress::Error.InvalidInput {
        write_stderr("contract: a stale handle must be refused with invalid input\n");
        failures += 1;
    }
    checks += 1;
    if stale_out.len != 0 {
        write_stderr("contract: a stale handle must not produce output\n");
        failures += 1;
    }
    checks += 1;
    if compress::finish(stale) != compress::Error.InvalidInput {
        write_stderr("contract: finish on a stale handle must be refused\n");
        failures += 1;
    }

    // The harder case: the slot is reused. `close` nulls the slot, so a check
    // that only tests for a null slot still passes the case above. Here a new
    // decoder occupies the same slot with a newer generation, and the stale
    // handle must not resolve to it.
    var retired = compress::Decoder { .. };
    checks += 1;
    if compress::open(retired, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("contract: could not open the decoder to be retired\n");
        failures += 1;
    }
    var retired_handle = retired.handle;
    var reused_stale = retired;
    compress::close(retired);
    var replacement = compress::Decoder { .. };
    checks += 1;
    if compress::open(replacement, compress::Format.Gzip, defaults) != compress::Error.None {
        write_stderr("contract: could not reopen the slot for the stale-handle check\n");
        failures += 1;
    }
    checks += 1;
    if replacement.handle == retired_handle {
        write_stderr("contract: the replacement did not reuse the retired slot\n");
        failures += 1;
    }
    var reused_out: u8[>..] = [];
    checks += 1;
    if compress::feed(reused_stale, text_in[..], reused_out) != compress::Error.InvalidInput {
        write_stderr("contract: a stale handle must not resolve to the decoder that reused its slot\n");
        failures += 1;
    }
    checks += 1;
    if reused_out.len != 0 {
        write_stderr("contract: a stale handle must not decode into the replacement\n");
        failures += 1;
    }
    // The replacement must still be usable: the stale call must not have
    // disturbed it.
    var replacement_out: u8[>..] = [];
    checks += 1;
    if compress::feed(replacement, text_in[..], replacement_out) != compress::Error.None ||
       replacement_out.len != 168 {
        write_stderr("contract: the replacement decoder was disturbed by the stale handle\n");
        failures += 1;
    }
    compress::close(replacement);

    // ---- Raw multi-member stream with a preset dictionary -----------------
    // inflateReset drops the window, and a raw stream carries no DICTID to
    // ask for the dictionary again, so the adapter must re-install it. This
    // is the only path that exercises that, and nothing covered it before.
    var two_members: u8[>..] = [];
    two_members.append(raw_dict_in);
    two_members.append(raw_dict_in);
    let members_limits = compress::Limits { trailing: compress::Trailing.Members,
                                            members: 2 };
    var raw_out: u8[>..] = [];
    let raw_run = run_chunked(compress::Format.Raw, members_limits, raw_dict[..],
                              two_members[..], 65536, raw_out);
    var expected_raw: u8[>..] = [];
    expected_raw.append(raw_dict_out);
    expected_raw.append(raw_dict_out);
    checks += 1;
    if raw_run.error != compress::Error.None || raw_out != expected_raw ||
       raw_run.members != 2 {
        write_stderr(str("contract: raw multi-member with a dictionary: ",
                         compress::describe(raw_run.error), " members ",
                         raw_run.members, " bytes ", raw_out.len, " of ",
                         expected_raw.len, "\n"));
        failures += 1;
    }

    // ---- decompress appends to a non-empty builder ------------------------
    var prefilled: u8[>..] = [];
    prefilled.append("prefix");
    checks += 1;
    if compress::decompress(text_in[..], compress::Format.Gzip, defaults, prefilled) !=
       compress::Error.None || prefilled.len != 6 + payload {
        write_stderr(str("contract: decompress must append, got ", prefilled.len,
                         " bytes\n"));
        failures += 1;
    }

    // ---- abi().live is unchanged by a refused open ------------------------
    let before = compress::abi().live;
    var refused = compress::Decoder { .. };
    let bad = compress::Limits { output_bytes: -1 };
    compress::open(refused, compress::Format.Gzip, bad);
    checks += 1;
    if compress::abi().live != before || refused.handle != 0 {
        write_stderr("contract: a refused open must not consume a slot\n");
        failures += 1;
    }

    // ---- Stable names -----------------------------------------------------
    checks += 11;
    failures += check_error_name(compress::Error.None, "none");
    failures += check_error_name(compress::Error.InvalidInput, "invalid input");
    failures += check_error_name(compress::Error.LimitExceeded, "limit exceeded");
    failures += check_error_name(compress::Error.Malformed, "malformed stream");
    failures += check_error_name(compress::Error.Truncated, "truncated stream");
    failures += check_error_name(compress::Error.Checksum, "checksum mismatch");
    failures += check_error_name(compress::Error.TrailingData, "trailing data");
    failures += check_error_name(compress::Error.TooManyMembers, "too many members");
    failures += check_error_name(compress::Error.DictionaryRequired, "dictionary required");
    failures += check_error_name(compress::Error.DictionaryMismatch, "dictionary mismatch");
    failures += check_error_name(compress::Error.SystemError, "system error");
    checks += 5;
    failures += check_state_name(compress::State.Idle, "idle");
    failures += check_state_name(compress::State.Active, "active");
    failures += check_state_name(compress::State.MemberEnd, "member end");
    failures += check_state_name(compress::State.Done, "done");
    failures += check_state_name(compress::State.Failed, "failed");

    // ---- Table exhaustion -------------------------------------------------
    // README, docs/api.md and docs/design.md all promise Error.SystemError for
    // a 33rd concurrent decoder. The adapter reports GC_LIMIT, which the Goose
    // layer maps to Error.LimitExceeded, so this fails until gc_open's
    // table-full branch sets meta.data[0] = GC_SYSTEM. Reproduced in
    // .build/probe/limit.goose.
    let limits = compress::Limits { .. };
    var held: compress::Decoder[>..] = [];
    var refusal = compress::Error.None;
    for i in 40 {
        var slot = compress::Decoder { .. };
        let opened = compress::open(slot, compress::Format.Raw, limits);
        if opened == compress::Error.None { held.push(slot); }
        else if refusal == compress::Error.None { refusal = opened; }
    }
    checks += 1;
    if held.len != 32 {
        write_stderr(str("contract: expected 32 open decoders, got ", held.len, "\n"));
        failures += 1;
    }
    checks += 1;
    if refusal != compress::Error.SystemError {
        write_stderr(str("contract: the 33rd open returned ",
                         compress::describe(refusal),
                         ", but the documented contract is system error\n"));
        failures += 1;
    }
    for i in held.len { compress::close(held[i]); }

    // ---- Cleanup ----------------------------------------------------------
    checks += 1;
    if compress::abi().live != 0 {
        write_stderr(str("contract: ", compress::abi().live, " decoders left open\n"));
        failures += 1;
    }

    if failures != 0 {
        write_stderr(str("contract: ", failures, " of ", checks, " checks failed\n"));
        exit(1);
    }
    print("contract ok: ", checks, " checks\n");
}
