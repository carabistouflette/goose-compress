# Third-party notices

This repository's code is Apache-2.0. It does not vendor zlib, libdeflate or the
Goose compiler.

## zlib

The library links the system zlib at build time (`-lz`) and calls `inflateInit2`,
`inflate`, `inflateSetDictionary`, `inflateReset`, `inflateEnd`, `zlibVersion`
and `zlibCompileFlags`. zlib is distributed under the zlib license:

> Copyright (C) 1995-2024 Jean-loup Gailly and Mark Adler
>
> This software is provided 'as-is', without any express or implied warranty. In
> no event will the authors be held liable for any damages arising from the use
> of this software.
>
> Permission is granted to anyone to use this software for any purpose,
> including commercial applications, and to alter it and redistribute it
> freely, subject to the following restrictions:
>
> 1. The origin of this software must not be misrepresented; you must not claim
>    that you wrote the original software. If you use this software in a
>    product, an acknowledgment in the product documentation would be
>    appreciated but is not required.
> 2. Altered source versions must be plainly marked as such, and must not be
>    misrepresented as being the original software.
> 3. This notice may not be removed or altered from any source distribution.

Distributors must preserve the notices and licenses of the actual zlib build
they ship. Consult the installed package's license file and the upstream zlib
distribution; this notice does not replace them.

## libdeflate

`bench/compare/libdeflate_oneshot.c` links libdeflate (`-ldeflate`) for the
benchmark only. No library code depends on it, and it is not required to build
or use goose-compress. libdeflate is distributed under the MIT license:

> Copyright 2016 Eric Biggers
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Goose compiler

The Goose compiler is a separately obtained development tool at the exact
commit in `GOOSE_REVISION`. It is not vendored here and carries its own license
and notices. `scripts/setup-goose.sh` fetches and builds it from its upstream
repository.

## CPython

`scripts/oracle.py`, `scripts/fixtures.py`, `scripts/corpus.py`,
`tests/test_compress.py` and `bench/measure.py` use CPython's `zlib` module as
the DEFLATE reference. CPython is distributed under the PSF License Agreement.
It is a development and validation dependency, not a runtime dependency of the
library.
