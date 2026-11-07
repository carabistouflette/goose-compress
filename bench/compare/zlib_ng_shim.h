/* SPDX-License-Identifier: Apache-2.0 */
/* zlib-ng installed with the `zng_` symbol prefix, as Arch's package does.
 * The status constants are unprefixed, so only the stream type and the entry
 * points need mapping. This is the same shape zlib-ng's own compat build
 * provides; it exists here so the reference row can be compiled against a
 * second backend without a second copy of the state machine. */
#ifndef ZLIB_NG_SHIM_H
#define ZLIB_NG_SHIM_H

#include <zlib-ng.h>

typedef zng_stream z_stream;

#define inflateInit2 zng_inflateInit2
#define inflate zng_inflate
#define inflateReset zng_inflateReset
#define inflateEnd zng_inflateEnd
#define zlibVersion zlibng_version

#endif
