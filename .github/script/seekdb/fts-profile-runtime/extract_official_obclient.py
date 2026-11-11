#!/usr/bin/env python3
"""Offline, pinned OceanBase obclient 2.2.3 extraction. Python 3.9+ stdlib only.

Run inside a private user-owned directory containing both official RPMs.
Never installs packages, executes payloads, or creates archive links.
"""
import argparse
import hashlib
import json
import lzma
import os
import re
import stat
import struct
import sys

BASE = "https://mirrors.oceanbase.com/oceanbase/community/stable/el/8/x86_64/"
SPECS = {
    "obclient": {
        "rpm": "obclient-2.2.3-1.el8.x86_64.rpm", "bytes": 13728920,
        "sha256": "35e38b580996fc583b0eaa961bd78911feff08981624154851db51d7e5aa935d",
        "cpioBytes": 59575212,
        "cpioSha256": "6022dca38b1e5aa4bbe2e00cbdf81d87258c15e128be29ac82ca9623e1707981",
        "entries": 23, "member": "u01/obclient/bin/obclient", "fileBytes": 14592368,
        "fileSha256": "798aa952d536563ee0291e69251a6d0cd0d85e0c470d5499768fa0731ff3c1be",
        "output": "bin/obclient",
    },
    "libobclient": {
        "rpm": "libobclient-2.2.3-1.el8.x86_64.rpm", "bytes": 1766420,
        "sha256": "e53cadb0d91c1a0d78369f0c9488be8d774f03ea09346e7b218799a5318a9a68",
        "cpioBytes": 7824736,
        "cpioSha256": "647dcf0f1fdffb7309286181d81ff581937d68030e49752c20e3d9e8ebeed635",
        "entries": 60, "member": "u01/obclient/lib/libobclnt.so.2.2.3",
        "fileBytes": 2544912,
        "fileSha256": "ad5113d2b2b3406b19cd3684a8d0859ebdcf810ec3afc8db7eda989b64281be7",
        "output": "lib/libobclnt.so.2.2.3",
    },
}
# Known metadata links from the two pinned admissions: inspect only, NEVER create.
IGNORED_LINKS = {"u01/obclient/lib/libobclnt.so": "libobclnt.so.2",
                 "u01/obclient/lib/libobclnt.so.2": "libobclnt.so.2.2.3"}
IGNORED_LINKS.update({"usr/lib/.build-id/" + build: "../../../../u01/obclient/" + target
    for build, target in (
        ("18/1b424a3e5450e57b0a109eaad924781ee06ce9", "bin/mysqlimport"),
        ("2e/0993b2fa9c3cd5ff21140ae851032c71fad81f", "bin/mysqltest"),
        ("4e/e24ed2048773e68e7ee3a8a83828366fe24853", "bin/mysql_waitpid"),
        ("62/842b89065f3ace3bdd13c5b998bbbc9c56f8e3", "bin/obclient"),
        ("87/eec81dc50eeba9a43d8bad6aba0dc88ae8a7b8", "bin/mysqldump"),
        ("1e/f5e27874a67ac01b76c4f2f2990053da43651a", "lib/plugin/dialog.so"),
        ("29/263fbef4a559222ba8badef0611cc25926a2fd", "bin/mariadb_config"),
        ("aa/d2345edd5f539044d0185268c8054961cb92c0", "lib/plugin/remote_io.so"),
        ("d7/e335aa3f8de93fd1dc7c40a085301ecaaad357", "lib/libobclnt.so.2.2.3"),
        ("d7/ea1e21f43a01c557937c83c249321f4e8fefff", "lib/plugin/auth_gssapi_client.so"),
        ("e8/c81acefec0182b8069bcf1e36b8379625d0178", "lib/plugin/mysql_clear_password.so"),
    )})
MAX_CPIO = 64 * 1024**2
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pinned_rpm(path, spec):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        require(stat.S_ISREG(meta.st_mode) and meta.st_size == spec["bytes"],
                "RPM is not the expected-sized regular file: " + spec["rpm"])
        data = stream.read(spec["bytes"] + 1)
    require(len(data) == spec["bytes"] and sha(data) == spec["sha256"],
            "RPM SHA256 mismatch: " + spec["rpm"])
    return data


