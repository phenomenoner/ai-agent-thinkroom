from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUNDLE = Path(__file__).parent / "bundled_skills"
RECEIPT_REL = Path(".thinkroom/skills-receipt-v1.json")
_KNOWN_PREVIOUS_RECEIPTS = {
    "8544d4efad9b1d605b8cd3e9706ba94963286a12d617809f79fdd50ae92cc9a5": {
        "bundle_version": "0.2.0",
        "files": {
            "thinkroom-install/SKILL.md": (
                "77cbe74de042bdd473144c0799a8dfa89363b1bc2254703d67bfefc1ec6b52fd"
            ),
            "thinkroom-operate/SKILL.md": (
                "6d905d1e84bfd0244d5f264d66f3e58ca7b8d0f997bd51676f76b971c08601d8"
            ),
            "thinkroom-trigger/SKILL.md": (
                "a1debe46591cb772b54e15dbf37637c7f46b1607c0d07fa542bc211d01e89a99"
            ),
        },
    },
    # Git for Windows could materialize the original text-only v0.2 bundle with
    # CRLF before the bundle declared stable LF attributes. Accept only the
    # exact historical manifest and payload digests for that checkout shape.
    "8ed21c60f8db3101ea213b0872a2eed6003766508d7f49a19f050371eb565e9f": {
        "bundle_version": "0.2.0",
        "files": {
            "thinkroom-install/SKILL.md": (
                "612bfb511f0517599bf93d32fcdab5415d3db7104dc86f57b3b732a7fa54c260"
            ),
            "thinkroom-operate/SKILL.md": (
                "c2baccebe91e3029dc8aef45d8539b0849be86e05b3777919b214fceeb60cd41"
            ),
            "thinkroom-trigger/SKILL.md": (
                "2630f95045e2aab2831d7c7982b4072544016714db4b10727e21f380593b5728"
            ),
        },
    },
    "5f999b5e4bcb26073bfc0a97eafa8422fadbaa122b9dd6583fa0886b32df6568": {
        "bundle_version": "0.2.1",
        "files": {
            "thinkroom-install/SKILL.md": (
                "74d1900deb32dc4215a17d1b76270e34cd533221b55700523dbf54e1ccd6ae9c"
            ),
            "thinkroom-install/agents/openai.yaml": (
                "f8e4e48ed350ffe45715b61599e066352fd39c8d3ab04f671db80210aba400b2"
            ),
            "thinkroom-operate/SKILL.md": (
                "4cc408d77e97e386523541c3c0306b45c322bf631003512b3296526f6e54d5a0"
            ),
            "thinkroom-operate/agents/openai.yaml": (
                "0ac5a5acb8f37605692721f87b1688de5494601c0dd0a1b9346cc8a480ca7823"
            ),
            "thinkroom-trigger/SKILL.md": (
                "a1debe46591cb772b54e15dbf37637c7f46b1607c0d07fa542bc211d01e89a99"
            ),
            "thinkroom-trigger/agents/openai.yaml": (
                "e2e2f1db29df78feb7941c729d26bb53dfd3c1fdf5c24d02c34b65c4ee8e8c3e"
            ),
        },
    },
    "c24944688e4a6e0b95a8d43c8e4e9177d9ec0388dc2dc9bd1cb1c88d6614975a": {
        "bundle_version": "0.2.2",
        "files": {
            "thinkroom-install/SKILL.md": (
                "74d1900deb32dc4215a17d1b76270e34cd533221b55700523dbf54e1ccd6ae9c"
            ),
            "thinkroom-install/agents/openai.yaml": (
                "f8e4e48ed350ffe45715b61599e066352fd39c8d3ab04f671db80210aba400b2"
            ),
            "thinkroom-operate/SKILL.md": (
                "9f282ae764d72efa74f98044978aa6d128adb89fec8cbcc8e21c3861977be6f6"
            ),
            "thinkroom-operate/agents/openai.yaml": (
                "0ac5a5acb8f37605692721f87b1688de5494601c0dd0a1b9346cc8a480ca7823"
            ),
            "thinkroom-trigger/SKILL.md": (
                "a1debe46591cb772b54e15dbf37637c7f46b1607c0d07fa542bc211d01e89a99"
            ),
            "thinkroom-trigger/agents/openai.yaml": (
                "e2e2f1db29df78feb7941c729d26bb53dfd3c1fdf5c24d02c34b65c4ee8e8c3e"
            ),
        },
    },
}


