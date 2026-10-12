#!/usr/bin/env python3
"""Fetch the exact source files recorded in source-lock.json and verify SHA256.

Hashes identify the files inspected during this project; they are not vendor signatures.
Never executes downloaded code or writes a block device.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request


def checksum(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", type=Path, default=Path(__file__).resolve().parents[1] / "source-lock.json")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    lock = json.loads(args.lock.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    for item in lock["sources"]:
        name = item["filename"]
        if Path(name).name != name or name in (".", ".."):
            raise ValueError("Invalid filename in lock file")
        dest = args.output / name
        if dest.is_symlink():
            raise ValueError(f"Refusing symlink: {dest}")
        if dest.exists():
            if not dest.is_file() or dest.stat().st_size != item["size_bytes"] or checksum(dest) != item["sha256"]:
                raise ValueError(f"Existing source differs from lock file: {dest}")
            print(f"Verified existing {name}", flush=True)
            continue
        if not item["url"].startswith("https://"):
            raise ValueError("Only HTTPS sources are supported")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=args.output, prefix=name + ".", suffix=".partial", delete=False) as f:
                temp = Path(f.name)
                sha = hashlib.sha256()
                size = 0
                print(f"Downloading {name} ({item['size_bytes']} bytes)", flush=True)
                with urllib.request.urlopen(item["url"], timeout=45) as response:
                    while data := response.read(4 * 1024 * 1024):
                        size += len(data)
                        if size > item["size_bytes"]:
                            raise ValueError("Download exceeds locked file size")
                        f.write(data)
                        sha.update(data)
            if size != item["size_bytes"] or sha.hexdigest() != item["sha256"]:
                raise ValueError(f"Size or SHA256 mismatch for {name}")
            # Hard-link creation fails if another process has created the destination.
            dest.hardlink_to(temp)
            print(f"Verified {name}", flush=True)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
