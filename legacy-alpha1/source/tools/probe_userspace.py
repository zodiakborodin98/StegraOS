#!/usr/bin/env python3
"""Read selected Frame files and probe ELF startup using QEMU Cortex-A57.

Requires dissect.btrfs, backports.zstd, readelf and qemu-aarch64-static.
No disk mounts, writes to devices, network calls or guest package installs.
QEMU user mode uses the host kernel; it does NOT validate the Switch kernel/GPU.
"""
import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import re
import subprocess
from dissect.btrfs import Btrfs


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("partition", type=Path)
    ap.add_argument("--qemu", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        ap.error("Output must be a new directory")
    out = args.output.resolve()
    (out / "lib").mkdir(parents=True)
    (out / "bin").mkdir()
    report = {"scope": "ELF startup on emulated Cortex-A57, using host kernel",
              "files": [], "commands": [], "complete_os_image": False}
    qemu = str(args.qemu.resolve())
    with args.partition.open("rb") as f:
        fs = Btrfs(f)

        def file_bytes(path):
            visited = set()
            current = path
            while True:
                if current in visited:
                    raise ValueError(f"Symlink loop at {path}")
                visited.add(current)
                inode = fs.get(current)
                if not inode.is_symlink():
                    break
                current = posixpath.normpath(posixpath.join(posixpath.dirname(current), inode.link))
            data = inode.open().read()
            if len(data) != inode.size:
                raise ValueError(f"Short read at {path}")
            return data

        def save(path, dest):
            data = file_bytes(path)
            dest.write_bytes(data)
            dest.chmod(0o755 if data.startswith(b"\x7fELF") else 0o644)
            report["files"].append({"path": path, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()})
            return data

        for path in ("/usr/lib/os-release", "/etc/fstab", "/etc/makepkg.conf"):
            report[path] = file_bytes(path).decode()
        names = [n for n, _ in fs.get("/usr/lib/holo/pacmandb/local").iterdir()
                 if n not in (".", "..")]
        report["packages"] = [n for n in names if any(k in n.lower() for k in
            ("systemd-", "glibc-", "gamescope-", "deckard-", "linux-", "steam", "fex"))]
        libraries = {}
        for directory in ("/usr/lib", "/usr/lib/systemd"):
            for name, inode in fs.get(directory).iterdir():
                if ".so" in name:
                    libraries.setdefault(name, directory + "/" + name)
        queue = []
        programs = {"bash": "/usr/bin/bash", "true": "/usr/bin/true",
                    "systemd": "/usr/lib/systemd/systemd", "gamescope": "/usr/bin/gamescope"}
        for name, path in programs.items():
            dest = out / "bin" / name
            save(path, dest)
            queue.append(dest)
        loader = out / "lib/ld-linux-aarch64.so.1"
        save("/usr/lib/ld-linux-aarch64.so.1", loader)
        queue.append(loader)
        missing = set()
        while queue:
            binary = queue.pop()
            result = subprocess.run(["readelf", "-d", str(binary)],
                                    capture_output=True, text=True, check=True)
            for needed in re.findall(r"\(NEEDED\).*?\[(.*?)\]", result.stdout):
                target = out / "lib" / needed
                if target.exists():
                    continue
                if needed not in libraries:
                    missing.add(needed)
                    continue
                save(libraries[needed], target)
                queue.append(target)
        report["missing_dt_needed"] = sorted(missing)
        # Each binary is started explicitly through the image's own dynamic loader.
        # This avoids a chroot or host library contamination.
        for cpu in ("cortex-a57", "max"):
            for name in programs:
                command = [qemu, "-cpu", cpu, str(loader), "--library-path", str(out / "lib"),
                           str(out / "bin" / name)]
                if name != "true":
                    command.append("--version")
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                    record = {"cpu": cpu, "program": name, "command": command,
                        "exit_code": result.returncode, "stdout": result.stdout[:12000],
                        "stderr": result.stderr[:12000]}
                except subprocess.TimeoutExpired:
                    record = {"cpu": cpu, "program": name, "command": command, "timeout": True}
                report["commands"].append(record)
                print(json.dumps({k: v for k, v in record.items() if k != "command"}), flush=True)
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(out / "report.json")


if __name__ == "__main__":
    main()
