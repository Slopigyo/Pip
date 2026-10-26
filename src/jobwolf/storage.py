"""Local workspace storage with user-scoped Windows DPAPI payload encryption.

SQLite headers, schema, revisions, and payload sizes are not encrypted. Database
backups are account/machine-bound, not portable exports. The application owns
JSON validation and any deliberate plaintext export; this module stores opaque
bytes and never falls back to plaintext or creates application key files.
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Protocol

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_CIPHERTEXT_BYTES = MAX_PAYLOAD_BYTES + 64 * 1024
_MAX_REVISION = (1 << 63) - 1
_FORMAT_VERSION = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_SCHEMA_SQL = f"""CREATE TABLE workspace (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    payload BLOB,
    CHECK (
        (revision = 0 AND payload IS NULL)
        OR (revision > 0 AND payload IS NOT NULL
            AND length(payload) BETWEEN 1 AND {_MAX_CIPHERTEXT_BYTES})
    )
) STRICT"""


class StorageError(Exception):
    """A safe-to-display storage failure, without private payload details."""


class ConflictError(StorageError):
    """The workspace changed since the caller's expected revision."""


class PayloadCipher(Protocol):
    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(wintypes.BYTE)),
    ]


class WindowsDPAPICipher:
    """DPAPI for the current Windows user, with interactive prompts disabled."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise StorageError("Windows DPAPI is required for workspace encryption.")
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            blob_pointer = ctypes.POINTER(_DataBlob)
            self._protect = crypt32.CryptProtectData
            self._protect.argtypes = [
                blob_pointer, wintypes.LPCWSTR, blob_pointer, wintypes.LPVOID,
                wintypes.LPVOID, wintypes.DWORD, blob_pointer,
            ]
            self._protect.restype = wintypes.BOOL
            self._unprotect = crypt32.CryptUnprotectData
            self._unprotect.argtypes = [
                blob_pointer, ctypes.POINTER(wintypes.LPWSTR), blob_pointer,
                wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, blob_pointer,
            ]
            self._unprotect.restype = wintypes.BOOL
            self._local_free = kernel32.LocalFree
            self._local_free.argtypes = [wintypes.HLOCAL]
            self._local_free.restype = wintypes.HLOCAL
        except (OSError, AttributeError):
            raise StorageError("Windows data protection is unavailable.") from None

    def protect(self, data: bytes) -> bytes:
        return self._transform(data, encrypt=True)

    def unprotect(self, data: bytes) -> bytes:
        return self._transform(data, encrypt=False)

    def _transform(self, data: bytes, *, encrypt: bool) -> bytes:
        input_limit = MAX_PAYLOAD_BYTES if encrypt else _MAX_CIPHERTEXT_BYTES
        output_limit = _MAX_CIPHERTEXT_BYTES if encrypt else MAX_PAYLOAD_BYTES
        if not isinstance(data, bytes) or len(data) > input_limit:
            raise StorageError("Invalid data protection input or payload size.")
        if not encrypt and not data:
            raise StorageError("The encrypted workspace payload is invalid.")

        buffer = ctypes.create_string_buffer(data, max(1, len(data)))
        source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE)))
        output = _DataBlob()
        operation = self._protect if encrypt else self._unprotect
        try:
            # No machine-scope flag, description, optional entropy, or UI prompt.
            success = operation(
                ctypes.byref(source), None, None, None, None,
                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output),
            )
            if not success:
                error_code = ctypes.get_last_error()
                raise StorageError(f"Windows data protection failed (error {error_code}).")
            if output.cbData > output_limit or (output.cbData and not output.pbData):
                raise StorageError("Windows data protection returned an invalid payload.")
            if encrypt and not output.cbData:
                raise StorageError("Windows data protection returned an empty payload.")
            return ctypes.string_at(output.pbData, output.cbData) if output.cbData else b""
        except (OSError, ValueError, ctypes.ArgumentError):
            raise StorageError("Windows data protection failed.") from None
        finally:
            # Best-effort clearing of native copies; Python bytes are not secure memory.
            ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
            if output.pbData:
                ctypes.memset(output.pbData, 0, output.cbData)
                if self._local_free(ctypes.cast(output.pbData, wintypes.HLOCAL)):
                    raise StorageError("Windows data protection could not release its buffer.") from None


def default_data_dir() -> Path:
    """Return the local Windows data directory without creating it."""
    if sys.platform != "win32":
        raise StorageError("The default workspace location requires Windows.")
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data or "\x00" in local_app_data:
        raise StorageError("LOCALAPPDATA must specify an absolute local data directory.")
    directory = Path(local_app_data)
    if not directory.is_absolute():
        raise StorageError("LOCALAPPDATA must specify an absolute local data directory.")
    return directory / "JobWolfBench"


class SQLiteStore:
    """A single revisioned, encrypted workspace with compare-and-swap writes.

    A missing path is initialized only by the constructor. Existing files must
    already have the exact supported schema; empty/truncated files are not new
    workspaces. Each operation opens and closes its own SQLite connection.
    Cipher injection is for tests/integration, never an automatic fallback.
    """

    def __init__(self, path: Path, cipher: PayloadCipher | None = None) -> None:
        self._cipher = WindowsDPAPICipher() if cipher is None else cipher
        try:
            self.path = Path(path).resolve()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents adopting or resetting an existing file.
            # Orphan sidecars may be the only remaining recovery data.
            if not self.path.exists() and any(
                Path(str(self.path) + suffix).exists()
                for suffix in ("-journal", "-wal", "-shm")
            ):
                raise StorageError("The workspace database is missing but recovery files remain.")
            try:
                with self.path.open("xb"):
                    pass
                created = True
            except FileExistsError:
                created = False
        except (OSError, ValueError, RuntimeError):
            raise StorageError("The workspace database location could not be opened.") from None

        with self._connection() as connection:
            if created:
                connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            if created:
                if connection.execute("PRAGMA user_version").fetchone() != (0,) or (
                    connection.execute("SELECT name FROM sqlite_schema").fetchone() is not None
                ):
                    raise StorageError("The new workspace database is not empty.")
                connection.execute(_SCHEMA_SQL)
                connection.execute(
                    "INSERT INTO workspace (id, revision, payload) VALUES (?, ?, ?)",
                    (1, 0, None),
                )
                connection.execute("PRAGMA user_version=1")
            self._read_record(connection)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            # mode=rw is critical: losing a database must not silently recreate it.
            with closing(sqlite3.connect(
                self.path.as_uri() + "?mode=rw", uri=True, timeout=5.0,
                isolation_level=None,
            )) as connection:
                connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_CIPHERTEXT_BYTES + 1024)
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA temp_store=MEMORY")
                if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                    raise StorageError("The workspace database has an unsupported journal mode.")
                # The inner context commits explicit transactions or rolls them back;
                # closing() also closes the connection on every exit path.
                with connection:
                    yield connection
        except (sqlite3.Error, OSError, ValueError):
            raise StorageError("The workspace database could not be accessed safely.") from None

    def _read_record(self, connection: sqlite3.Connection) -> tuple[int, bytes | None]:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > _FORMAT_VERSION:
            raise StorageError("The workspace database format is newer than this application supports.")
        if version != _FORMAT_VERSION:
            raise StorageError("The workspace database format is unsupported; no reset was attempted.")
        # The schema is part of format v1. Reject unexpected objects, especially
        # triggers/views, rather than repairing an unknown database in place.
        schema = connection.execute("SELECT type, name, sql FROM sqlite_schema").fetchall()
        if schema != [("table", "workspace", _SCHEMA_SQL)]:
            raise StorageError("The workspace database schema is invalid; no reset was attempted.")
        if connection.execute("PRAGMA quick_check(1)").fetchall() != [("ok",)]:
            raise StorageError("The workspace database is corrupt; no reset was attempted.")
        rows = connection.execute("SELECT id, revision, payload FROM workspace LIMIT 2").fetchall()
        if len(rows) != 1:
            raise StorageError("The workspace database record is missing or invalid.")
        identifier, revision, ciphertext = rows[0]
        if identifier != 1 or type(revision) is not int or not 0 <= revision <= _MAX_REVISION:
            raise StorageError("The workspace database revision is invalid.")
        if revision == 0:
            if ciphertext is not None:
                raise StorageError("The initial workspace database record is invalid.")
        elif not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= _MAX_CIPHERTEXT_BYTES:
            raise StorageError("The encrypted workspace payload is invalid.")
        return revision, ciphertext

    def _decrypt(self, ciphertext: bytes) -> bytes:
        try:
            payload = self._cipher.unprotect(ciphertext)
        except Exception:  # noqa: BLE001 -- Cipher exceptions may contain private data.
            payload = None
        # Raise outside the handler so no private adapter exception is retained
        # in the public exception's __context__, even as a suppressed exception.
        if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD_BYTES:
            raise StorageError("The workspace could not be decrypted; stored data was not changed.")
        return payload

    def _encrypt(self, payload: bytes) -> bytes:
        try:
            ciphertext = self._cipher.protect(payload)
        except Exception:  # noqa: BLE001 -- Cipher exceptions may contain private data.
            ciphertext = None
        if not isinstance(ciphertext, bytes) or not 0 < len(ciphertext) <= _MAX_CIPHERTEXT_BYTES:
            raise StorageError("The workspace could not be encrypted; no data was saved.")
        return ciphertext

    def load(self) -> tuple[int, bytes | None]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            revision, ciphertext = self._read_record(connection)
            return revision, None if ciphertext is None else self._decrypt(ciphertext)

    def save(self, payload: bytes, expected_revision: int) -> int:
        if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD_BYTES:
            raise StorageError("The workspace payload must be bytes no larger than 8 MiB.")
        if type(expected_revision) is not int or not 0 <= expected_revision <= _MAX_REVISION:
            raise StorageError("The expected workspace revision is invalid.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            revision, previous_ciphertext = self._read_record(connection)
            if revision != expected_revision:
                raise ConflictError("The workspace changed; reload it before saving again.")
            if revision == _MAX_REVISION:
                raise StorageError("The workspace revision limit has been reached.")
            # Do not let a caller overwrite data it cannot decrypt, even if it
            # knows the revision (e.g. a copied backup or damaged ciphertext).
            if previous_ciphertext is not None:
                self._decrypt(previous_ciphertext)
            ciphertext = self._encrypt(payload)
            new_revision = revision + 1
            updated = connection.execute(
                "UPDATE workspace SET revision = ?, payload = ? WHERE id = ? AND revision = ?",
                (new_revision, ciphertext, 1, expected_revision),
            )
            if updated.rowcount != 1:
                raise ConflictError("The workspace changed; reload it before saving again.")
        return new_revision
