#!/bin/sh
# Builds the fork's Wine for the images that carry it (the beta and the
# :latest-patched stable tag). One script so the two builder stages cannot
# drift apart.
#
#   build-wine.sh deps              install the toolchain and -dev libraries
#   build-wine.sh build             clone WINE_REF, apply the series, build, install
#   build-wine.sh apply SRC MANIFEST  apply the series to a checkout (for tests)
#
# Install prefix is /opt/wine. The manifest, /opt/wine/share/bb64-patches,
# records the Wine source and what happened to each patch:
#   applied <name> switch    applied, with its runtime switch
#   applied <name> builtin   applied, the switch is part of the patch
#   upstream <name>          the source already has it, so it was skipped
# The container reads it to know which switches this Wine has.
set -eu

PATCHES="$(cd "$(dirname "$0")" && pwd)"

apply_series() {
    src="$1"; manifest="$2"
    : > "$manifest"
    switches=""
    for name in $(grep -v '^#' "$PATCHES/series" | grep -v '^$'); do
        p="$PATCHES/$name.patch"
        [ -f "$p" ] || { echo "build-wine: $name is in series but $p is missing" >&2; exit 1; }
        # Already in the source: applying it in reverse works. Skip it and its
        # switch, which would otherwise gate code that is now upstream's.
        if git -C "$src" apply --reverse --check "$p" 2>/dev/null; then
            echo "build-wine: $name is already in this Wine, skipped"
            echo "upstream $name" >> "$manifest"
            continue
        fi
        # Its own --check first, so a context mismatch fails the build with the
        # patch named rather than half-applying it.
        if ! git -C "$src" apply --check "$p"; then
            echo "build-wine: $name does not apply to this Wine source; regenerate it" >&2
            exit 1
        fi
        git -C "$src" apply "$p"
        if [ -f "$PATCHES/$name.switch.patch" ]; then
            switches="$switches $name"
            echo "applied $name switch" >> "$manifest"
        else
            echo "applied $name builtin" >> "$manifest"
        fi
        echo "build-wine: applied $name"
    done
    for name in $switches; do
        p="$PATCHES/$name.switch.patch"
        if ! git -C "$src" apply --check "$p"; then
            echo "build-wine: $name.switch does not apply; regenerate it against the stack" >&2
            exit 1
        fi
        git -C "$src" apply "$p"
        echo "build-wine: applied $name.switch"
    done
}

case "${1:-}" in
deps)
    # Toolchain plus the -dev libraries Wine links against. Scoped to fonts,
    # TLS and the X stack Backblaze's GUI needs: no OpenGL, sound or scanner
    # dev packages, so configure leaves those subsystems out and the runtime
    # library set stays small.
    apt-get update
    apt-get install -y --no-install-recommends \
        ca-certificates git gcc flex bison make pkg-config python3 \
        gcc-mingw-w64-x86-64 \
        libfreetype-dev libfontconfig-dev libgnutls28-dev libpng-dev \
        libx11-dev libxext-dev libxrender-dev libxrandr-dev libxi-dev \
        libxfixes-dev libxcursor-dev libxinerama-dev libxcomposite-dev \
        libxxf86vm-dev
    rm -rf /var/lib/apt/lists/*
    ;;
build)
    : "${WINE_REF:?WINE_REF must name the Wine tag, branch or commit to build}"
    work=/wine
    mkdir -p "$work"
    # Partial (blobless) clone keeps the checkout small while still allowing
    # WINE_REF to be any tag, branch or commit. Blobs arrive on checkout.
    git clone --filter=blob:none https://gitlab.winehq.org/wine/wine.git "$work/src"
    git -C "$work/src" checkout "$WINE_REF"
    apply_series "$work/src" "$work/bb64-patches"

    # Cap build parallelism by available RAM. Wine's parallel link steps are
    # memory-heavy, so -j<all cores> on a host with many cores and modest RAM
    # thrashes into swap. Default to about one job per 2 GB, never more than
    # the core count. Override with MAKE_JOBS.
    mkdir -p "$work/build"
    cd "$work/build"
    ../src/configure --enable-win64 --disable-tests --prefix=/opt/wine
    cpus="$(nproc)"
    mem_gb="$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo)"
    jobs="${MAKE_JOBS:-$(( mem_gb / 2 ))}"
    [ "$jobs" -ge 1 ] || jobs=1
    [ "$jobs" -le "$cpus" ] || jobs="$cpus"
    echo "Building Wine with -j${jobs} (cpus=${cpus}, mem=${mem_gb}GB; override with MAKE_JOBS)"
    make -j"$jobs"
    make install
    rm -rf /opt/wine/share/man /opt/wine/include
    { echo "wine_ref $WINE_REF"; cat "$work/bb64-patches"; } > /opt/wine/share/bb64-patches
    ;;
apply)
    apply_series "$2" "$3"
    ;;
*)
    echo "usage: build-wine.sh deps | build | apply SRC MANIFEST" >&2
    exit 2
    ;;
esac
