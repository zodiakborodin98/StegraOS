#!/usr/bin/env python3
"""Read-only offline audit of an extracted SteamOS ARM root filesystem.

Only Python's standard library is used. Nothing from the target root is run.
This is a compatibility inventory, not a bootability or CPU-ISA certification.
"""

import argparse
from collections import deque
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys


SYSTEMD_REQUIREMENTS = "https://github.com/systemd/systemd/blob/v258/README"
SYSTEMD_NEWS = "https://github.com/systemd/systemd/blob/v258/NEWS"
MACHINES = {3: "i386", 40: "arm", 62: "x86_64", 183: "aarch64", 243: "riscv"}
CORE_FILES = (
    "/usr/lib/systemd/systemd", "/lib/systemd/systemd", "/sbin/init",
    "/usr/bin/gamescope", "/usr/bin/steam", "/usr/bin/steamwebhelper",
    "/usr/lib/libc.so.6", "/lib/aarch64-linux-gnu/libc.so.6",
    "/usr/lib/ld-linux-aarch64.so.1", "/lib/ld-linux-aarch64.so.1",
    "/usr/bin/bash", "/usr/bin/fex", "/usr/bin/FEXInterpreter", "/usr/bin/box64",
)
PACMAN_DATABASES = (
    "/usr/lib/holo/pacmandb/local", "/usr/lib/holo/pacmandb",
    "/var/lib/pacman/local",
)


class AuditError(ValueError):
    """Untrusted filesystem structure or malformed input."""


class RootFS:
    """Resolve Linux paths within a fixed root, including absolute symlinks.

    The tree must not be modified concurrently. This is an offline reader, not
    a sandbox against an attacker racing filesystem changes during the audit.
    Windows junctions/reparse points other than actual symlinks are rejected.
    """

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise AuditError("The extracted root must be a directory.")

    @staticmethod
    def parts(path):
        # Pacman directory names include epoch separators (e.g. foo-1:2.0-1).
        # ':' is an ordinary POSIX filename character, but on Windows can
        # select an NTFS alternate data stream. Drive-qualified paths remain
        # invalid Linux logical paths on every host.
        if ("\0" in path or "\\" in path or re.match(r"^[A-Za-z]:", path)
                or (os.name != "posix" and ":" in path)):
            raise AuditError("Unsupported Linux path component.")
        return path.split("/")

    def resolve(self, logical):
        pending = deque(self.parts(logical))
        resolved = []
        links = 0
        while pending:
            component = pending.popleft()
            if component in ("", "."):
                continue
            if component == "..":
                if not resolved:
                    raise AuditError("Path attempts to escape the extracted root.")
                resolved.pop()
                continue
            candidate = self.root.joinpath(*resolved, component)
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                links += 1
                if links > 40:
                    raise AuditError("Too many symlinks (possible cycle).")
                target = os.readlink(candidate)
                new_parts = self.parts(target)
                if target.startswith("/"):
                    resolved = []
                pending.extendleft(reversed(new_parts))
            else:
                if getattr(info, "st_file_attributes", 0) & 0x400:
                    raise AuditError("Unsupported filesystem reparse point.")
                if pending and not stat.S_ISDIR(info.st_mode):
                    raise AuditError("Non-directory in a target path.")
                resolved.append(component)
        return self.root.joinpath(*resolved)

    def read(self, logical, limit=1024 * 1024):
        path = self.resolve(logical)
        if not stat.S_ISREG(path.stat().st_mode):
            raise AuditError("Target is not a regular file.")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise AuditError("Metadata file exceeds the size limit.")
        return data


def kernel_version(value):
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise AuditError("Kernel version must begin with major.minor[.patch].")
    return tuple(int(part or 0) for part in match.groups())


def package_major(version):
    # Arch version syntax can include an epoch, e.g. 2:258.1-1.
    match = re.match(r"^(?:\d+:)?(\d+)(?:[.\-+~:]|$)", version)
    return int(match.group(1)) if match else None


def parse_desc(data):
    result = {}
    field = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.startswith("%") and line.endswith("%"):
            field = line.strip("%")
            result.setdefault(field, [])
        elif line and field:
            result[field].append(line)
    return {key: values[0] for key, values in result.items() if values}


