"""Tests of rhcache.py with hostile inputs. Run: python3 -m unittest -v test_rhcache.py

The archives here are made by hand in the ltar format of leantar 0.1.20. The parser does not
decompress, so the payloads are arbitrary bytes. Set RH_TEST_ARTIFACT to the path of the real
artifact of BuildAid.D1 (SHA-256 8ebf2188...) to also test a real archive.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rhcache as rc  # noqa: E402


def cstr(s: str | bytes) -> bytes:
    return (s.encode() if isinstance(s, str) else s) + b"\0"


def payload(kind: int, body: bytes = b"x" * 16) -> bytes:
    if kind in (rc.ZSTD, rc.LGZ, rc.LGZ_MODULE):
        return struct.pack("<Q", len(body)) + body
    if kind in (rc.HASH_PLAIN, rc.HASH_JSON):
        return struct.pack("<Q", 7)
    if kind == rc.HASH0_OUTPUT:
        return b"\1" * 24 + bytes([rc.OUTPUT_END])
    if kind == rc.HASH_OUTPUT:
        return b"\1" * 32 + bytes([rc.OUTPUT_END])
    return b""


def ltar(base="D1", trace=None, trace_kind=rc.HASH0_OUTPUT, entries=None, magic=b"LTR4", flag=0, tail=b""):
    """Make an archive. entries is a list of (index, name, kind) or raw bytes."""
    if entries is None:
        entries = [(0, f"{base}.olean", rc.LGZ), (0, f"{base}.ilean", rc.ZSTD), (1, f"{base}.c", rc.ZSTD)]
    out = magic + bytes([flag]) + (struct.pack("<Q", 5) if flag == 1 else b"")
    out += cstr(trace if trace is not None else f"{base}.trace") + bytes([trace_kind]) + payload(trace_kind)
    for e in entries:
        if isinstance(e, bytes):
            out += e
            continue
        index, name, kind = e
        out += bytes([index]) + cstr(name) + bytes([kind])
        if kind == rc.LGZ_MODULE:
            out += bytes([0]) + cstr(f"{base}.olean.server") + bytes([0]) + cstr(f"{base}.olean.private")
        out += payload(kind)
    return out + tail


class TestLtar(unittest.TestCase):
    def ok(self, data, base="D1"):
        return rc.check_ltar(data, base)

    def bad(self, data, base="D1", msg=None):
        with self.assertRaises(rc.CacheError) as cm:
            rc.check_ltar(data, base)
        if msg:
            self.assertIn(msg, str(cm.exception))

    def test_good(self):
        lt = self.ok(ltar())
        self.assertEqual([e.name for e in lt.entries], ["D1.olean", "D1.ilean", "D1.c"])
        self.ok(ltar(flag=1))
        self.ok(ltar(entries=[(0, "D1.olean", rc.ZSTD), (0, "D1.ilean", rc.ZSTD), (1, "D1.c", rc.ZSTD)]))
        self.ok(ltar(base="Divisors₂"), base="Divisors₂")

    def test_path_traversal(self):
        for name in ["../D1.olean", "../../etc/passwd", "/etc/passwd", "..", ".", "a/D1.olean",
                     "..\\D1.olean", "C:D1.olean", "C:\\Windows\\x", "D1.olean\n", "D1\x7f.c"]:
            with self.subTest(name=name):
                self.bad(ltar(entries=[(0, name, rc.ZSTD), (0, "D1.ilean", rc.ZSTD), (1, "D1.c", rc.ZSTD)]))
        self.bad(ltar(trace="../D1.trace"), msg="unsafe")
        self.bad(ltar(trace="/tmp/D1.trace"), msg="unsafe")

    def test_unexpected_entries(self):
        good = [(0, "D1.olean", rc.LGZ), (0, "D1.ilean", rc.ZSTD), (1, "D1.c", rc.ZSTD)]
        cases = {
            "extra library": good + [(0, "libevil.so", rc.ZSTD)],
            "extra script": good + [(1, "run.sh", rc.ZSTD)],
            "other module": [(0, "D2.olean", rc.LGZ), (0, "D1.ilean", rc.ZSTD), (1, "D1.c", rc.ZSTD)],
            "server part": good + [(0, "D1.olean.server", rc.ZSTD)],
            "repeated": good + [(0, "D1.olean", rc.LGZ)],
            "missing c": good[:2],
            "c in lib": [(0, "D1.olean", rc.LGZ), (0, "D1.ilean", rc.ZSTD), (0, "D1.c", rc.ZSTD)],
            "third base": good[:2] + [(2, "D1.c", rc.ZSTD)],
            "module part": [(0, "D1.olean", rc.LGZ_MODULE), (0, "D1.ilean", rc.ZSTD), (1, "D1.c", rc.ZSTD)],
            "hash kind": [(0, "D1.olean", rc.HASH_PLAIN), (0, "D1.ilean", rc.ZSTD), (1, "D1.c", rc.ZSTD)],
        }
        for what, entries in cases.items():
            with self.subTest(what=what):
                self.bad(ltar(entries=entries))
        self.bad(ltar(base="D2"), base="D1", msg="trace")

    def test_structure(self):
        good = ltar()
        self.bad(ltar(magic=b"LTR3"), msg="LTR4")
        self.bad(ltar(magic=b"LTAR"))
        self.bad(ltar(flag=2), msg="flag")
        self.bad(ltar(trace_kind=rc.LGZ), msg="compression")
        self.bad(ltar(trace_kind=9))
        self.bad(good[:-3], msg="ends early")
        self.bad(good + b"\0", msg="no end")
        self.bad(good + b"\0" + cstr("") + b"comment without end")
        self.bad(good + b"\0" + cstr("") + cstr("a comment"), msg="comments")
        long_len = ltar(entries=[b"\0" + cstr("D1.olean") + bytes([rc.LGZ]) + struct.pack("<Q", 1 << 40)])
        self.bad(long_len)
        self.bad(ltar(entries=[b"\0" + cstr(b"\xff\xfe.olean") + bytes([rc.ZSTD]) + payload(rc.ZSTD)]), msg="UTF-8")
        self.bad(ltar(entries=[(0, "D1.olean", 9)]), msg="compression")
        self.bad(ltar(entries=[b"\0" + cstr("x" * 300) + bytes([rc.ZSTD]) + payload(rc.ZSTD)]))

    def test_output_descriptor(self):
        tagged = b"LTR4\0" + cstr("D1.trace") + bytes([rc.HASH0_OUTPUT]) + b"\1" * 24 + bytes([1]) + b"\2" * 8 + bytes([7, 0])
        rest = ltar()[len(ltar(entries=[])):]
        self.ok(tagged + rest)
        badtag = b"LTR4\0" + cstr("D1.trace") + bytes([rc.HASH0_OUTPUT]) + b"\1" * 24 + bytes([9, 0])
        self.bad(badtag + rest, msg="tag")

    @unittest.skipUnless(os.environ.get("RH_TEST_ARTIFACT"), "set RH_TEST_ARTIFACT to the real artifact of BuildAid.D1")
    def test_real_artifact(self):
        data = Path(os.environ["RH_TEST_ARTIFACT"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), "8ebf21882817f74cf91995bf7ae093a94d0a981acb91c4f9ddc304d60760f0e9")
        lt = self.ok(data)
        self.assertFalse(lt.has_hash)
        self.assertEqual([(e.index, e.name) for e in lt.entries], [(0, "D1.olean"), (0, "D1.ilean"), (1, "D1.c")])


def olean(githash=rc.LEAN_GITHASH, version=2, flags=1, marker=b"olean", lean=rc.LEAN_VERSION):
    return rc.OLEAN_HEADER.pack(marker, version, flags, lean, githash, 0x7F0000) + b"\0" * 64


class Manifest:
    """A manifest with its own pins, put in place of the real ones for the test."""

    def __init__(self, rows, header=None):
        header = header or dict(rc.HEADER, modules=len(rows))
        lines = [json.dumps(header)] + [json.dumps(r) for r in rows]
        self.data = gzip.compress(("\n".join(lines) + "\n").encode())
        self.header = header

    def __enter__(self):
        self.saved = rc.MANIFEST_SHA256, rc.HEADER
        rc.MANIFEST_SHA256, rc.HEADER = hashlib.sha256(self.data).hexdigest(), self.header
        return self

    def __exit__(self, *exc):
        rc.MANIFEST_SHA256, rc.HEADER = self.saved


ROW = ["BuildAid.D1", "f31a6064cf4d5da1", "355b7e26dd974a28.ltar", "8ebf21882817f74cf91995bf7ae093a94d0a981acb91c4f9ddc304d60760f0e9"]


class TestManifest(unittest.TestCase):
    def test_real_pin_rejects_other_data(self):
        with self.assertRaises(rc.CacheError):
            rc.parse_manifest(gzip.compress(b"{}\n"))

    def test_rows(self):
        with Manifest([ROW]) as m:
            rows = rc.parse_manifest(m.data)
            self.assertEqual(rows[0].url_path, "/artifacts/leanprover-community/riemann-hypothesis/355b7e26dd974a28.art")
        bad_rows = {
            "dots": ["A..B", *ROW[1:]],
            "slash": ["A/B", *ROW[1:]],
            "parent": ["..", *ROW[1:]],
            "space": ["A B", *ROW[1:]],
            "hash": [ROW[0], "XYZ", *ROW[2:]],
            "artifact": [*ROW[:2], "../../x.ltar", ROW[3]],
            "digest": [*ROW[:3], "0" * 63],
            "short": ROW[:3],
        }
        for what, row in bad_rows.items():
            with self.subTest(what=what), Manifest([row]) as m, self.assertRaises(rc.CacheError):
                rc.parse_manifest(m.data)
        with Manifest([ROW, ROW]) as m, self.assertRaises(rc.CacheError):
            rc.parse_manifest(m.data)
        with Manifest([ROW], header=dict(rc.HEADER, modules=2)) as m, self.assertRaises(rc.CacheError):
            rc.parse_manifest(m.data)
        with Manifest([ROW], header=dict(rc.HEADER, source_tree="0" * 40, modules=1)) as m:
            rc.HEADER = dict(rc.HEADER, source_tree="1" * 40)
            with self.assertRaises(rc.CacheError):
                rc.parse_manifest(m.data)


class TestTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self.tmp.name)
        self.rows = [rc.Row(*ROW), rc.Row("Main", "0" * 16, "0" * 16 + ".ltar", "0" * 64)]
        b = self.proj / ".lake" / "build"
        for row in self.rows:
            stem = Path(*row.parts)
            lib = b / "lib" / "lean" / stem
            lib.parent.mkdir(parents=True, exist_ok=True)
            lib.with_suffix(".olean").write_bytes(olean())
            lib.with_suffix(".ilean").write_bytes(b'{"version": 2}')
            lib.with_suffix(".trace").write_bytes(b'{"depHash": "0"}')
            c = b / "ir" / stem.with_suffix(".c")
            c.parent.mkdir(parents=True, exist_ok=True)
            c.write_bytes(b"// Lean compiler output\n")
        self.build = b

    def tearDown(self):
        self.tmp.cleanup()

    def bad(self, msg):
        with self.assertRaises(rc.CacheError) as cm:
            rc.verify_tree(self.proj, self.rows)
        self.assertIn(msg, str(cm.exception))

    def test_good(self):
        self.assertEqual(rc.verify_tree(self.proj, self.rows), {"c": 2, "ilean": 2, "olean": 2, "trace": 2})

    def test_unexpected_file(self):
        (self.build / "lib" / "lean" / "libevil.so").write_bytes(b"\x7fELF")
        self.bad("unexpected file")

    def test_missing_file(self):
        (self.build / "ir" / "Main.c").unlink()
        self.bad("missing")

    def test_olean_header(self):
        p = self.build / "lib" / "lean" / "Main.olean"
        for data, what in [(olean(githash=b"0" * 40), "githash"), (olean(version=3), "version 3"),
                           (olean(flags=0), "flags"), (olean(lean=b"4.36.0"), "4.36.0"),
                           (b"\x7fELF" + b"\0" * 100, "marker"), (b"olean", "short")]:
            with self.subTest(what=what):
                p.write_bytes(data)
                self.bad("Main.olean")

    def test_executable_content(self):
        (self.build / "ir" / "Main.c").write_bytes(b"\x7fELF\2\1\1")
        self.bad("Lean compiler header")
        (self.build / "ir" / "Main.c").write_bytes(b"// Lean compiler output\n")
        (self.build / "lib" / "lean" / "Main.ilean").write_bytes(b"#!/bin/sh\nrm -rf ~\n")
        self.bad("not JSON")

    def test_hard_link(self):
        a = self.build / "lib" / "lean" / "Main.trace"
        a.unlink()
        os.link(self.build / "lib" / "lean" / "Main.ilean", a)
        self.bad("hard link")

    @unittest.skipIf(os.name == "nt", "Windows has no execute bit")
    def test_mode(self):
        p = self.build / "ir" / "Main.c"
        p.chmod(0o755)
        self.bad("executable")
        p.chmod(0o4644)
        self.bad("set-id")

    def test_symlinks(self):
        target = self.proj / "outside.txt"
        target.write_text("x")
        link = self.build / "lib" / "lean" / "Evil.olean"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("this system does not allow links")
        self.bad("not a regular file")
        link.unlink()
        dlink = self.build / "lib" / "lean" / "Linked"
        dlink.symlink_to(self.proj, target_is_directory=True)
        self.bad("a link")

    def test_ilean_cross_check(self):
        good = hashlib.sha256(b'{"version": 2}').hexdigest()
        man = {"lib/lean/BuildAid/D1.ilean": good, "lib/lean/Main.ilean": good}
        self.assertEqual(rc.verify_tree(self.proj, self.rows, man)["ilean"], 2)
        man["lib/lean/Main.ilean"] = "0" * 64
        with self.assertRaises(rc.CacheError) as cm:
            rc.verify_tree(self.proj, self.rows, man)
        self.assertIn("other published build", str(cm.exception))

    def test_ilean_manifest_format(self):
        f = self.proj / "m.sha256"
        f.write_text("ab  .lake/build/lib/lean/A/B.ilean\ncd *./.lake/build/lib/lean/C.ilean\nef  .lake/build/lib/lean/A/B.olean\n")
        self.assertEqual(rc.load_ilean_manifest(f), {"lib/lean/A/B.ilean": "ab", "lib/lean/C.ilean": "cd"})


class TestJobs(unittest.TestCase):
    def test_jobs_and_links(self):
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            cache, proj = t / "cache", t / "proj"
            cache.mkdir()
            proj.mkdir()
            data = ltar()
            row = rc.Row("BuildAid.D1", "f31a6064cf4d5da1", "1111111111111111.ltar", hashlib.sha256(data).hexdigest())
            (cache / row.artifact).write_bytes(data)
            jobs = rc.make_jobs([row], cache, proj)
            self.assertEqual(jobs[0]["hash"], "f31a6064cf4d5da1")
            self.assertTrue(jobs[0]["base"][0].endswith(os.path.join("lib", "lean", "BuildAid")))
            self.assertTrue(jobs[0]["base"][1].endswith(os.path.join("ir", "BuildAid")))
            # A changed artifact fails, even after an earlier check.
            (cache / row.artifact).write_bytes(ltar(entries=[(0, "../D1.olean", rc.LGZ)]))
            with self.assertRaises(rc.CacheError):
                rc.make_jobs([row], cache, proj)
            (cache / row.artifact).write_bytes(data)
            # A link in the output path fails.
            outside = t / "outside"
            outside.mkdir()
            (proj / ".lake").mkdir()
            try:
                (proj / ".lake" / "build").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("this system does not allow links")
            with self.assertRaises(rc.CacheError):
                rc.make_jobs([row], cache, proj)


if __name__ == "__main__":
    unittest.main()
