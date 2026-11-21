#!/usr/bin/env bash
# Get the exact declaration graph of the release from the community build cache, with no full build.
#
# Stages, in order of priority:
#   selftest Show that the sandbox has no network and no write access outside its output, while the VM is
#            online. Also runs the tests of rhcache.py. Opens no cache file.
#   prepare  Needs the network. Check the source, get Mathlib from its official cache, download the pinned
#            community cache (4.94 GB), check every archive entry, unpack, and check the tree. Runs no
#            program on the cache files outside the sandbox.
#   extract  No network. Run `rh extract` in the sandbox: the exact declaration graph.
#   axioms   No network. Run `rh trust` without the replay: #print axioms and the axiom walk.
#   replay   No network. Run `rh trust` with the leanchecker of the toolchain (--fresh Main). It needs
#            much memory and time; run it only after a measurement on a smaller target.
#
# All results come from build files that other people made. The records say so (rh -artifacts).
#
# Usage: run.sh STAGE    Environment: WORK (default /srv/rh), RH (default $WORK/bin/rh),
#                        LEAN_HOME (default /opt/lean-4.33.1), JOBS (default: the number of CPUs).
set -euo pipefail
umask 022

STAGE="${1:?usage: run.sh selftest|prepare|extract|axioms|replay}"
WORK="${WORK:-/srv/rh}"
RH="${RH:-$WORK/bin/rh}"
LEAN_HOME="${LEAN_HOME:-/opt/lean-4.33.1}"
JOBS="${JOBS:-$(nproc)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$WORK/proj"   # leanprover-community/riemann-hypothesis at REV, with the unpacked cache
DL="$WORK/dl"       # downloads: manifests and the cache archives
OUT="$WORK/out"     # results
RECORDS="$WORK/records/index"

# The pins. A change to any of them is a change of the source, and every stage must run again.
REPO=https://github.com/leanprover-community/riemann-hypothesis
REV=e50d09817ad81469f737e8f637561e6ccf86c670
SRC_TREE=24df20b62d9976ccd20da819bc20b16f808f0fdd        # git tree of src at REV: the release plus Challenge.lean
RELEASE_TREE=b41b516b1f7baee4d83ced235ecc71fd43c97695    # git tree of lean/src in tamnd/riemann-lean 9110120
TOOLCHAIN=leanprover/lean4:v4.33.1
LEAN_COMMIT=819816b2e0a3bf405af45ae5c7af2491d8f5bee6
DEPS_SHA=fee57ee9ed399d0a638ca955e5ad40a37a36eeb9b6dfd9f5964114fa7bbe4774      # lake-manifest.json, the same as the release
LAKEFILE_SHA=32af981ad33964a949f3e4d1e188e4afd0f85fecf6bf86e8ed133f00f16727ca  # lakefile.toml at REV
RECORDS_URL=https://github.com/tamnd/riemann/releases/download/records-9110120-r2/records.tar
RECORDS_SHA=8f71abe55be31ae951a2a1be9b63eb557864df136c53dca2f60e7bbef999c040    # tamnd/riemann manifests/records.yaml
ILEAN_URL=https://huggingface.co/datasets/JackMcCarthy/riemann-hypothesis-build/resolve/9036e64326acf4acfae159b96664e9708445c1d1/olean-manifest.sha256
ILEAN_SHA=fb3609ad73a17102e6e3e2346c5358a9c0957bcf5945442a7e5ffaa00c441ff5

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
py() { python3 -I "$HERE/rhcache.py" "$@"; }

[ -x "$LEAN_HOME/bin/lean" ] || die "no Lean at $LEAN_HOME"
"$LEAN_HOME/bin/lean" --version | grep -q "$LEAN_COMMIT" || die "the Lean binary is not commit $LEAN_COMMIT"
command -v bwrap >/dev/null || die "install bubblewrap"
ulimit -n "$(ulimit -Hn)"
[ "$(cat /proc/sys/vm/max_map_count)" -ge 1048576 ] || die "set vm.max_map_count to 1048576: Lean maps one region for each of about 183,000 modules"
mkdir -p "$DL" "$OUT"
rm -f "$OUT/$STAGE.status"
trap 'echo $? > "$OUT/$STAGE.status"' EXIT  # the host reads this file: 0 is success

# The sandbox: no network, no capabilities, a read-only system and toolchain, and write access only to the
# directories in the WRITE array. Paths keep their names, so the job list and the flags of rh need no change.
sandbox() {
  local args=(--unshare-all --die-with-parent --new-session --cap-drop ALL
    --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64
    --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group
    --proc /proc --dev /dev --tmpfs /tmp
    --ro-bind "$LEAN_HOME" "$LEAN_HOME" --ro-bind "$PROJ" "$PROJ" --ro-bind "$DL" "$DL")
  [ -e "$RH" ] && args+=(--ro-bind "$RH" "$RH")
  [ -d "$RECORDS" ] && args+=(--ro-bind "$RECORDS" "$RECORDS")
  local w
  for w in "${WRITE[@]}"; do args+=(--bind "$w" "$w"); done
  # The root of the sandbox is a tmpfs that holds the mount points; make it read-only too. Only the binds
  # in WRITE and /tmp stay writable.
  args+=(--remount-ro /)
  bwrap "${args[@]}" --setenv HOME /tmp --setenv PATH "$LEAN_HOME/bin:/usr/bin:/bin" --chdir "$PROJ" "$@"
}

