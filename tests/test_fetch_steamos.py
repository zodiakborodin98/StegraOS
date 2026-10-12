# SPDX-License-Identifier: GPL-2.0-only
"""Portable tests: no network, mounts, root privileges or Valve downloads."""
import hashlib
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import zstandard

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch-steamos.py"
SPEC = importlib.util.spec_from_file_location("fetch_steamos", SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fetch
SPEC.loader.exec_module(fetch)


def make_index(chunks, sha512=True):
    algorithm = "sha512_256" if sha512 else "sha256"
    flags = fetch.CA_FORMAT_SHA512_256 if sha512 else 0
    data = struct.pack("<6Q", 48, fetch.CA_FORMAT_INDEX, flags, 1, 16, 4096)
    data += struct.pack("<2Q", fetch.UINT64_MAX, fetch.CA_FORMAT_TABLE)
    offset = 0
    for raw in chunks:
        offset += len(raw)
        data += struct.pack("<Q", offset) + hashlib.new(algorithm, raw).digest()
    return data + struct.pack("<5Q", 0, 0, 48, len(data) - 48 + 40, fetch.CA_FORMAT_TABLE_TAIL_MARKER)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = self.root / "root.caibx"
        self.output = self.root / "root.img"
        self.cache = self.root / "cache"

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, chunks, sha512=True):
        self.index.write_bytes(make_index(chunks, sha512))
        algorithm = "sha512_256" if sha512 else "sha256"
        for raw in chunks:
            digest = hashlib.new(algorithm, raw).hexdigest()
            target = self.cache / digest[:4] / (digest + ".cacnk")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zstandard.ZstdCompressor().compress(raw))
        return hashlib.sha256(b"".join(chunks)).hexdigest()

    def test_cache_reconstruction_duplicate_and_zero_chunks(self):
        chunks = [b"first", b"\0" * 128, b"first", b"last"]
        expected = self.prepare(chunks)
        with patch.object(fetch, "urlopen", side_effect=AssertionError("network forbidden")):
            fetch.assemble(self.index, self.output, [], 4, expected, self.cache)
            fetch.assemble(self.index, self.output, [], 4, expected, self.cache)
        self.assertEqual(self.output.read_bytes(), b"".join(chunks))
        self.assertEqual(list(self.root.glob("*.partial")), [])

    def test_sha256_chunk_mode(self):
        expected = self.prepare([b"legacy digest"], sha512=False)
        fetch.assemble(self.index, self.output, [], 2, expected, self.cache)
        self.assertEqual(fetch.sha256_file(self.output), expected)

    def test_malformed_index(self):
        valid = make_index([b"example"])
        mutations = [b"", valid[:-1], valid + b"x"]
        # Header length, magic, flags, chunk maximum, table sentinel/magic,
        # descending end-offset, every footer field.
        for position, value in [(0, 47), (8, 0), (16, 1 << 40), (40, 0), (48, 0),
                                (56, 0), (64, 0), (104, 1), (112, 1), (120, 49),
                                (128, 0), (136, 0)]:
            data = bytearray(valid)
            struct.pack_into("<Q", data, position, value)
            mutations.append(bytes(data))
        for bad in mutations:
            with self.subTest(length=len(bad)):
                self.index.write_bytes(bad)
                with self.assertRaises(ValueError):
                    fetch.parse_caibx(self.index)

    def test_inconsistent_duplicate_length(self):
        data = bytearray(make_index([b"aa", b"aa"]))
        struct.pack_into("<Q", data, 104, 5)
        self.index.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            fetch.parse_caibx(self.index)

    def test_bad_final_hash_never_publishes(self):
        self.prepare([b"hello"])
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            fetch.assemble(self.index, self.output, [], 1, "0" * 64, self.cache)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob("*.partial")), [])
        self.assertTrue(list(self.cache.rglob("*.cacnk")))

    def test_existing_output_is_not_overwritten(self):
        expected = self.prepare([b"hello"])
        self.output.write_bytes(b"valuable")
        with self.assertRaises(FileExistsError):
            fetch.assemble(self.index, self.output, [], 1, expected, self.cache)
        self.assertEqual(self.output.read_bytes(), b"valuable")

    def test_corrupt_cache_is_repaired_and_reused(self):
        expected = self.prepare([b"hello"])
        chunk = next(self.cache.rglob("*.cacnk"))
        chunk.write_bytes(b"corrupt")
        blob = zstandard.ZstdCompressor().compress(b"hello")
        with patch.object(fetch, "download_chunk", return_value=blob) as downloader:
            fetch.assemble(self.index, self.output, ["https://example.invalid/store"], 1, expected, self.cache)
            downloader.assert_called_once()
        self.assertEqual(chunk.read_bytes(), blob)

    def test_decompression_bomb_wrong_hash_truncation_and_trailing_bytes(self):
        raw = b"hello"
        digest = hashlib.new("sha512_256", raw).digest()
        good = zstandard.ZstdCompressor().compress(raw)
        bad_blobs = [zstandard.ZstdCompressor().compress(b"x" * 1000000),
                     good[:-1], good + b"trailing", zstandard.ZstdCompressor().compress(b"wrong")]
        for blob in bad_blobs:
            with self.subTest(blob_size=len(blob)), self.assertRaises(ValueError):
                fetch.validate_chunk(blob, len(raw), digest, "sha512_256")

    def test_frame_without_declared_content_size(self):
        raw = b"valid streaming frame"
        blob = zstandard.ZstdCompressor(write_content_size=False).compress(raw)
        digest = hashlib.new("sha512_256", raw).digest()
        self.assertEqual(fetch.validate_chunk(blob, len(raw), digest, "sha512_256"), raw)

    def test_valve_sized_frame_window(self):
        # The real first Deckard chunk has a 262144-byte zstd window and output.
        # Synthetic bytes retain that regression without redistributing an image.
        raw = bytes(range(256)) * 1024
        blob = zstandard.ZstdCompressor().compress(raw)
        self.assertEqual(zstandard.get_frame_parameters(blob).window_size, 262144)
        digest = hashlib.new("sha512_256", raw).digest()
        self.assertEqual(fetch.validate_chunk(blob, len(raw), digest, "sha512_256"), raw)

    def test_missing_cache_fails_without_network(self):
        self.index.write_bytes(make_index([b"absent"]))
        with patch.object(fetch, "urlopen", side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(RuntimeError, "cache chunk"):
                fetch.assemble(self.index, self.output, [], 1, "0" * 64, self.cache)

    def test_empty_index(self):
        self.index.write_bytes(make_index([]))
        fetch.assemble(self.index, self.output, [], 1, hashlib.sha256(b"").hexdigest(), self.cache)
        self.assertEqual(self.output.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