def packages(fs):
    found = []
    issues = []
    seen_desc = set()
    dbs = []
    for database in PACMAN_DATABASES:
        try:
            directory = fs.resolve(database)
            if not directory.is_dir():
                continue
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
            dbs.append(database)
        except FileNotFoundError:
            continue
        except (OSError, AuditError) as exc:
            issues.append({"path": database, "error": type(exc).__name__})
            continue
        for entry in entries:
            # Metadata roots contain lock files, ALPM_DB_VERSION, and local/.
            # Never trust host-side symlink traversal through entry.is_dir().
            logical = database + "/" + entry.name + "/desc"
            try:
                resolved = fs.resolve(logical)
                if resolved in seen_desc:
                    continue
                data = parse_desc(fs.read(logical))
                seen_desc.add(resolved)
                if "NAME" not in data or "VERSION" not in data:
                    continue
                found.append({"name": data["NAME"], "version": data["VERSION"],
                              "architecture": data.get("ARCH", "unknown"),
                              "metadata_path": logical})
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, AuditError) as exc:
                # Skip ordinary non-package files without treating them as errors.
                if entry.name not in ("ALPM_DB_VERSION", "db.lck"):
                    issues.append({"path": logical, "error": type(exc).__name__})
    return sorted(found, key=lambda item: (item["name"], item["version"], item["metadata_path"])), dbs, issues


def inspect_elf(path):
    """Read ELF headers/notes without loading a complete executable into memory."""
    if not stat.S_ISREG(path.stat().st_mode):
        raise AuditError("ELF candidate is not a regular file.")
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size

        def read_at(offset, amount):
            if offset < 0 or amount < 0 or offset + amount > size:
                raise AuditError("Truncated ELF data.")
            stream.seek(offset)
            value = stream.read(amount)
            if len(value) != amount:
                raise AuditError("Truncated ELF read.")
            return value

        ident = stream.read(16)
        if not ident.startswith(b"\x7fELF"):
            return {"kind": "script" if ident.startswith(b"#!") else "non-ELF"}
        if len(ident) != 16 or ident[4] not in (1, 2) or ident[5] not in (1, 2):
            raise AuditError("Invalid ELF identification.")
        bits = 64 if ident[4] == 2 else 32
        endian = "<" if ident[5] == 1 else ">"
        fmt = endian + ("HHIQQQIHHHHHH" if bits == 64 else "HHIIIIIHHHHHH")
        header = struct.unpack(fmt, read_at(16, struct.calcsize(fmt)))
        machine, phoff, shoff = header[1], header[4], header[5]
        phentsize, phnum, shentsize, shnum = header[8:12]
        if phnum == 0xFFFF or (shoff and shnum == 0):
            raise AuditError("Extended ELF header counts are not supported by this audit.")
        if phnum > 4096 or shnum > 65535:
            raise AuditError("Unreasonable ELF header count.")
        ranges = set()
        interpreter = None
        for offset, stride, count, is_program in (
            (phoff, phentsize, phnum, True), (shoff, shentsize, shnum, False)
        ):
            header_format = endian + (
                ("IIQQQQQQ" if bits == 64 else "IIIIIIII") if is_program
                else ("IIQQQQIIQQ" if bits == 64 else "IIIIIIIIII")
            )
            header_size = struct.calcsize(header_format)
            if count and (not offset or stride < header_size):
                raise AuditError("Invalid ELF header table.")
            for index in range(count):
                fields = struct.unpack(header_format, read_at(offset + index * stride, header_size))
                if is_program:
                    kind = fields[0]
                    start, length = (fields[2], fields[5]) if bits == 64 else (fields[1], fields[4])
                    if kind == 3:
                        if length > 4096:
                            raise AuditError("Oversized ELF interpreter path.")
                        interpreter = read_at(start, length).rstrip(b"\0").decode("utf-8", "replace")
                    is_note = kind == 4
                else:
                    start, length = fields[4:6]
                    is_note = fields[1] == 7
                if is_note and length:
                    if length > 16 * 1024 * 1024:
                        raise AuditError("Oversized ELF note segment.")
                    ranges.add((start, length))
        abi_tags = []
        for start, length in sorted(ranges):
            note_data = read_at(start, length)
            cursor = 0
            while cursor + 12 <= len(note_data):
                namesz, descsz, kind = struct.unpack_from(endian + "III", note_data, cursor)
                cursor += 12
                name_end = cursor + namesz
                desc_start = cursor + ((namesz + 3) & ~3)
                desc_end = desc_start + descsz
                next_note = desc_start + ((descsz + 3) & ~3)
                if name_end > len(note_data) or desc_end > len(note_data):
                    raise AuditError("Truncated ELF note.")
                if note_data[cursor:name_end].rstrip(b"\0") == b"GNU" and kind == 1 and descsz >= 16:
                    os_id, major, minor, patch = struct.unpack_from(endian + "IIII", note_data, desc_start)
                    tag = {"os": "Linux" if os_id == 0 else "os-id-" + str(os_id),
                           "minimum_kernel": f"{major}.{minor}.{patch}"}
                    if tag not in abi_tags:
                        abi_tags.append(tag)
                cursor = next_note
        return {"kind": "ELF", "bits": bits, "endian": "little" if endian == "<" else "big",
                "machine_id": machine, "architecture": MACHINES.get(machine, "unknown"),
                "interpreter": interpreter, "gnu_abi_tags": abi_tags}


