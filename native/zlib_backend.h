/* SPDX-License-Identifier: Apache-2.0 */
/* Which zlib the adapter is compiled against.
 *
 * The default is the system zlib, which is what the package links and what
 * every published number was measured on. Defining GC_ZLIB_NG compiles the
 * same adapter against zlib-ng, which ships the same state machine under a
 * `zng_` prefix and a slightly different stream layout. Nothing above this
 * header changes: the handle table, the generation check, the per-decoder
 * lock, the budgets and the error taxonomy are the same code.
 *
 * This exists so the backend can be measured and tested rather than assumed.
 * It is not the default, because zlib-ng is not installed everywhere and
 * because the maintained codec is zlib.
 */
#ifndef GC_ZLIB_BACKEND_H
#define GC_ZLIB_BACKEND_H

#ifdef GC_ZLIB_NG

#include <zlib-ng.h>

/* zlib-ng keeps the status constants unprefixed and renames the stream and the
 * entry points. The typedefs below are the zlib spellings the adapter uses;
 * zlib-ng's own compatibility build provides the same mapping. */
typedef zng_stream z_stream;
typedef uint8_t Bytef;
typedef uint32_t uInt;
typedef unsigned long uLong;

#define inflateInit2 zng_inflateInit2
#define inflate zng_inflate
#define inflateReset zng_inflateReset
#define inflateEnd zng_inflateEnd
#define inflateSetDictionary zng_inflateSetDictionary
#define adler32 zng_adler32
#define crc32 zng_crc32
#define zlibVersion zlibng_version

/* The linker symbol behind `inflate`, for tests that wrap it. */
#define GC_INFLATE_SYMBOL zng_inflate

#else

#include <zlib.h>

#define GC_INFLATE_SYMBOL inflate

#endif

#endif
