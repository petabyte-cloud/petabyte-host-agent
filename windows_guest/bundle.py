"""bundle.py — streaming, limit-enforcing extraction of a buyer upload into the guest workspace.

Limits are enforced on the bytes actually written (header sizes are not trusted). Everything about a
zip entry name goes through adobe_contract.check_rel_path, plus zip-specific rejections: encrypted
entries, non-stored/deflate methods, symlink entries, case-insensitive (NFC+casefold) collisions,
file-vs-directory collisions. Nested archives are written as plain files, never expanded. After
extraction every written path must still resolve inside the workspace (reparse-point defense).
A single standalone .aep/.prproj is accepted as a one-file workspace."""
import os
import stat
import time
import unicodedata
import zipfile
import zlib

import adobe_contract as C

CHUNK = 1 << 20
_MAGIC = {".aep": (b"RIFX",), ".prproj": (b"\x1f\x8b", b"<?xml")}
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)


class BundleError(ValueError):
    """Upload rejected; str(e) is buyer-safe."""


def extract(src, workspace, project_path, *, limits=None, clock=time.monotonic) -> list:
    """Extract `src` (zip or standalone project file) under `workspace`. Returns the list of
    project-relative forward-slash paths written. Raises BundleError."""
    limits = {**C.LIMITS, **(limits or {})}
    try:
        rel = C.check_rel_path(project_path)
    except C.ContractError as e:
        raise BundleError(str(e))
    os.makedirs(workspace, exist_ok=True)
    root = os.path.realpath(workspace)
    if zipfile.is_zipfile(src):
        try:
            files = _extract_zip(src, root, limits, clock)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError) as e:
            raise BundleError(f"upload is not a valid zip archive ({type(e).__name__})")
    else:
        files = [_copy_standalone(src, root, rel, limits, clock)]
    for f in files:
        full = os.path.join(root, *f.split("/"))
        real = os.path.realpath(full)
        if os.path.commonpath([real, root]) != root or os.path.islink(full) or not os.path.isfile(full):
            raise BundleError("an extracted file resolves outside the workspace")
    if rel not in files:
        raise BundleError("project file not found in the upload")
    return files


def _key(rel):
    return unicodedata.normalize("NFC", rel).casefold()


def _extract_zip(src, root, limits, clock):
    t0, total, files = clock(), 0, []
    seen, dirs = set(), set()              # casefolded keys of files / directories
    with zipfile.ZipFile(src) as zf:
        infos = zf.infolist()
        if len(infos) > limits["max_files"]:
            raise BundleError("upload has too many files")
        for zi in infos:
            name = zi.filename
            is_dir = name.endswith(("/", "\\"))
            name = name.rstrip("/\\") if is_dir else name
            if zi.flag_bits & 0x1:
                raise BundleError("encrypted zip entries are not supported")
            if zi.compress_type not in _METHODS:
                raise BundleError("zip entries must be stored or deflated")
            if stat.S_ISLNK(zi.external_attr >> 16):
                raise BundleError("symbolic links are not allowed in the upload")
            try:
                rel = C.check_rel_path(name)
            except C.ContractError as e:
                raise BundleError(f"{e}: {C._clip(name, 80)!r}")
            key = _key(rel)
            parts = key.split("/")
            for d in ("/".join(parts[:i]) for i in range(1, len(parts))):
                if d in seen:
                    raise BundleError("a path is used both as a file and a directory")
                dirs.add(d)
            if is_dir:
                if key in seen:
                    raise BundleError("a path is used both as a file and a directory")
                dirs.add(key)
                continue
            if key in seen or key in dirs:
                raise BundleError("duplicate or case-colliding paths in the upload")
            seen.add(key)
            dest = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            written = 0
            with zf.open(zi) as f, open(dest, "wb") as out:
                while chunk := f.read(CHUNK):
                    written += len(chunk)
                    total += len(chunk)
                    _check(written, total, t0, limits, clock)
                    out.write(chunk)
            files.append(rel)
    return files


def _check(written, total, t0, limits, clock):
    if written > limits["max_file_bytes"]:
        raise BundleError("a file in the upload is larger than allowed")
    if total > limits["max_expanded_bytes"]:
        raise BundleError("upload expands beyond the allowed size")
    if clock() - t0 > limits["max_extract_s"]:
        raise BundleError("upload took too long to unpack")


def _copy_standalone(src, root, rel, limits, clock):
    ext = os.path.splitext(rel)[1].lower()
    with open(src, "rb") as f:
        head = f.read(8)
        if not any(head.startswith(m) for m in _MAGIC.get(ext, ())):
            raise BundleError("upload is neither a zip archive nor a project file")
        f.seek(0)
        dest = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        t0, written = clock(), 0
        with open(dest, "wb") as out:
            while chunk := f.read(CHUNK):
                written += len(chunk)
                _check(written, written, t0, limits, clock)
                out.write(chunk)
    return rel