def _manifest() -> tuple[dict[str, Any], bytes, dict[str, bytes]]:
    manifest_path = BUNDLE / "manifest.json"
    if BUNDLE.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("manifest path may not be symlink")
    raw = manifest_path.read_bytes()
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "bundle_version", "product_version", "entries"}
        or data.get("schema_version") != "1"
        or not isinstance(data.get("bundle_version"), str)
        or not 1 <= len(data["bundle_version"]) <= 128
        or not isinstance(data.get("product_version"), str)
        or not 1 <= len(data["product_version"]) <= 128
        or not isinstance(data.get("entries"), list)
        or not data["entries"]
    ):
        raise ValueError("invalid manifest")
    seen: set[str] = set()
    payloads: dict[str, bytes] = {}
    for entry in data["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ValueError("invalid manifest entry")
        rel, digest = entry.get("path"), entry.get("sha256")
        path = Path(rel) if isinstance(rel, str) else Path(".")
        if (
            not isinstance(rel, str)
            or "\\" in rel
            or path.is_absolute()
            or path.as_posix() != rel
            or ".." in path.parts
            or rel in seen
            or not isinstance(digest, str)
            or len(digest) != 64
            or digest.lower() != digest
        ):
            raise ValueError("invalid manifest entry")
        seen.add(rel)
        source = BUNDLE / rel
        if any(
            part.is_symlink()
            for part in [
                BUNDLE,
                *[BUNDLE.joinpath(*path.parts[:i]) for i in range(1, len(path.parts) + 1)],
            ]
        ):
            raise ValueError("manifest source path may not contain symlink")
        if not source.is_file():
            raise ValueError("manifest payload mismatch")
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("manifest payload mismatch")
        payloads[rel] = payload
    actual = {
        str(p.relative_to(BUNDLE)).replace(os.sep, "/")
        for p in BUNDLE.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    if any(p.is_symlink() for p in BUNDLE.rglob("*")) or actual != seen:
        raise ValueError("manifest payload set mismatch")
    return data, raw, payloads


def _target_path(root: Path, relative: str) -> Path:
    current = root
    for part in Path(relative).parts:
        if current.is_symlink():
            raise ValueError("unsafe target path")
        current = current / part
    if current.is_symlink():
        raise ValueError("unsafe target path")
    return current


def _preflight_managed_path(root: Path, relative: str | Path) -> None:
    parts = Path(relative).parts
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            value = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(value.st_mode):
            raise ValueError("unsafe target path")
        final = index == len(parts) - 1
        if final:
            if not stat.S_ISREG(value.st_mode):
                raise ValueError("unsafe managed file")
        elif not stat.S_ISDIR(value.st_mode):
            raise ValueError("unsafe target directory")


def _preflight_install(
    root: Path, manifest: dict[str, Any], raw_manifest: bytes
) -> list[dict[str, str]]:
    _safe_target(root)
    managed = [*[entry["path"] for entry in manifest["entries"]], RECEIPT_REL.as_posix()]
    for relative in managed:
        _preflight_managed_path(root, relative)
    receipt = root / RECEIPT_REL
    receipt_exists = receipt.exists()
    receipt_files: dict[str, str] = {}
    if receipt_exists:
        try:
            raw_receipt = receipt.read_bytes()
        except OSError as exc:
            raise ValueError("invalid receipt") from exc
        receipt_data = _validate_receipt_bytes(raw_receipt, manifest, raw_manifest)
        receipt_files = {item["path"]: item["sha256"] for item in receipt_data["files"]}
    classifications: list[dict[str, str]] = []
    for entry in manifest["entries"]:
        destination = _target_path(root, entry["path"])
        previous_digest = receipt_files.get(entry["path"])
        if not receipt_exists:
            state = "DIVERGED" if destination.exists() else "ADD"
        elif previous_digest is None:
            state = "DIVERGED" if destination.exists() else "ADD"
        elif not destination.exists():
            state = "DIVERGED"
        else:
            actual_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if actual_digest != previous_digest:
                state = "DIVERGED"
            else:
                state = "EXACT" if previous_digest == entry["sha256"] else "UPDATE"
        classifications.append({"path": entry["path"], "classification": state})
    return classifications


def _safe_target(root: Path, *, create: bool = False) -> None:
    absolute = root.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("target path may not contain symlink")
    if root.exists() and not root.is_dir():
        raise ValueError("target root must be a directory")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    receipt_dir = root / RECEIPT_REL.parent
    if receipt_dir.is_symlink():
        raise ValueError("receipt directory may not be symlink")
    receipt = root / RECEIPT_REL
    if receipt.is_symlink():
        raise ValueError("receipt may not be symlink")


@dataclass(frozen=True)
class _Snapshot:
    data: bytes | None
    identity: tuple[int, int] | None


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_DIR_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW
_TMPFILE = getattr(os, "O_TMPFILE", 0)
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_AT_SYMLINK_FOLLOW = 0x400
_RENAMEAT2: Any
_LINKAT: Any
try:
    _LIBC = ctypes.CDLL(None, use_errno=True)
    _RENAMEAT2 = _LIBC.renameat2
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int
    _LINKAT = _LIBC.linkat
    _LINKAT.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
    ]
    _LINKAT.restype = ctypes.c_int
except (AttributeError, OSError):
    _RENAMEAT2 = None
    _LINKAT = None
_SECURE_DIRFD_SUPPORTED = bool(
    _NOFOLLOW
    and _DIRECTORY
    and _TMPFILE
    and _RENAMEAT2 is not None
    and _LINKAT is not None
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.rmdir in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
)


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    if _RENAMEAT2 is None:
        raise ValueError("secure no-replace rename is unavailable")
    result = _RENAMEAT2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _link_fd_noreplace(parent_fd: int, source_fd: int, target: str) -> None:
    if _LINKAT is None:
        raise ValueError("secure fd publication is unavailable")
    source = os.fsencode(f"/proc/self/fd/{source_fd}")
    result = _LINKAT(
        _AT_FDCWD,
        source,
        parent_fd,
        os.fsencode(target),
        _AT_SYMLINK_FOLLOW,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _assert_protected_parent(value: os.stat_result) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError("unsafe target directory")
    writable_by_others = value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    trusted_sticky_owner = value.st_mode & stat.S_ISVTX and value.st_uid in {
        0,
        os.geteuid(),
    }
    if writable_by_others and not trusted_sticky_owner:
        raise ValueError("target ancestor is writable by untrusted principals")
    if value.st_uid not in {0, os.geteuid()}:
        raise ValueError("target ancestor is owned by an untrusted principal")


def _assert_trusted_managed_dir(value: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ValueError("target is writable by untrusted principals")


class _SecureTree:
    def __init__(self, root: Path, *, create: bool) -> None:
        if not _SECURE_DIRFD_SUPPORTED:
            raise RuntimeError("secure Skills mutation requires dirfd and O_NOFOLLOW support")
        absolute = root.absolute()
        self._fds: list[int] = []
        self._relative: dict[tuple[str, ...], tuple[int, tuple[int, int]]] = {}
        self._absolute: list[tuple[str, tuple[int, int]]] = []
        self._created: list[tuple[int, str, tuple[int, int]]] = []
        anchor_fd = os.open(absolute.anchor, _DIR_FLAGS)
        self._fds.append(anchor_fd)
        self._anchor_fd = anchor_fd
        current = anchor_fd
        for part in absolute.parts[1:]:
            _assert_protected_parent(os.fstat(current))
            child = self._open_dir(current, part, create=create)
            self._fds.append(child)
            self._absolute.append((part, _identity(os.fstat(child))))
            current = child
        self.root_fd = current
        _assert_trusted_managed_dir(os.fstat(self.root_fd))
        try:
            fcntl.flock(self.root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise RuntimeError("another Skills mutation owns the target root") from exc
        self._relative[()] = (self.root_fd, _identity(os.fstat(self.root_fd)))

    def _open_dir(self, parent_fd: int, name: str, *, create: bool) -> int:
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            created = False
            try:
                os.mkdir(name, mode=0o755, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                pass
            child = -1
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
                if created:
                    os.fsync(parent_fd)
                    self._created.append((parent_fd, name, _identity(os.fstat(child))))
                return child
            except BaseException:
                if child >= 0:
                    os.close(child)
                if created:
                    try:
                        os.rmdir(name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except BaseException as cleanup_exc:
                        raise RuntimeError(
                            "failed to roll back created Skills directory"
                        ) from cleanup_exc
                raise
        except OSError as exc:
            raise ValueError("target path changed during mutation") from exc

    def parent(self, relative: str | Path, *, create: bool) -> tuple[int, str]:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError("unsafe target path")
        current = self.root_fd
        parts: tuple[str, ...] = ()
        for part in path.parts[:-1]:
            parts = (*parts, part)
            known = self._relative.get(parts)
            if known is None:
                child = self._open_dir(current, part, create=create)
                _assert_trusted_managed_dir(os.fstat(child))
                self._fds.append(child)
                known = (child, _identity(os.fstat(child)))
                self._relative[parts] = known
            current = known[0]
        _assert_trusted_managed_dir(os.fstat(current))
        return current, path.name

    def verify(self) -> None:
        current = os.dup(self._anchor_fd)
        try:
            for part, expected in self._absolute:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
                os.close(current)
                current = child
                if _identity(os.fstat(current)) != expected:
                    raise ValueError("target path changed during mutation")
            _assert_trusted_managed_dir(os.fstat(current))
        except OSError as exc:
            raise ValueError("target path changed during mutation") from exc
        finally:
            os.close(current)
        for parts, (_, expected) in self._relative.items():
            if not parts:
                continue
            current = os.dup(self.root_fd)
            try:
                for part in parts:
                    child = os.open(part, _DIR_FLAGS, dir_fd=current)
                    os.close(current)
                    current = child
                if _identity(os.fstat(current)) != expected:
                    raise ValueError("target path changed during mutation")
                _assert_trusted_managed_dir(os.fstat(current))
            except OSError as exc:
                raise ValueError("target path changed during mutation") from exc
            finally:
                os.close(current)

    def rollback_created_directories(self) -> None:
        for parent_fd, name, expected in reversed(self._created):
            child = -1
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
                if _identity(os.fstat(child)) != expected:
                    raise ValueError("created Skills directory identity changed")
                os.close(child)
                child = -1
                os.rmdir(name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except BaseException as exc:
                if child >= 0:
                    os.close(child)
                raise RuntimeError("failed to roll back created Skills directory") from exc
        self._created.clear()

    def close(self) -> None:
        for fd in reversed(self._fds):
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()


def _snapshot_at(parent_fd: int, name: str) -> _Snapshot:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return _Snapshot(None, None)
    except OSError as exc:
        raise ValueError("unsafe managed file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("unsafe managed file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return _Snapshot(b"".join(chunks), _identity(info))
    finally:
        os.close(fd)


def _same_snapshot(actual: _Snapshot, expected: _Snapshot) -> bool:
    return actual.data == expected.data and actual.identity == expected.identity


def _anonymous_payload_fd(parent_fd: int, data: bytes) -> int:
    try:
        fd = os.open(".", os.O_RDWR | _TMPFILE, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError("secure anonymous Skills staging is unavailable") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("failed to write anonymous Skills payload")
            view = view[written:]
        os.fsync(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _atomic_write_at(parent_fd: int, name: str, data: bytes, expected: _Snapshot) -> _Snapshot:
    if not _same_snapshot(_snapshot_at(parent_fd, name), expected):
        raise ValueError("target path changed during mutation")
    if expected.data is not None:
        raise ValueError("refusing to overwrite managed file")
    fd = -1
    try:
        fd = _anonymous_payload_fd(parent_fd, data)
        installed = _Snapshot(data, _identity(os.fstat(fd)))
        try:
            _link_fd_noreplace(parent_fd, fd, name)
        except FileExistsError as exc:
            raise ValueError("target path changed during mutation") from exc
        try:
            if not _same_snapshot(_snapshot_at(parent_fd, name), installed):
                raise ValueError("target path changed during mutation")
            os.fsync(parent_fd)
        except BaseException:
            try:
                if _same_snapshot(_snapshot_at(parent_fd, name), installed):
                    _unlink_at(parent_fd, name, installed)
            except BaseException as cleanup_exc:
                raise RuntimeError("failed to roll back published Skills payload") from cleanup_exc
            raise
        return installed
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _restore_unlink_failure(
    parent_fd: int,
    name: str,
    quarantine: str,
    expected: _Snapshot,
    backup_fd: int,
) -> None:
    current = _snapshot_at(parent_fd, name)
    if _same_snapshot(current, expected):
        os.fsync(parent_fd)
        return
    moved = _snapshot_at(parent_fd, quarantine)
    if current.data is None and _same_snapshot(moved, expected):
        _rename_noreplace(parent_fd, quarantine, name)
        os.fsync(parent_fd)
        return
    if current.data is None:
        _link_fd_noreplace(parent_fd, backup_fd, name)
        restored = _snapshot_at(parent_fd, name)
        if restored.data != expected.data:
            raise RuntimeError("restored Skills payload does not match deleted bytes")
        os.fsync(parent_fd)
        return
    raise RuntimeError("refusing to overwrite changed Skills payload during rollback")


def _unlink_at(parent_fd: int, name: str, expected: _Snapshot) -> None:
    if expected.data is None or not _same_snapshot(_snapshot_at(parent_fd, name), expected):
        raise ValueError("target path changed during mutation")
    backup_fd = _anonymous_payload_fd(parent_fd, expected.data)
    quarantine = f".{name}.thinkroom-delete-{secrets.token_hex(8)}"
    try:
        _rename_noreplace(parent_fd, name, quarantine)
        moved = _snapshot_at(parent_fd, quarantine)
        if not _same_snapshot(moved, expected):
            try:
                if _snapshot_at(parent_fd, name).data is not None or moved.data is None:
                    raise RuntimeError("cannot restore changed Skills filename")
                _rename_noreplace(parent_fd, quarantine, name)
                os.fsync(parent_fd)
            except BaseException as cleanup_exc:
                raise RuntimeError("failed to restore changed Skills filename") from cleanup_exc
            raise ValueError("target path changed during mutation")
        try:
            os.unlink(quarantine, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except BaseException:
            try:
                _restore_unlink_failure(parent_fd, name, quarantine, expected, backup_fd)
            except BaseException as cleanup_exc:
                raise RuntimeError("failed to roll back deleted Skills payload") from cleanup_exc
            raise
    finally:
        os.close(backup_fd)


def _restore_at(parent_fd: int, name: str, original: _Snapshot) -> None:
    current = _snapshot_at(parent_fd, name)
    if original.data is None:
        if current.data is not None:
            _unlink_at(parent_fd, name, current)
        return
    if current.data is not None:
        if _same_snapshot(current, original):
            return
        raise ValueError("refusing to overwrite changed managed file")
    _atomic_write_at(parent_fd, name, original.data, current)


def _replace_at(parent_fd: int, name: str, data: bytes, expected: _Snapshot) -> _Snapshot:
    if expected.data is None:
        raise ValueError("managed file is missing")
    _unlink_at(parent_fd, name, expected)
    try:
        return _atomic_write_at(parent_fd, name, data, _Snapshot(None, None))
    except BaseException:
        try:
            _restore_at(parent_fd, name, expected)
        except BaseException as cleanup_exc:
            raise RuntimeError("failed to roll back replaced Skills payload") from cleanup_exc
        raise


def _rollback_publish_at(
    parent_fd: int,
    name: str,
    installed: _Snapshot,
    original: _Snapshot,
) -> None:
    if not _same_snapshot(_snapshot_at(parent_fd, name), installed):
        raise ValueError("refusing to overwrite changed managed file during rollback")
    _unlink_at(parent_fd, name, installed)
    if original.data is None:
        return
    _restore_at(parent_fd, name, original)


def _validate_receipt_bytes(
    raw_receipt: bytes, manifest: dict[str, Any], raw_manifest: bytes
) -> dict[str, Any]:
    try:
        data = json.loads(raw_receipt)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid receipt") from exc
    if not isinstance(data, dict):
        raise ValueError("invalid receipt")
    if set(data) != {"receipt_version", "bundle_version", "manifest_sha256", "files"}:
        raise ValueError("invalid receipt")
    expected = {e["path"]: e["sha256"] for e in manifest["entries"]}
    files = data.get("files")
    if not isinstance(files, list) or not all(
        isinstance(item, dict) and set(item) == {"path", "sha256"} for item in files
    ):
        raise ValueError("invalid receipt")
    receipt_paths = [item.get("path") for item in files]
    actual = {item.get("path"): item.get("sha256") for item in files}
    receipt_paths_valid = all(isinstance(path, str) for path in receipt_paths)
    manifest_sha256 = data.get("manifest_sha256")
    current_manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
    previous = (
        _KNOWN_PREVIOUS_RECEIPTS.get(manifest_sha256) if isinstance(manifest_sha256, str) else None
    )
    authority_matches = (
        data.get("bundle_version") == manifest.get("bundle_version")
        and manifest_sha256 == current_manifest_sha256
        and actual == expected
    ) or (
        previous is not None
        and data.get("bundle_version") == previous["bundle_version"]
        and actual == previous["files"]
    )
    if (
        not receipt_paths_valid
        or len(receipt_paths) != len(set(receipt_paths))
        or data.get("receipt_version") != "1"
        or not authority_matches
    ):
        raise ValueError("invalid receipt")
    return data


def _read_receipt(root: Path, manifest: dict[str, Any], raw_manifest: bytes) -> dict[str, Any]:
    _safe_target(root)
    receipt = root / RECEIPT_REL
    try:
        raw_receipt = receipt.read_bytes()
    except OSError as exc:
        raise ValueError("invalid receipt") from exc
    return _validate_receipt_bytes(raw_receipt, manifest, raw_manifest)


def plan(target: str | Path) -> list[dict[str, str]]:
    manifest, raw, _ = _manifest()
    root = Path(target)
    return _preflight_install(root, manifest, raw)


def install(target: str | Path) -> list[dict[str, str]]:
    manifest, raw, payloads = _manifest()
    root = Path(target)
    preflight = _preflight_install(root, manifest, raw)
    if any(item["classification"] == "DIVERGED" for item in preflight):
        raise ValueError("DIVERGED managed or unmanaged target")
    tree = _SecureTree(root, create=True)
    records: dict[str, tuple[int, str, _Snapshot]] = {}
    accepted: dict[str, _Snapshot] = {}
    applied: list[tuple[str, _Snapshot, _Snapshot]] = []
    receipt_rel = RECEIPT_REL.as_posix()
    managed = [*[e["path"] for e in manifest["entries"]], receipt_rel]
    try:
        for relative in managed:
            try:
                parent_fd, name = tree.parent(relative, create=False)
            except FileNotFoundError:
                accepted[relative] = _Snapshot(None, None)
            else:
                accepted[relative] = _snapshot_at(parent_fd, name)
        receipt_snapshot = accepted[receipt_rel]
        receipt_data: dict[str, Any] | None = None
        if receipt_snapshot.data is not None:
            receipt_data = _validate_receipt_bytes(receipt_snapshot.data, manifest, raw)
        receipt_exists = receipt_snapshot.data is not None
        receipt_files = (
            {item["path"]: item["sha256"] for item in receipt_data["files"]}
            if receipt_data is not None
            else {}
        )
        classifications = []
        for entry in manifest["entries"]:
            snapshot = accepted[entry["path"]]
            previous_digest = receipt_files.get(entry["path"])
            if not receipt_exists:
                state = "DIVERGED" if snapshot.data is not None else "ADD"
            elif previous_digest is None:
                state = "DIVERGED" if snapshot.data is not None else "ADD"
            elif snapshot.data is None:
                state = "DIVERGED"
            else:
                actual_digest = hashlib.sha256(snapshot.data).hexdigest()
                if actual_digest != previous_digest:
                    state = "DIVERGED"
                else:
                    state = "EXACT" if previous_digest == entry["sha256"] else "UPDATE"
            classifications.append({"path": entry["path"], "classification": state})
        if classifications != preflight or any(
            item["classification"] == "DIVERGED" for item in classifications
        ):
            raise ValueError("DIVERGED managed or unmanaged target")
        for relative in managed:
            parent_fd, name = tree.parent(relative, create=True)
            snapshot = _snapshot_at(parent_fd, name)
            if not _same_snapshot(snapshot, accepted[relative]):
                raise ValueError("target path changed during mutation")
            records[relative] = (parent_fd, name, snapshot)
        receipt_bytes = (
            json.dumps(
                {
                    "receipt_version": "1",
                    "bundle_version": manifest["bundle_version"],
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "files": [
                        {"path": entry["path"], "sha256": entry["sha256"]}
                        for entry in manifest["entries"]
                    ],
                },
                indent=2,
            )
            + "\n"
        ).encode()
        staged = [(entry["path"], payloads[entry["path"]]) for entry in manifest["entries"]]
        for relative, data in [*staged, (receipt_rel, receipt_bytes)]:
            parent_fd, name, snapshot = records[relative]
            if snapshot.data == data:
                continue
            installed = (
                _atomic_write_at(parent_fd, name, data, snapshot)
                if snapshot.data is None
                else _replace_at(parent_fd, name, data, snapshot)
            )
            applied.append((relative, installed, snapshot))
        tree.verify()
        return classifications
    except BaseException:
        rollback_error: BaseException | None = None
        for relative, installed, original in reversed(applied):
            parent_fd, name, _ = records[relative]
            try:
                _rollback_publish_at(parent_fd, name, installed, original)
            except BaseException as exc:
                if rollback_error is None:
                    rollback_error = exc
        try:
            tree.rollback_created_directories()
        except BaseException as exc:
            if rollback_error is None:
                rollback_error = exc
        if rollback_error is not None:
            raise RuntimeError("failed to roll back Skills install") from rollback_error
        raise
    finally:
        tree.close()


def status(target: str | Path) -> list[dict[str, str]]:
    manifest, raw, _ = _manifest()
    root = Path(target)
    return _preflight_install(root, manifest, raw)


def uninstall(target: str | Path) -> None:
    root = Path(target)
    _safe_target(root)
    if not root.exists():
        return
    manifest, raw, _ = _manifest()
    tree = _SecureTree(root, create=False)
    receipt_rel = RECEIPT_REL.as_posix()
    records: dict[str, tuple[int, str, _Snapshot]] = {}
    deleted: list[str] = []
    try:
        receipt_parent, receipt_name = tree.parent(receipt_rel, create=False)
        receipt_snapshot = _snapshot_at(receipt_parent, receipt_name)
        if receipt_snapshot.data is None:
            return
        records[receipt_rel] = (receipt_parent, receipt_name, receipt_snapshot)
        data = _validate_receipt_bytes(receipt_snapshot.data, manifest, raw)
        for item in data["files"]:
            relative = item["path"]
            parent_fd, name = tree.parent(relative, create=False)
            snapshot = _snapshot_at(parent_fd, name)
            if snapshot.data is None or hashlib.sha256(snapshot.data).hexdigest() != item["sha256"]:
                raise ValueError("DIVERGED")
            records[relative] = (parent_fd, name, snapshot)
        for item in data["files"]:
            relative = item["path"]
            parent_fd, name, snapshot = records[relative]
            _unlink_at(parent_fd, name, snapshot)
            deleted.append(relative)
        _unlink_at(receipt_parent, receipt_name, receipt_snapshot)
        deleted.append(receipt_rel)
        tree.verify()
    except BaseException:
        rollback_error: BaseException | None = None
        for relative in reversed(deleted):
            parent_fd, name, snapshot = records[relative]
            try:
                _restore_at(parent_fd, name, snapshot)
            except BaseException as exc:
                if rollback_error is None:
                    rollback_error = exc
        if rollback_error is not None:
            raise RuntimeError("failed to roll back Skills uninstall") from rollback_error
        raise
    finally:
        tree.close()
