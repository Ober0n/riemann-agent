#!/usr/bin/env python3
"""Fetch, check and unpack-plan the pinned community Lake cache of the release.

The cache holds build files that other people made. This program treats each file as untrusted
data. It checks every pin, lists every entry of every archive before leantar sees it, and checks
the unpacked tree. It never decompresses, loads or runs anything from the cache.

Why the entry check is necessary: leantar 0.1.20 joins each entry name to its base directory with
no check. An absolute name or a name with ".." writes outside the base directory.

Subcommands (use -h on each for the arguments):
  manifest  download the pinned module manifest; check its SHA-256, header and rows
  fetch     download each artifact; check its SHA-256 against the manifest
  check     list each entry of each artifact; reject anything that is not expected
  jobs      check again, then write the job list for `leantar -f -x -j -`
  verify    check the unpacked .lake/build tree before any Lean or Lake program reads it

Needs Python 3.10 or later, and the standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import hashlib
import http.client
import json
import os
import re
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

# The pins. They come from leanprover-community/riemann-hypothesis at e50d098 (scripts/restore-cache.py
# and scripts/cache-baselines.json) and from the pinned Lean release.
HOST = "rh-cache.taucetiproject.org"
MANIFEST_PATH = "/bootstrap/e50d09817ad81469f737e8f637561e6ccf86c670/modules.jsonl.gz"
MANIFEST_SHA256 = "292caa386a620d935cfc736b56fcceaab9fe7073086693b62e899c1ee4725f04"
HEADER = {
    "schema": 1,
    "source_tree": "24df20b62d9976ccd20da819bc20b16f808f0fdd",
    "toolchain": "leanprover/lean4:v4.33.1",
    "map_sha256": "fed2443b161bd8194d9507e5c9c0e555b865dbba6963a0d57172723a439e6941",
    "modules": 175187,
}
ARTIFACT_PATH = "/artifacts/leanprover-community/riemann-hypothesis/{}.art"
LEAN_VERSION = b"4.33.1"
LEAN_GITHASH = b"819816b2e0a3bf405af45ae5c7af2491d8f5bee6"
USER_AGENT = "rh-cached-check/1"

MAX_MANIFEST = 64 << 20
MAX_ARTIFACT = 1 << 30
MAX_NAME = 255
MODULE = re.compile(r"[\w']+(?:\.[\w']+)*\Z")  # Unicode names, for example Divisors₂, occur in the release.
HEX16 = re.compile(r"[0-9a-f]{16}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

# The ltar format of leantar 0.1.20 (src/ltar.rs).
LTAR_V4 = b"LTR4"
ZSTD, LGZ, HASH_PLAIN, HASH_JSON, HASH_OUTPUT, LGZ_MODULE, HASH0_OUTPUT = range(7)
TRACE_KINDS = {ZSTD, HASH_PLAIN, HASH_JSON, HASH_OUTPUT, HASH0_OUTPUT}
OUTPUT_END = 0
OUTPUT_WITH_HASH = {1, 2, 3, 4, 5}  # olean part, bc, ir, ir.sig, ltar: each is followed by a u64
OUTPUT_FLAG = {6, 7}  # module no, module yes

# The olean header of Lean 4.33.1 (src/library/module.cpp): marker, version, flags, version string, githash, base address.
OLEAN_HEADER = struct.Struct("<5sBB33s40sQ")


class CacheError(Exception):
    """A check failed. The message names the file and the reason."""


class Row(NamedTuple):
    name: str  # the module, for example BuildAid.D1
    dep_hash: str  # the input hash that leantar writes into the .trace
    artifact: str  # the archive name in the manifest, <hex16>.ltar
    digest: str  # the SHA-256 of the archive

    @property
    def parts(self) -> list[str]:
        return self.name.split(".")

    @property
    def base(self) -> str:
        return self.parts[-1]

    @property
    def url_path(self) -> str:
        return ARTIFACT_PATH.format(self.artifact[: -len(".ltar")])


# --- The manifest -------------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_manifest(data: bytes) -> list[Row]:
    """Check the pinned manifest and return its rows."""
    if sha256_bytes(data) != MANIFEST_SHA256:
        raise CacheError("manifest: the SHA-256 is not the pinned one")
    lines = gzip.decompress(data).splitlines()
    if not lines or json.loads(lines[0]) != HEADER:
        raise CacheError("manifest: the header is not the pinned one")
    rows, seen = [], set()
    for n, line in enumerate(lines[1:], start=2):
        r = json.loads(line)
        if not (isinstance(r, list) and len(r) == 4 and all(isinstance(x, str) for x in r)):
            raise CacheError(f"manifest line {n}: not a row of four strings")
        row = Row(*r)
        if not MODULE.fullmatch(row.name) or row.name in seen:
            raise CacheError(f"manifest line {n}: bad or repeated module name {row.name!r}")
        if not HEX16.fullmatch(row.dep_hash) or not HEX64.fullmatch(row.digest):
            raise CacheError(f"manifest line {n}: bad hash for {row.name}")
        if not (row.artifact.endswith(".ltar") and HEX16.fullmatch(row.artifact[:-5])):
            raise CacheError(f"manifest line {n}: bad artifact name for {row.name}")
        seen.add(row.name)
        rows.append(row)
    if len(rows) != HEADER["modules"]:
        raise CacheError(f"manifest: {len(rows)} rows, not {HEADER['modules']}")
    return rows


def source_modules(src: Path) -> set[str]:
    out = set()
    for root, _dirs, files in os.walk(src):
        for f in files:
            if f.endswith(".lean"):
                rel = Path(root, f).relative_to(src).with_suffix("")
                out.add(".".join(rel.parts))
    return out


def check_against_source(rows: list[Row], src: Path, ignore: set[str]) -> None:
    want = source_modules(src) - ignore
    have = {r.name for r in rows}
    if want != have:
        raise CacheError(f"manifest: {len(want - have)} source modules have no row, {len(have - want)} rows have no source")


# --- HTTP ---------------------------------------------------------------------------------------

_local = threading.local()


def http_get(path: str, limit: int, retries: int = 5) -> bytes:
    """GET https://HOST/path on a kept-alive connection of this thread. The body may hold at most limit bytes."""
    delay = 1.0
    for attempt in range(retries):
        conn = getattr(_local, "conn", None)
        if conn is None:
            conn = _local.conn = http.client.HTTPSConnection(HOST, timeout=60)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            resp = conn.getresponse()
            length = resp.getheader("Content-Length")
            if length is not None and int(length) > limit:
                resp.close()
                raise CacheError(f"{path}: {length} bytes is more than the limit {limit}")
            body = resp.read(limit + 1)
            if resp.status != 200:
                raise OSError(f"HTTP {resp.status}")
            if len(body) > limit:
                raise CacheError(f"{path}: more than {limit} bytes")
            return body
        except CacheError:
            raise
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            _local.conn = None
            if attempt == retries - 1:
                raise CacheError(f"{path}: {e}") from e
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def fetch_one(row: Row, out: Path) -> bool:
    """Download one artifact unless a good copy exists. Return True when it downloaded."""
    dest = out / row.artifact
    if dest.is_file() and not dest.is_symlink() and sha256_file(dest) == row.digest:
        return False
    body = http_get(row.url_path, MAX_ARTIFACT)
    if sha256_bytes(body) != row.digest:
        raise CacheError(f"{row.name}: the artifact does not match its pinned SHA-256")
    tmp = out / f".{row.artifact}.{os.getpid()}.{threading.get_ident()}.part"
    tmp.write_bytes(body)
    os.replace(tmp, dest)
    return True