check_source() {
  [ "$(git -C "$PROJ" rev-parse HEAD)" = "$REV" ] || die "the clone is not at $REV"
  [ "$(git -C "$PROJ" rev-parse HEAD:src)" = "$SRC_TREE" ] || die "src is not the pinned tree"
  local idx; idx="$(mktemp)"
  GIT_INDEX_FILE="$idx" git -C "$PROJ" read-tree "HEAD:src"
  GIT_INDEX_FILE="$idx" git -C "$PROJ" rm -q --cached Challenge.lean
  [ "$(GIT_INDEX_FILE="$idx" git -C "$PROJ" write-tree)" = "$RELEASE_TREE" ] || die "src without Challenge.lean is not the release"
  rm -f "$idx"
  git -C "$PROJ" diff --quiet HEAD -- src lakefile.toml lake-manifest.json lean-toolchain || die "the work tree has changes"
  [ "$(tr -d '\n' < "$PROJ/lean-toolchain")" = "$TOOLCHAIN" ] || die "the toolchain differs"
  [ "$(sha "$PROJ/lake-manifest.json")" = "$DEPS_SHA" ] || die "lake-manifest.json differs"
  [ "$(sha "$PROJ/lakefile.toml")" = "$LAKEFILE_SHA" ] || die "lakefile.toml differs"
}

