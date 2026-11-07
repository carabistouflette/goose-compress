#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
revision=$(cat GOOSE_REVISION)
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "GOOSE_REVISION must contain a full lowercase 40-character commit hash" >&2
    exit 1
fi
toolchain_dir="$PWD/.toolchain/goose"

require_clean_source() {
    local source_status index_flags entry
    index_flags=$(git -C "$toolchain_dir" ls-files -v)
    while IFS= read -r entry; do
        # Lowercase tags hide assume-unchanged files; S hides skip-worktree.
        case "${entry:0:1}" in
            [a-z]|S)
                echo "Compiler index hides source checks: $toolchain_dir" >&2
                echo "Clear assume-unchanged/skip-worktree flags before setup." >&2
                exit 1
                ;;
        esac
    done <<< "$index_flags"
    source_status=$(git -C "$toolchain_dir" status --porcelain --untracked-files=normal)
    if [[ -n "$source_status" ]]; then
        echo "Compiler source is modified: $toolchain_dir" >&2
        echo "Preserve or remove local source changes before setup; build output ignored by Git is allowed." >&2
        exit 1
    fi
}

if [[ ! -d "$toolchain_dir/.git" ]]; then
    git init "$toolchain_dir"
    git -C "$toolchain_dir" remote add origin https://github.com/aardappel/goose.git
fi
require_clean_source
git -C "$toolchain_dir" fetch --depth 1 origin "$revision"
git -C "$toolchain_dir" checkout --detach "$revision"
require_clean_source
cmake -S "$toolchain_dir" -B "$toolchain_dir/build" \
    -DCMAKE_BUILD_TYPE=Release -DGOOSE_BUILTIN_LIBS=none
cmake --build "$toolchain_dir/build" --parallel "${JOBS:-2}"