# --- The ltar format ----------------------------------------------------------------------------


class Entry(NamedTuple):
    index: int  # the base directory: 0 is lib/lean, 1 is ir
    name: str
    kind: int  # the compression byte
    extra: tuple[tuple[int, str], ...]
    size: int  # the bytes of the payload in the archive


class Ltar(NamedTuple):
    has_hash: bool
    trace: str
    trace_kind: int
    entries: list[Entry]
    comments: int


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise CacheError("the archive ends early")
        b = self.data[self.pos : self.pos + n]
        self.pos += n
        return b

    def u8(self) -> int:
        return self.take(1)[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def cstr(self) -> bytes:
        end = self.data.find(b"\0", self.pos)
        if end < 0:
            raise CacheError("a name has no end")
        b = self.data[self.pos : end]
        self.pos = end + 1
        return b

    def at_end(self) -> bool:
        return self.pos == len(self.data)


def safe_name(raw: bytes) -> str:
    """Return the entry name when it is one plain file name, else fail."""
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CacheError("an entry name is not UTF-8") from e
    if (
        not name
        or len(raw) > MAX_NAME
        or name in (".", "..")
        or any(c in name for c in "/\\:")
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
    ):
        raise CacheError(f"unsafe entry name {name!r}")
    return name


def _skip_payload(r: _Reader, kind: int) -> int:
    if kind in (ZSTD, LGZ, LGZ_MODULE):
        n = r.u64()
        if n > MAX_ARTIFACT:
            raise CacheError(f"a payload of {n} bytes")
        r.take(n)
        return n
    if kind in (HASH_PLAIN, HASH_JSON):
        r.take(8)
        return 8
    if kind in (HASH_OUTPUT, HASH0_OUTPUT):
        start = r.pos
        r.take(32 if kind == HASH_OUTPUT else 24)
        while True:
            tag = r.u8()
            if tag == OUTPUT_END:
                return r.pos - start
            if tag in OUTPUT_WITH_HASH:
                r.take(8)
            elif tag not in OUTPUT_FLAG:
                raise CacheError(f"unknown output tag {tag}")
    raise CacheError(f"unknown compression {kind}")


def parse_ltar(data: bytes) -> Ltar:
    """List the structure of a version 4 ltar archive without decompressing it."""
    r = _Reader(data)
    if r.take(4) != LTAR_V4:
        raise CacheError("not an LTR4 archive")
    flag = r.u8()
    if flag not in (0, 1):
        raise CacheError(f"bad hash flag {flag}")
    if flag == 1:
        r.u64()
    trace = safe_name(r.cstr())
    trace_kind = r.u8()
    if trace_kind not in TRACE_KINDS:
        raise CacheError(f"the trace has compression {trace_kind}")
    _skip_payload(r, trace_kind)
    entries, comments = [], 0
    while not r.at_end():
        index = r.u8()
        raw = r.cstr()
        if not raw:  # a comment
            r.cstr()
            comments += 1
            continue
        name = safe_name(raw)
        kind = r.u8()
        extra = tuple((r.u8(), safe_name(r.cstr())) for _ in range(2 if kind == LGZ_MODULE else 0))
        size = _skip_payload(r, kind)
        entries.append(Entry(index, name, kind, extra, size))
    return Ltar(flag == 1, trace, trace_kind, entries, comments)


def check_ltar(data: bytes, base: str) -> Ltar:
    """Parse the archive of the module with the last name component base, and check each entry."""
    lt = parse_ltar(data)
    if lt.trace != base + ".trace":
        raise CacheError(f"the trace is {lt.trace!r}, not {base}.trace")
    if lt.comments:
        raise CacheError("the archive has comments")
    allowed = {(0, base + ".olean"): {ZSTD, LGZ}, (0, base + ".ilean"): {ZSTD}, (1, base + ".c"): {ZSTD}}
    seen = set()
    for e in lt.entries:
        key = (e.index, e.name)
        if key not in allowed:
            raise CacheError(f"unexpected entry {e.name!r} in base directory {e.index}")
        if key in seen:
            raise CacheError(f"repeated entry {e.name!r}")
        if e.kind not in allowed[key] or e.extra:
            raise CacheError(f"entry {e.name!r} has compression {e.kind}")
        seen.add(key)
    if seen != set(allowed):
        missing = sorted(n for _, n in set(allowed) - seen)
        raise CacheError(f"missing entries {missing}")
    return lt


# --- The unpack plan ----------------------------------------------------------------------------


def no_links_below(root: Path, path: Path) -> None:
    """Fail when a component of path below root exists and is a link."""
    rel = path.relative_to(root)
    cur = root
    for part in rel.parts:
        cur = cur / part
        try:
            st = cur.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise CacheError(f"a link in the output path: {cur}")


def make_jobs(rows: list[Row], cache: Path, project: Path) -> list[dict]:
    project = project.resolve()
    build = project / ".lake" / "build"
    if build.is_symlink() or (project / ".lake").is_symlink():
        raise CacheError(".lake or .lake/build is a link")
    jobs = []
    for row in rows:
        path = cache / row.artifact
        if path.is_symlink() or not path.is_file():
            raise CacheError(f"{row.name}: no artifact {path}")
        data = path.read_bytes()
        if sha256_bytes(data) != row.digest:
            raise CacheError(f"{row.name}: the artifact does not match its pinned SHA-256")
        lt = check_ltar(data, row.base)
        if not lt.has_hash and not HEX16.fullmatch(row.dep_hash):
            raise CacheError(f"{row.name}: no input hash")
        lib = build / "lib" / "lean" / Path(*row.parts[:-1])
        ir = build / "ir" / Path(*row.parts[:-1])
        for d in (lib, ir):
            no_links_below(project, d)
        jobs.append({"file": str(path.resolve()), "hash": row.dep_hash, "base": [str(lib), str(ir)]})
    return jobs


# --- The unpacked tree --------------------------------------------------------------------------


def expected_files(rows: list[Row]) -> dict[str, str]:
    """Map each expected path below .lake/build (with /) to its kind."""
    out = {}
    for row in rows:
        stem = "/".join(row.parts)
        for ext in ("olean", "ilean", "trace"):
            out[f"lib/lean/{stem}.{ext}"] = ext
        out[f"ir/{stem}.c"] = "c"
    return out


def check_olean_header(head: bytes) -> None:
    if len(head) < OLEAN_HEADER.size:
        raise CacheError("short olean")
    marker, version, flags, lean_version, githash, _base = OLEAN_HEADER.unpack(head[: OLEAN_HEADER.size])
    if marker != b"olean" or version != 2 or flags != 1:
        raise CacheError(f"olean header: marker {marker!r}, version {version}, flags {flags}")
    if lean_version.rstrip(b"\0") != LEAN_VERSION or githash != LEAN_GITHASH:
        raise CacheError(f"olean header: Lean {lean_version.rstrip(bytes(1))!r} at {githash!r}")


def check_content(kind: str, path: Path) -> None:
    with path.open("rb") as f:
        head = f.read(OLEAN_HEADER.size)
    if kind == "olean":
        check_olean_header(head)
    elif kind == "c":
        if not head.startswith(b"// Lean compiler output"):
            raise CacheError("a .c file without the Lean compiler header")
    elif not head.lstrip().startswith(b"{"):  # .ilean and .trace are JSON objects
        raise CacheError(f"a .{kind} file that is not JSON")


def load_ilean_manifest(path: Path) -> dict[str, str]:
    """Read lines '<sha256>  .lake/build/lib/lean/<path>.ilean' (sha256sum format)."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        name = name.lstrip("*").removeprefix("./")
        prefix = ".lake/build/"
        if name.endswith(".ilean") and name.startswith(prefix):
            out[name[len(prefix):]] = digest
    return out


def verify_tree(project: Path, rows: list[Row], ilean: dict[str, str] | None = None) -> dict[str, int]:
    """Check the unpacked tree. Return counts by kind."""
    build = project / ".lake" / "build"
    want = expected_files(rows)
    found, errors = {}, []
    for root, dirs, files in os.walk(build, followlinks=False):
        for d in dirs:
            p = Path(root, d)
            if p.is_symlink():
                errors.append(f"a link: {p}")
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for f in files:
            p = Path(root, f)
            rel = p.relative_to(build).as_posix()
            st = p.lstat()
            if not stat.S_ISREG(st.st_mode):
                errors.append(f"not a regular file: {rel}")
            elif st.st_nlink != 1:
                errors.append(f"a hard link: {rel}")
            elif st.st_mode & 0o7111:
                errors.append(f"an executable or set-id mode {oct(st.st_mode & 0o7777)}: {rel}")
            elif rel not in want:
                errors.append(f"an unexpected file: {rel}")
            else:
                found[rel] = want[rel]
        if len(errors) > 50:
            break
    missing = sorted(set(want) - set(found))
    if missing:
        errors.append(f"{len(missing)} missing files, for example {missing[:3]}")
    counts: dict[str, int] = {}
    for rel, kind in sorted(found.items()):
        if len(errors) > 50:
            break
        try:
            check_content(kind, build / rel)
            if kind == "ilean" and ilean is not None:
                if ilean.get(rel) is None:
                    raise CacheError("no row in the .ilean manifest")
                if sha256_file(build / rel) != ilean[rel]:
                    raise CacheError("the .ilean differs from the other published build")
        except CacheError as e:
            errors.append(f"{rel}: {e}")
            continue
        counts[kind] = counts.get(kind, 0) + 1
    if errors:
        raise CacheError("the unpacked tree failed:\n  " + "\n  ".join(errors[:50]))
    return counts


# --- The command line ---------------------------------------------------------------------------


def read_rows(path: Path) -> list[Row]:
    return parse_manifest(path.read_bytes())


def cmd_manifest(a: argparse.Namespace) -> None:
    data = http_get(MANIFEST_PATH, MAX_MANIFEST)
    rows = parse_manifest(data)
    if a.src:
        check_against_source(rows, Path(a.src), set(a.ignore))
    a.out.write_bytes(data)
    print(f"manifest: {len(rows)} modules; SHA-256, header and rows match the pins")


def cmd_fetch(a: argparse.Namespace) -> None:
    rows = read_rows(a.manifest)
    a.dir.mkdir(parents=True, exist_ok=True)
    done = new = 0
    failed = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(fetch_one, row, a.dir): row for row in rows}
        for fut in cf.as_completed(futs):
            try:
                new += fut.result()
            except CacheError as e:
                failed.append(str(e))
            done += 1
            if done % 10000 == 0:
                print(f"fetch: {done}/{len(rows)}", flush=True)
    if failed:
        raise CacheError(f"{len(failed)} artifacts failed, for example: {failed[:3]}")
    print(f"fetch: {len(rows)} artifacts match their pinned SHA-256 ({new} new)")


def cmd_check(a: argparse.Namespace) -> None:
    rows = read_rows(a.manifest)
    bad = []
    for row in rows:
        try:
            data = (a.dir / row.artifact).read_bytes()
            if sha256_bytes(data) != row.digest:
                raise CacheError("the SHA-256 differs")
            check_ltar(data, row.base)
        except (CacheError, OSError) as e:
            bad.append(f"{row.name}: {e}")
    if bad:
        raise CacheError(f"{len(bad)} artifacts failed the entry check:\n  " + "\n  ".join(bad[:20]))
    print(f"check: each entry of {len(rows)} artifacts is an expected file name for its module")


def cmd_jobs(a: argparse.Namespace) -> None:
    jobs = make_jobs(read_rows(a.manifest), a.dir, a.project)
    a.out.write_text(json.dumps(jobs), encoding="utf-8")
    print(f"jobs: {len(jobs)} checked artifacts")


def cmd_verify(a: argparse.Namespace) -> None:
    ilean = load_ilean_manifest(a.ilean_manifest) if a.ilean_manifest else None
    counts = verify_tree(a.project, read_rows(a.manifest), ilean)
    print(f"verify: {counts}; no links, no executable files, no unexpected files")
    if ilean is not None:
        print("verify: each .ilean is byte-identical to the other published build")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("manifest")
    s.add_argument("out", type=Path)
    s.add_argument("--src", help="the src directory; the manifest must name exactly its modules")
    s.add_argument("--ignore", nargs="*", default=["Challenge"], help="source modules that are not in the cache")
    s.set_defaults(func=cmd_manifest)
    s = sub.add_parser("fetch")
    s.add_argument("manifest", type=Path)
    s.add_argument("dir", type=Path)
    s.add_argument("--workers", type=int, default=48)
    s.set_defaults(func=cmd_fetch)
    s = sub.add_parser("check")
    s.add_argument("manifest", type=Path)
    s.add_argument("dir", type=Path)
    s.set_defaults(func=cmd_check)
    s = sub.add_parser("jobs")
    s.add_argument("manifest", type=Path)
    s.add_argument("dir", type=Path)
    s.add_argument("project", type=Path)
    s.add_argument("out", type=Path)
    s.set_defaults(func=cmd_jobs)
    s = sub.add_parser("verify")
    s.add_argument("manifest", type=Path)
    s.add_argument("project", type=Path)
    s.add_argument("--ilean-manifest", type=Path, help="the olean-manifest.sha256 of the other published build")
    s.set_defaults(func=cmd_verify)
    a = p.parse_args(argv)
    try:
        a.func(a)
    except CacheError as e:
        print(f"rhcache: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
