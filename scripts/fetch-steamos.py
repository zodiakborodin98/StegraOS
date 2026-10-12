#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Adapted from hashtagbasit/SteamOS-ARM-Handhelds, scripts/extract_rootfs.py.
# Upstream project scripts are GNU GPL version 2; this adaptation preserves it.
# https://github.com/hashtagbasit/SteamOS-ARM-Handhelds
"""Reconstruct a manifest-pinned casync image, without mounting or executing it.

Only zstd-compressed casync chunks are supported. Downloads are resumable through
the compressed chunk cache: each cached chunk is verified again on every run.
No release or download store is selected implicitly. Requires `zstandard`.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import re
import ssl
import struct
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import zstandard

CA_FORMAT_INDEX = 0x96824D9C7B129FF9
CA_FORMAT_TABLE = 0xE75B9E112F17417D
CA_FORMAT_TABLE_TAIL_MARKER = 0x4B4F050E5549ECD1
CA_FORMAT_SHA512_256 = 0x2000000000000000
KNOWN_FLAGS = 0xF0000001FFFFFFFF
INDEX_HEADER_LEN, TABLE_HEADER_LEN, TABLE_ENTRY_LEN = 48, 16, 40
UINT64_MAX = (1 << 64) - 1
# Bound resource use even for an untrusted index. Valve's chunks max out at 256 KiB.
MAX_CHUNK_SIZE = 64 * 1024 * 1024
MAX_INDEX_SIZE = 256 * 1024 * 1024
# Loading the Windows certificate store for each small chunk is expensive.
# SSLContext is safe to share once configured and continues to verify HTTPS.
HTTPS_CONTEXT = ssl.create_default_context()


@dataclass(frozen=True)
class Index:
    image_size: int
    chunks: tuple[tuple[int, int, bytes], ...]
    digest_name: str


def parse_caibx(path: str | Path) -> Index:
    with open(path, "rb") as stream:
        data = stream.read(MAX_INDEX_SIZE + 1)
    if len(data) > MAX_INDEX_SIZE:
        raise ValueError("index exceeds the 256 MiB safety limit")
    if len(data) < INDEX_HEADER_LEN + TABLE_HEADER_LEN + TABLE_ENTRY_LEN:
        raise ValueError("truncated casync index")
    size, magic, flags, minimum, average, maximum = struct.unpack_from("<6Q", data)
    if size != INDEX_HEADER_LEN or magic != CA_FORMAT_INDEX:
        raise ValueError("invalid casync index header")
    if flags & ~KNOWN_FLAGS:
        raise ValueError(f"unsupported index feature flags: {flags:#x}")
    if not 0 < minimum <= average <= maximum <= MAX_CHUNK_SIZE:
        raise ValueError("invalid or excessive chunk size bounds")
    table_size, table_magic = struct.unpack_from("<2Q", data, INDEX_HEADER_LEN)
    # casync specifies an incremental table with this UINT64_MAX sentinel.
    if table_size != UINT64_MAX or table_magic != CA_FORMAT_TABLE:
        raise ValueError("invalid casync table header")
    body_length = len(data) - INDEX_HEADER_LEN - TABLE_HEADER_LEN
    if body_length % TABLE_ENTRY_LEN:
        raise ValueError("truncated casync table")
    zero1, zero2, index_offset, footer_size, marker = struct.unpack_from("<5Q", data, len(data) - 40)
    if (zero1 or zero2 or index_offset != INDEX_HEADER_LEN
            or footer_size != len(data) - INDEX_HEADER_LEN
            or marker != CA_FORMAT_TABLE_TAIL_MARKER):
        raise ValueError("invalid casync table footer")
    chunks = []
    previous = 0
    lengths: dict[bytes, int] = {}
    for pos in range(INDEX_HEADER_LEN + TABLE_HEADER_LEN, len(data) - 40, 40):
        end = struct.unpack_from("<Q", data, pos)[0]
        digest = data[pos + 8:pos + 40]
        length = end - previous
        if not 0 < length <= maximum or end > (1 << 63) - 1:
            raise ValueError(f"invalid chunk offset/length at table byte {pos}")
        if digest in lengths and lengths[digest] != length:
            raise ValueError("same chunk digest has inconsistent lengths")
        lengths[digest] = length
        chunks.append((previous, length, digest))
        previous = end
    digest_name = "sha512_256" if flags & CA_FORMAT_SHA512_256 else "sha256"
    # Fail before downloading if the interpreter's crypto backend lacks it.
    hashlib.new(digest_name)
    return Index(previous, tuple(chunks), digest_name)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_chunk(blob: bytes, length: int, digest: bytes, digest_name: str) -> bytes:
    """Cap decompressed output, including frames with a deceptive content size."""
    # zstd's max_output_size is not a bound when a frame advertises its size.
    # Streaming reads instead cap the actual output before allocating it.
    context = zstandard.ZstdDecompressor(max_window_size=MAX_CHUNK_SIZE)
    try:
        parameters = zstandard.get_frame_parameters(blob)
        if parameters.window_size > MAX_CHUNK_SIZE:
            raise ValueError("zstd frame window exceeds memory limit")
        if parameters.content_size not in (-1, zstandard.CONTENTSIZE_UNKNOWN, length):
            raise ValueError("unexpected zstd frame content size")
        with context.stream_reader(io.BytesIO(blob), read_across_frames=True) as reader:
            raw = reader.read(length + 1)
            if len(raw) != length or reader.read(1):
                raise ValueError(f"chunk decompressed length differs from {length}")
        # This bounded one-shot pass also rejects truncated frames and trailing
        # bytes; content size was already bounded above by the streaming pass.
        frame_size = zstandard.frame_content_size(blob)
        # frame_content_size() returns -1, whereas CONTENTSIZE_UNKNOWN is the
        # unsigned C constant on some python-zstandard versions.
        if frame_size not in (-1, zstandard.CONTENTSIZE_UNKNOWN, length):
            raise ValueError("unexpected zstd frame content size")
        raw = context.decompress(blob, max_output_size=length, allow_extra_data=False)
    except zstandard.ZstdError as exc:
        raise ValueError(f"invalid zstd chunk: {exc}") from exc
    if len(raw) != length or hashlib.new(digest_name, raw).digest() != digest:
        raise ValueError("chunk length or digest mismatch")
    return raw


def chunk_url(store: str, digest_hex: str) -> str:
    return f"{store.rstrip('/')}/{digest_hex[:4]}/{digest_hex}.cacnk"


def download_chunk(stores: list[str], digest_hex: str, limit: int) -> bytes:
    last_error: Exception | None = None
    for store in stores:
        for attempt in range(4):
            try:
                request = Request(chunk_url(store, digest_hex), headers={"User-Agent": "switch-steamos-fetch/1.0"})
                with urlopen(request, timeout=30, context=HTTPS_CONTEXT) as response:
                    if urlparse(response.geturl()).scheme != "https":
                        raise ValueError("refusing non-HTTPS redirect")
                    blob = response.read(limit + 1)
                    if len(blob) > limit:
                        raise ValueError("compressed chunk exceeds size limit")
                    return blob
            except HTTPError as exc:
                last_error = exc
                if exc.code in (400, 401, 403, 404):
                    break
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
            if attempt < 3:
                time.sleep(min(4, 0.5 * 2 ** attempt))
    raise RuntimeError(f"unable to download chunk {digest_hex}: {last_error}")


def atomic_cache_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix="chunk-", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def assemble(index_path: str | Path, output: str | Path, stores: list[str],
             workers: int, expected_sha256: str, cache: str | Path) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("--sha256 must be an immutable 64-character manifest SHA256")
    expected_sha256 = expected_sha256.lower()
    if not 1 <= workers <= 128:
        raise ValueError("--workers must be between 1 and 128")
    for store in stores:
        parsed = urlparse(store)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("stores must be HTTPS base URLs without query or fragment")
    index = parse_caibx(index_path)
    output, cache = Path(output).absolute(), Path(cache).absolute()
    if output.is_symlink():
        raise ValueError("refusing a symlink output")
    if output.exists():
        if not output.is_file():
            raise ValueError("output exists and is not a regular file")
        if output.stat().st_size == index.image_size and sha256_file(output) == expected_sha256:
            print("Existing image already matches the requested SHA256.", flush=True)
            return
        raise FileExistsError("output already exists with different contents; choose another --output")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    by_hash: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
    for offset, length, digest in index.chunks:
        by_hash[digest].append((offset, length))
    print(f"Image: {index.image_size} bytes; {len(index.chunks)} chunks, {len(by_hash)} unique", flush=True)
    print(f"Cache: {cache}; workers: {workers}; digest: {index.digest_name}", flush=True)
    descriptor, temporary = tempfile.mkstemp(prefix=output.name + ".", suffix=".partial", dir=output.parent)
    write_lock = threading.Lock()
    started = time.monotonic()
    try:
        # One file object is shared, with seek/write protected on every platform.
        with os.fdopen(descriptor, "w+b", buffering=0) as image:
            image.truncate(index.image_size)

            def worker(digest: bytes, locations: list[tuple[int, int]]) -> int:
                digest_hex, length = digest.hex(), locations[0][1]
                chunk_path = cache / digest_hex[:4] / (digest_hex + ".cacnk")
                limit = length + length // 128 + 65536
                blob, raw = None, None
                if chunk_path.is_file() and not chunk_path.is_symlink():
                    try:
                        with chunk_path.open("rb") as stream:
                            blob = stream.read(limit + 1)
                        if len(blob) > limit:
                            raise ValueError("oversized cached chunk")
                        raw = validate_chunk(blob, length, digest, index.digest_name)
                    except (ValueError, OSError):
                        raw = None
                fetched = 0
                if raw is None:
                    if not stores:
                        raise RuntimeError(f"missing or invalid cache chunk {digest_hex}; supply --store")
                    blob = download_chunk(stores, digest_hex, limit)
                    raw = validate_chunk(blob, length, digest, index.digest_name)
                    atomic_cache_write(chunk_path, blob)
                    fetched = len(blob)
                # Newly created image is zero-filled; avoid writing zero chunks.
                if any(raw):
                    with write_lock:
                        for offset, expected in locations:
                            image.seek(offset)
                            if image.write(raw) != expected:
                                raise OSError(f"short image write at {offset}")
                return fetched

            downloaded, completed = 0, 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(worker, digest, locations) for digest, locations in by_hash.items()]
                try:
                    for future in as_completed(futures):
                        downloaded += future.result()
                        completed += 1
                        if completed % 200 == 0 or completed == len(futures):
                            elapsed = max(time.monotonic() - started, 0.001)
                            print(f"[{completed}/{len(futures)}] downloaded {downloaded / 1048576:.1f} MiB; {completed / elapsed:.1f} chunk/s", flush=True)
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
            image.flush()
            os.fsync(image.fileno())
        print("Verifying complete image SHA256 ...", flush=True)
        actual = sha256_file(temporary)
        if actual != expected_sha256:
            raise ValueError(f"image SHA256 mismatch: expected {expected_sha256}, got {actual}")
        if output.exists() or output.is_symlink():
            raise FileExistsError("output appeared during reconstruction; refusing to replace it")
        os.replace(temporary, output)
        print(f"Verified: {output}\nSHA256: {actual}", flush=True)
    finally:
        # Only remove the unique temporary file created by this invocation.
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, help="local rootfs.img.caibx")
    parser.add_argument("--output", required=True, help="image file to create; never a device")
    parser.add_argument("--store", action="append", default=[], help="HTTPS castr base URL; repeat for fallback stores")
    parser.add_argument("--sha256", required=True, help="expected SHA256 from the RAUC manifest")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cache", required=True, help="persistent compressed chunk cache directory")
    args = parser.parse_args()
    try:
        assemble(args.index, args.output, args.store, args.workers, args.sha256, args.cache)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; verified cached chunks can be reused by the same command.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