def rpm_header(data, offset):
    require(offset + 16 <= len(data), "Truncated RPM header")
    magic, count, size = struct.unpack_from(">8sII", data, offset)
    require(magic == b"\x8e\xad\xe8\x01\0\0\0\0" and count <= 4096
            and size <= 4 * 1024**2, "Invalid or oversized RPM header")
    start = offset + 16 + count * 16
    end = start + size
    require(end <= len(data), "Truncated RPM header store")
    tags = {}
    for i in range(count):
        tag, kind, at, number = struct.unpack_from(">IIII", data, offset + 16 + i * 16)
        require(tag not in tags and at <= size, "Invalid/duplicate RPM tag")
        tags[tag] = (kind, at, number)
    return end, tags, data[start:end]


def unpack_rpm(data, key, spec):
    require(data[:6] == b"\xed\xab\xee\xdb\x03\0" and len(data) >= 96,
            "Invalid RPM v3 lead")
    end, _, _ = rpm_header(data, 96)
    aligned = (end + 7) & ~7
    require(not any(data[end:aligned]), "Nonzero RPM signature padding")
    offset, tags, store = rpm_header(data, aligned)
    def string(tag):
        require(tag in tags and tags[tag][0] == 6 and tags[tag][2] == 1,
                "Missing/wrong RPM string tag")
        at = tags[tag][1]
        stop = store.find(b"\0", at)
        require(stop >= at, "Unterminated RPM tag")
        return store[at:stop].decode("ascii")
    require((string(1000), string(1001), string(1002), string(1124), string(1125))
            == (key, "2.2.3", "1.el8", "cpio", "xz"), "Unexpected RPM identity/payload")
    require(data[offset:offset+6] == b"\xfd7zXZ\0", "Payload is not pinned xz format")
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=128 * 1024**2)
    payload = decoder.decompress(data[offset:], max_length=MAX_CPIO + 1)
    require(decoder.eof and not decoder.unused_data and len(payload) <= MAX_CPIO,
            "Truncated, trailing, or oversized xz payload")
    require(len(payload) == spec["cpioBytes"] and sha(payload) == spec["cpioSha256"],
            "Pinned CPIO size/SHA256 mismatch")
    return payload, offset


def cpio_member(data, wanted):
    require(len(data) <= MAX_CPIO, "CPIO total bound exceeded")
    position, seen, result = 0, {}, None
    def take(size):
        nonlocal position
        require(0 <= size <= len(data) - position, "Truncated CPIO")
        part = data[position:position + size]
        position += size
        return part
    while True:
        header = take(110)
        require(header[:6] in (b"070701", b"070702")
                and re.fullmatch(b"[0-9a-fA-F]{104}", header[6:]), "Invalid newc header")
        fields = [int(header[6+i*8:14+i*8], 16) for i in range(13)]
        _, mode, _, _, nlink, _, size, _, _, _, _, namesize, checksum = fields
        require(2 <= namesize <= 512 and size <= 32 * 1024**2, "CPIO member bound exceeded")
        raw_name = take(namesize)
        require(raw_name[-1:] == b"\0" and b"\0" not in raw_name[:-1], "Invalid CPIO name")
        name = raw_name[:-1].decode("ascii")
        require(not any(take((-(110 + namesize)) % 4)), "Nonzero CPIO name padding")
        if name == "TRAILER!!!":
            require(size == 0 and not any(data[position:]), "Invalid CPIO trailer")
            break
        name = name[2:] if name.startswith("./") else name
        require(re.fullmatch(r"[A-Za-z0-9_./+\-]+", name)
                and not name.startswith("/") and all(p not in ("", ".", "..") for p in name.split("/"))
                and name not in seen and len(seen) < 256 and not mode & 0o7000,
                "Unsafe/duplicate CPIO path or mode")
        kind = stat.S_IFMT(mode)
        require(kind in (stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK), "Special CPIO type rejected")
        require(kind != stat.S_IFREG or nlink == 1, "CPIO hardlink rejected")
        require(kind != stat.S_IFDIR or size == 0, "CPIO directory payload rejected")
        content = take(size)
        require(not any(take((-size) % 4)), "Nonzero CPIO data padding")
        require((header[:6] == b"070701" and checksum == 0)
                or (header[:6] == b"070702" and sum(content) % 2**32 == checksum), "CPIO checksum mismatch")
        if kind == stat.S_IFLNK:
            require(name != wanted and name in IGNORED_LINKS
                    and content == IGNORED_LINKS[name].encode("ascii"), "Unapproved CPIO link rejected")
        if name == wanted:
            require(kind == stat.S_IFREG, "Selected member is not a regular file")
            result = content
        seen[name] = kind
    for name in seen:
        parts = name.split("/")
        require(all(seen.get("/".join(parts[:i])) in (None, stat.S_IFDIR)
                    for i in range(1, len(parts))), "CPIO path crosses a non-directory")
    require(result is not None, "Selected CPIO member absent")
    return result, len(seen)