def audit(root, target_kernel="4.9.140", target_arch="aarch64"):
    fs = RootFS(root)
    target_version = kernel_version(target_kernel)
    package_list, databases, metadata_issues = packages(fs)
    findings = []

    def finding(level, code, message, **evidence):
        findings.append({"level": level, "code": code, "message": message, **evidence})

    os_release = {}
    try:
        content = fs.read("/etc/os-release").decode("utf-8", "replace")
        for line in content.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in ("ID", "ID_LIKE", "VERSION_ID", "PRETTY_NAME", "BUILD_ID"):
                    os_release[key] = value.strip().strip('"').strip("'")
    except (OSError, AuditError) as exc:
        finding("unknown", "os-release-unreadable", "OS identity metadata could not be read.", error=type(exc).__name__)
    if not package_list:
        finding("unknown", "no-package-metadata", "No readable installed pacman packages found in the known SteamOS/Arch database locations.")
    if metadata_issues:
        finding("warning", "package-metadata-errors", "Some package metadata could not be safely read.", details=metadata_issues)
    systemd_packages = [item for item in package_list if item["name"] == "systemd"]
    if not systemd_packages:
        finding("unknown", "systemd-version-unknown", "The installed systemd version is not established by package metadata.")
    for item in systemd_packages:
        major = package_major(item["version"])
        if major is None:
            finding("unknown", "systemd-version-unparsed", "Could not parse systemd's package version.", package=item)
        elif major >= 258:
            if target_version < (5, 4, 0):
                finding("blocker", "systemd-kernel-baseline", "systemd >= 258 requires upstream Linux >= 5.4; the target kernel is below that baseline. A patched vendor build needs separate source/runtime evidence.", package=item, source=SYSTEMD_REQUIREMENTS)
            finding("warning", "systemd-cgroup-v1-removed", "systemd >= 258 removed cgroup v1/legacy hierarchy support. Legacy cgroup boot flags cannot restore it; verify target kernel cgroup v2 capabilities.", package=item, source=SYSTEMD_NEWS)
        else:
            finding("warning", "systemd-older-needs-verification", "Version is below 258: this rule establishes no blanket Linux 4.9 incompatibility or compatibility. Check build patches, kernel configuration, syscalls and boot behavior.", package=item)
            finding("warning", "legacy-cgroup-boot-flags", "Do not blindly copy legacy cgroup boot flags such as systemd.unified_cgroup_hierarchy=0. Verify this systemd build's supported hierarchy and the Switch kernel configuration.", package=item)
    if len({item["version"] for item in systemd_packages}) > 1:
        finding("warning", "conflicting-systemd-metadata", "Package databases contain conflicting systemd versions; the active package database must be established.")

    binaries = []
    for logical in CORE_FILES:
        try:
            path = fs.resolve(logical)
            record = {"path": logical, "resolved_path": "/" + path.relative_to(fs.root).as_posix(), **inspect_elf(path)}
            if record["kind"] == "ELF":
                if record["architecture"] != target_arch:
                    finding("blocker", "core-elf-architecture", "A selected core executable/library is not native to the target architecture. Translation or a replacement would be required.", path=logical, architecture=record["architecture"])
                for tag in record["gnu_abi_tags"]:
                    if tag["os"] == "Linux" and kernel_version(tag["minimum_kernel"]) > target_version:
                        finding("blocker", "elf-kernel-abi", "An ELF GNU ABI tag declares a newer minimum Linux kernel than the target.", path=logical, minimum_kernel=tag["minimum_kernel"])
                interpreter = record.get("interpreter")
                if interpreter:
                    try:
                        interpreter_path = fs.resolve(interpreter)
                        if not stat.S_ISREG(interpreter_path.stat().st_mode):
                            raise AuditError("Interpreter is not a regular file.")
                    except (OSError, AuditError) as exc:
                        finding("blocker", "elf-interpreter-unavailable", "An ELF interpreter is unavailable inside the extracted root.", path=logical, interpreter=interpreter, error=type(exc).__name__)
            binaries.append(record)
        except FileNotFoundError:
            binaries.append({"path": logical, "kind": "missing"})
        except (OSError, AuditError, struct.error) as exc:
            binaries.append({"path": logical, "kind": "unreadable", "error": type(exc).__name__})
            finding("unknown", "core-file-unreadable", "A selected core file could not be safely inspected.", path=logical, error=type(exc).__name__)

    if not any(item["kind"] == "ELF" and item["path"] in CORE_FILES[:3] for item in binaries):
        finding("unknown", "init-elf-not-found", "No systemd/init ELF was inspected; this may be an incomplete extraction or a different filesystem layout.")
    relevant = [item for item in package_list if re.search(r"systemd|glibc|linux|gamescope|mesa|vulkan|nvidia|tegra|steam|fex|box64", item["name"], re.I)]
    return {
        "schema_version": 1,
        "target": {"architecture": target_arch, "kernel": target_kernel},
        "os_release": os_release,
        "package_databases": databases,
        "package_count": len(package_list),
        "relevant_packages": relevant,
        "binaries": binaries,
        "findings": findings,
        "status": "blocked" if any(item["level"] == "blocker" for item in findings) else "unverified",
        "limitations": [
            "ELF AArch64 architecture does not prove ARMv8.0/A57 instruction-set compatibility; instruction analysis/build flags and execution on the Switch are still required.",
            "A missing or sufficiently old GNU ABI tag does not prove compatibility: newer syscalls, library dependencies and CPU instructions may still be required.",
            "No target binaries were executed. Bootloader, kernel configuration, GPU ABI/Vulkan features, firmware, input, audio, suspend and games are untested.",
            "Package metadata describes the extracted tree and is not cryptographic proof that files match the advertised packages or source.",
            "Use a stationary, completely extracted tree; concurrent changes and partial copies invalidate the audit.",
        ],
    }