stage_prepare() {
  # 1. The source.
  if [ ! -d "$PROJ/.git" ]; then
    log "clone $REPO"
    git clone -q --filter=blob:none --no-checkout "$REPO" "$PROJ"
    git -C "$PROJ" -c advice.detachedHead=false checkout -q "$REV"
  fi
  check_source
  log "source: the release ($RELEASE_TREE) plus Challenge.lean; toolchain, lakefile and dependencies match the pins"

  # 2. The records of rh index for riemann-lean 9110120.
  if [ ! -d "$RECORDS" ]; then
    curl -fsSL --retry 5 -o "$DL/records.tar" "$RECORDS_URL"
    [ "$(sha "$DL/records.tar")" = "$RECORDS_SHA" ] || die "records.tar does not match its pin"
    python3 -I -c 'import sys, tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2], filter="data")' "$DL/records.tar" "$WORK"
    [ -f "$RECORDS/summary.json" ] || die "records.tar has no records/index"
  fi

  # 3. Mathlib from its official cache only. The cache of the project is not used here.
  if [ -n "$(find "$PROJ/.lake/build" -mindepth 1 -print -quit 2>/dev/null)" ] && [ ! -f "$OUT/unpacked" ]; then
    die "$PROJ/.lake/build has files from an earlier, unfinished run; delete it and run prepare again"
  fi
  if [ ! -f "$PROJ/.lake/packages/mathlib/.lake/build/lib/lean/Mathlib.olean" ]; then
    log "get Mathlib from cache.mathlib.org"
    (cd "$PROJ" && PATH="$LEAN_HOME/bin:$PATH" LAKE_ARTIFACT_CACHE=false \
      MATHLIB_CACHE_GET_URL=https://cache.mathlib.org/mathlib4 lake exe mathlib/cache get)
  fi
  [ "$(sha "$PROJ/lake-manifest.json")" = "$DEPS_SHA" ] || die "lake-manifest.json changed"

  # 4. The cache: the pinned manifest, every archive, and a check of every entry name.
  [ -f "$DL/modules.jsonl.gz" ] || py manifest "$DL/modules.jsonl.gz" --src "$PROJ/src"
  py fetch "$DL/modules.jsonl.gz" "$DL/artifacts" --workers 48
  py check "$DL/modules.jsonl.gz" "$DL/artifacts"
  if [ ! -f "$DL/olean-manifest.sha256" ]; then
    curl -fsSL --retry 5 -o "$DL/olean-manifest.sha256" "$ILEAN_URL"
  fi
  [ "$(sha "$DL/olean-manifest.sha256")" = "$ILEAN_SHA" ] || die "olean-manifest.sha256 does not match its pin"

  # 5. Unpack in the sandbox. The job list comes from the archives that passed the check, read again now.
  if [ ! -f "$OUT/unpacked" ]; then
    mkdir -p "$PROJ/.lake/build"
    py jobs "$DL/modules.jsonl.gz" "$DL/artifacts" "$PROJ" "$DL/jobs.json"
    log "unpack with the leantar of the toolchain ($JOBS jobs), in the sandbox"
    WRITE=("$PROJ/.lake/build")
    sandbox leantar --jobs "$JOBS" -f -x -j - < "$DL/jobs.json"

    # 6. Check the unpacked tree before Lake or Lean reads it. Only a tree that passes gets the marker.
    py verify "$DL/modules.jsonl.gz" "$PROJ" --ilean-manifest "$DL/olean-manifest.sha256" | tee "$OUT/verify.txt"
    touch "$OUT/unpacked"
  fi

  # 7. The current trust report, so that the offline stages can add to it.
  mkdir -p "$OUT/content/records"
  curl -fsSL -o "$OUT/content/records/trust.json" https://raw.githubusercontent.com/tamnd/riemann/main/records/trust.json

  cat > "$OUT/artifacts.json" <<EOF
{
  "kind": "cached",
  "source": "https://rh-cache.taucetiproject.org (the Lake cache of leanprover-community/riemann-hypothesis)",
  "revision": "$REPO/tree/$REV",
  "pins": {
    "modules.jsonl.gz": "$(sha "$DL/modules.jsonl.gz")",
    "revision map": "fed2443b161bd8194d9507e5c9c0e555b865dbba6963a0d57172723a439e6941",
    "olean-manifest.sha256": "$ILEAN_SHA",
    "records.tar": "$RECORDS_SHA",
    "lake-manifest.json": "$DEPS_SHA"
  },
  "note": "The src of $REV without Challenge.lean is the src of riemann-lean 9110120. The .olean files are those of JackMcCarthy/riemann-hypothesis-build oleans.tar.zst with the package name set to tree. Each .ilean is byte-identical to that build. Each archive entry was checked before unpacking."
}
EOF
  log "prepare: done"
}

# Show that the sandbox holds, before any cache file is opened: run it while the VM is online.
stage_selftest() {
  python3 -I "$HERE/test_rhcache.py" -v
  mkdir -p "$PROJ/src" "$PROJ/.lake" "$DL"
  WRITE=("$OUT")
  sandbox python3 -I - "$PROJ" "$DL" "$LEAN_HOME" "$RH" "$OUT" "$HOME" <<'EOF'
import os, socket, sys
proj, dl, lean, rh, out, home = sys.argv[1:7]
fails = []
def expect_error(what, fn):
    try:
        fn()
    except OSError:
        print("ok:", what)
        return
    fails.append(what)
    print("FAIL:", what)
def write(path):
    with open(path, "w") as f:
        f.write("x")
expect_error("no TCP connection to 1.1.1.1:443", lambda: socket.create_connection(("1.1.1.1", 443), timeout=5))
expect_error("no DNS", lambda: socket.getaddrinfo("github.com", 443))
for d in (os.path.join(proj, "src"), dl, lean, os.path.dirname(rh), "/usr", "/etc"):
    expect_error(f"no write to {d}", lambda d=d: write(os.path.join(d, "sandbox-test")))
for p in (os.path.join(home, ".ssh"), "/etc/shadow", "/root", "/home"):
    expect_error(f"{p} is not visible", lambda p=p: os.stat(p))
caps = [l for l in open("/proc/self/status") if l.startswith("CapEff:")][0].split()[1]
print("ok: no capabilities" if int(caps, 16) == 0 else fails.append("capabilities") or "FAIL: capabilities " + caps)
write(os.path.join(out, "sandbox-test")); os.remove(os.path.join(out, "sandbox-test")); print("ok: the output directory is writable")
sys.exit(1 if fails else 0)
EOF
  log "selftest: the sandbox holds"
}

stage_extract() {
  [ -f "$OUT/unpacked" ] || die "run prepare first"
  check_source
  log "rh extract in the sandbox; Lean imports Main (all 175,187 modules)"
  WRITE=("$PROJ/.lake" "$OUT")
  sandbox /usr/bin/time -v "$RH" extract -lean "$PROJ" -index "$RECORDS" -out "$OUT/records" \
    -keep "$OUT/constants.jsonl" -artifacts "$OUT/artifacts.json" -timeout 24h 2>&1 | tee "$OUT/extract.log"
  grep -q "declarations with the exact graph" "$OUT/extract.log" || die "rh extract failed; see $OUT/extract.log"
  gzip -kf "$OUT/constants.jsonl"
  (cd "$OUT" && sha256sum constants.jsonl.gz records/extract.json records/summary.json > extract.sha256)
  log "extract: done; the records are in $OUT/records"
}

stage_trust() {
  [ -f "$OUT/unpacked" ] || die "run prepare first"
  check_source
  [ -f "$OUT/content/records/trust.json" ] || die "run prepare first: it gets the current trust report"
  WRITE=("$PROJ/.lake" "$OUT")
  sandbox /usr/bin/time -v "$RH" trust -lean "$PROJ" -content "$OUT/content" -artifacts "$OUT/artifacts.json" \
    -timeout 24h "$@" 2>&1 | tee "$OUT/$STAGE.log"
}

case "$STAGE" in
  selftest) stage_selftest ;;
  prepare) stage_prepare ;;
  extract) stage_extract ;;
  axioms) stage_trust ;;
  replay) stage_trust -lean4checker "$LEAN_HOME/bin/leanchecker" ;;
  *) die "unknown stage $STAGE" ;;
esac
