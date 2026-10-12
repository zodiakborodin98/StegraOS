#!/usr/bin/env python3
"""Extract named GPT partitions from a Steam Frame ZIP, without root or mounts.

This is an inspection/preparation tool. It does not produce a Switch boot image.
Reads the complete ZIP member to validate its CRC before marking output complete.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import uuid
import zipfile
import zlib


def parse_gpt(prefix, image_size):
    sector = next((s for s in (512, 4096)
                   if prefix[s:s + 8] == b"EFI PART"), None)
    if sector is None:
        raise ValueError("GPT header not found at sector 512 or 4096")
    size, crc = struct.unpack_from("<II", prefix, sector + 12)
    if not 92 <= size <= sector:
        raise ValueError("Invalid GPT header size")
    header = bytearray(prefix[sector:sector + size])
    header[16:20] = bytes(4)
    if zlib.crc32(header) != crc:
        raise ValueError("GPT header CRC mismatch")
    first_usable, last_usable = struct.unpack_from("<QQ", prefix, sector + 40)
    entry_lba = struct.unpack_from("<Q", prefix, sector + 72)[0]
    count, stride, entries_crc = struct.unpack_from("<III", prefix, sector + 80)
    if count > 4096 or stride < 128 or stride > 4096:
        raise ValueError("Invalid GPT partition table dimensions")
    table = prefix[entry_lba * sector:entry_lba * sector + count * stride]
    if len(table) != count * stride:
        raise ValueError("GPT table exceeds inspected prefix")
    if zlib.crc32(table) != entries_crc:
        raise ValueError("GPT partition table CRC mismatch")
    parts = []
    for index in range(count):
        entry = table[index * stride:(index + 1) * stride]
        if entry[:16] == bytes(16):
            continue
        start, end = struct.unpack_from("<QQ", entry, 32)
        if not first_usable <= start <= end <= last_usable:
            raise ValueError("Partition is outside GPT usable range")
        if (end + 1) * sector > image_size:
            raise ValueError("Partition is outside disk image")
        parts.append({
            "index": index + 1,
            "name": entry[56:128].decode("utf-16le").rstrip("\0"),
            "type_guid": str(uuid.UUID(bytes_le=entry[:16])),
            "uuid": str(uuid.UUID(bytes_le=entry[16:32])),
            "start_lba": start, "end_lba": end,
            "offset_bytes": start * sector,
            "size_bytes": (end - start + 1) * sector,
        })
    ordered = sorted(parts, key=lambda p: p["offset_bytes"])
    for prev, cur in zip(ordered, ordered[1:]):
        if prev["offset_bytes"] + prev["size_bytes"] > cur["offset_bytes"]:
            raise ValueError("Overlapping partitions")
    return sector, parts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--partitions", nargs="+", default=["rootfs-A", "var-A", "home"])
    args = parser.parse_args()
    if not args.archive.is_file():
        parser.error("Archive must be an existing regular file")
    if args.output.exists():
        parser.error("Output directory must not exist; existing files are never overwritten")
    args.output.mkdir(parents=True)
    manifest = {"status": "incomplete", "archive": args.archive.name,
                "archive_bytes": args.archive.stat().st_size}
    outputs = []
    try:
        with args.archive.open("rb") as f:
            manifest["archive_sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
        with zipfile.ZipFile(args.archive) as archive:
            images = [i for i in archive.infolist() if i.filename.endswith(".img")]
            if len(images) != 1:
                raise ValueError("Expected exactly one raw .img ZIP member")
            member = images[0]
            if member.flag_bits & 1:
                raise ValueError("Encrypted ZIP is unsupported")
            manifest.update(member=member.filename, image_bytes=member.file_size,
                            zip_crc32=f"{member.CRC:08x}")
            with archive.open(member) as stream:
                prefix = stream.read(4 * 1024 * 1024)
                sector, parts = parse_gpt(prefix, member.file_size)
                manifest.update(sector_bytes=sector, partitions=parts)
                selected = []
                for name in args.partitions:
                    matches = [p for p in parts if p["name"] == name]
                    if len(matches) != 1:
                        raise ValueError(f"Expected one partition named {name!r}")
                    if name in [p["name"] for p in selected]:
                        raise ValueError("Duplicate requested partition")
                    selected.append(matches[0])
                needed = sum(p["size_bytes"] for p in selected) + 64 * 1024 * 1024
                if shutil.disk_usage(args.output).free < needed:
                    raise ValueError(f"Need at least {needed} free bytes for extraction")
                for p in selected:
                    # Names are not used as paths: numbered files also support untrusted GPT names.
                    filename = f"partition-{p['index']:02d}.img"
                    path = args.output / (filename + ".partial")
                    outputs.append((p, path, path.open("xb"), hashlib.sha256(), filename))
                offset = 0
                data = prefix
                last_gib = -1
                while data:
                    for p, path, f, digest, filename in outputs:
                        start = max(offset, p["offset_bytes"])
                        end = min(offset + len(data), p["offset_bytes"] + p["size_bytes"])
                        if start < end:
                            block = data[start - offset:end - offset]
                            f.write(block)
                            digest.update(block)
                    offset += len(data)
                    if offset // (1024 ** 3) != last_gib:
                        last_gib = offset // (1024 ** 3)
                        print(f"Read {offset / 1024**3:.2f} / {member.file_size / 1024**3:.2f} GiB", flush=True)
                    data = stream.read(8 * 1024 * 1024)
                if offset != member.file_size:
                    raise ValueError("Truncated ZIP member")
        manifest["extracted"] = []
        for p, path, f, digest, filename in outputs:
            f.close()
            if path.stat().st_size != p["size_bytes"]:
                raise ValueError(f"Partition length mismatch: {p['name']}")
            path.rename(args.output / filename)
            manifest["extracted"].append({"name": p["name"], "file": filename,
                "sha256": digest.hexdigest(), "size_bytes": p["size_bytes"]})
        manifest["status"] = "complete"
        manifest["validated"] = ["ZIP member CRC32", "primary GPT header CRC32",
                                 "GPT entry array CRC32", "partition bounds", "partition lengths"]
        manifest["not_validated"] = ["vendor signature or published SHA256",
                                    "Switch CPU compatibility", "Switch boot", "GPU", "suspend/resume"]
    except Exception as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        print(manifest["error"], file=sys.stderr)
    finally:
        for _, _, f, _, _ in outputs:
            f.close()
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if manifest["status"] != "complete":
        return 1
    print(args.output / "manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