def text_report(report):
    lines = ["SteamOS extracted-rootfs audit (read-only)",
             "Target: {architecture}, Linux {kernel}".format(**report["target"]),
             "Status: " + report["status"].upper(),
             "OS: " + json.dumps(report["os_release"], ensure_ascii=True),
             "Package records: " + str(report["package_count"]), "", "Relevant packages:"]
    lines += ["  {name} {version} [{architecture}] ({metadata_path})".format(**item) for item in report["relevant_packages"]]
    lines += ["", "Selected files:"]
    for item in report["binaries"]:
        detail = item["kind"]
        if item["kind"] == "ELF":
            detail += f" {item['architecture']} {item['bits']}-bit"
            detail += "; GNU ABI tags: " + json.dumps(item["gnu_abi_tags"])
        lines.append("  " + item["path"] + ": " + detail)
    lines += ["", "Findings:"]
    for item in report["findings"]:
        lines.append("  " + item["level"].upper() + " [" + item["code"] + "] " + item["message"])
        for field in ("path", "package", "minimum_kernel", "source", "details"):
            if field in item:
                lines.append("    " + field + ": " + json.dumps(item[field], ensure_ascii=True))
    lines += ["", "Limitations:"] + ["  " + item for item in report["limitations"]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Exit codes: 0 = unverified with no detected blocker; 1 = blocker found; 2 = invalid input. Reports go to stdout; no files are modified.",
    )
    parser.add_argument("root", help="Path to an already extracted SteamOS root filesystem")
    parser.add_argument("--target-kernel", default="4.9.140", help="Target Switchroot kernel release (default: 4.9.140)")
    parser.add_argument("--target-arch", choices=("aarch64", "x86_64", "arm", "i386"), default="aarch64")
    parser.add_argument("--json", action="store_true", help="Print a JSON report to stdout instead of text")
    args = parser.parse_args(argv)
    try:
        report = audit(args.root, args.target_kernel, args.target_arch)
    except (OSError, AuditError) as exc:
        parser.exit(2, "audit error: " + str(exc) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=True) if args.json else text_report(report))
    # 1 identifies explicit blockers, 0 means unverified rather than compatible.
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    sys.exit(main())