def write_file(parent, name, content, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)


def run(args):
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.output),
            "Output must be a NEW simple directory name inside the current directory")
    current = os.open(".", os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        meta = os.fstat(current)
        require(meta.st_uid == os.geteuid() and not meta.st_mode & 0o022,
                "Run inside a user-owned directory that is not group/other writable")
        # Both whole-file pins pass BEFORE either RPM header/payload is parsed.
        rpms = {key: pinned_rpm(getattr(args, key + "_rpm"), spec) for key, spec in SPECS.items()}
        files, packages = {}, []
        for key, spec in SPECS.items():
            payload, offset = unpack_rpm(rpms[key], key, spec)
            content, count = cpio_member(payload, spec["member"])
            require(count == spec["entries"] and len(content) == spec["fileBytes"]
                    and sha(content) == spec["fileSha256"], "Selected member count/size/SHA256 mismatch")
            files[key] = content
            packages.append({"name": spec["rpm"], "source": BASE + spec["rpm"],
                "bytes": len(rpms[key]), "sha256": sha(rpms[key]), "payloadCompression": "xz",
                "payloadOffset": offset, "cpioBytes": len(payload), "cpioSha256": sha(payload), "entries": count})
        os.mkdir(args.output, 0o700, dir_fd=current)  # fails rather than reusing ANY existing path
        root = os.open(args.output, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=current)
        try:
            rows = []
            for key, spec in SPECS.items():
                materialized = key == "obclient" or args.companion_library
                if materialized:
                    directory, filename = spec["output"].split("/")
                    os.mkdir(directory, 0o700, dir_fd=root)
                    child = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=root)
                    try:
                        write_file(child, filename, files[key], 0o755 if key == "obclient" else 0o644)
                        check = os.open(filename, os.O_RDONLY | NOFOLLOW, dir_fd=child)
                        with os.fdopen(check, "rb") as stream:
                            saved = stream.read(spec["fileBytes"] + 1)
                        require(len(saved) == spec["fileBytes"] and sha(saved) == spec["fileSha256"],
                                "Written file verification failed")
                    finally:
                        os.close(child)
                rows.append({"member": spec["member"], "source": BASE + spec["rpm"],
                    "bytes": len(files[key]), "sha256": sha(files[key]), "materialized": materialized,
                    "output": spec["output"] if materialized else None})
            receipt = {"status": "verified_and_extracted", "packages": packages, "files": rows,
                "requiredVendorSharedLibraries": [], "archiveLinksCreated": 0, "payloadsExecuted": 0,
                "note": "obclient has no libobclnt dependency. Companion library is verification-only unless explicitly requested. No libaio is staged or changed."}
            write_file(root, "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode(), 0o600)
        finally:
            os.close(root)
    finally:
        os.close(current)
    print(json.dumps({"status": "verified_and_extracted", "client": args.output + "/bin/obclient",
                      "receipt": args.output + "/receipt.json", "sha256": SPECS["obclient"]["fileSha256"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obclient-rpm", default=SPECS["obclient"]["rpm"])
    parser.add_argument("--libobclient-rpm", default=SPECS["libobclient"]["rpm"])
    parser.add_argument("--output", required=True, help="new simple directory name under the current directory")
    parser.add_argument("--companion-library", action="store_true", help="also copy verified libobclnt.so.2.2.3; not needed by this client")
    arguments = parser.parse_args()
    try:
        run(arguments)
    except (ValueError, OSError, lzma.LZMAError) as error:
        sys.exit("ERROR: " + str(error))
