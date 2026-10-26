"""Storage tests use synthetic data and OS temporary directories, never user files."""

import ctypes
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from jobwolf import storage
from jobwolf.storage import (
    MAX_PAYLOAD_BYTES,
    ConflictError,
    PayloadCipher,
    SQLiteStore,
    StorageError,
    WindowsDPAPICipher,
    default_data_dir,
)

PRIVATE_TEXT = b"JOBWOLF_SYNTHETIC_PRIVATE_93e8c4_no_real_person"
PAYLOAD = b'{"schema_version":1,"notes":"' + PRIVATE_TEXT + b'"}'


def mock_cipher() -> Mock:
    """An in-memory opaque-token stand-in, not encryption or a runtime option."""
    payloads: dict[bytes, bytes] = {}

    def protect(data: bytes) -> bytes:
        token = b"test-only-opaque-token:" + len(payloads).to_bytes(8, "big")
        payloads[token] = data
        return token

    cipher = Mock(spec=PayloadCipher)
    cipher.protect.side_effect = protect
    cipher.unprotect.side_effect = payloads.__getitem__
    return cipher


class TemporaryStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="jobwolf-storage-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        repository = Path(__file__).resolve().parents[1]
        self.assertFalse(self.root.is_relative_to(repository))
        self.path = self.root / "nested directory" / "workspace #1.sqlite"

    def disk_snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*") if path.is_file()
        }

    def assert_private_text_absent(self) -> None:
        for name, content in self.disk_snapshot().items():
            with self.subTest(file=name):
                self.assertNotIn(PRIVATE_TEXT, content)
                self.assertNotIn(PRIVATE_TEXT.decode().encode("utf-16-le"), content)

    def assert_safe_error(self, operation) -> StorageError:
        with self.assertRaises(StorageError) as caught:
            operation()
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(PRIVATE_TEXT.decode(), rendered)
        self.assertIsNone(caught.exception.__cause__)
        return caught.exception

    def assert_rejected_without_changes(self, store: SQLiteStore, revision: int) -> None:
        before = self.disk_snapshot()
        self.assert_safe_error(store.load)
        self.assert_safe_error(lambda: store.save(b"replacement", revision))
        self.assertEqual(self.disk_snapshot(), before)

    def observe_encrypted_transaction(self, store: SQLiteStore, revision: int) -> int:
        """Inspect the live rollback journal, not just files after it is deleted."""
        connect = sqlite3.connect
        test = self
        journals_seen = []

        class ObservingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith("UPDATE workspace"):
                    journals_seen.extend(test.root.rglob("*-journal"))
                    test.assert_private_text_absent()
                return result

        def observing_connect(*args, **kwargs):
            return connect(*args, **kwargs, factory=ObservingConnection)

        with patch.object(storage.sqlite3, "connect", side_effect=observing_connect):
            new_revision = store.save(PAYLOAD + b"\n", revision)
        self.assertTrue(journals_seen, "The test must observe an active rollback journal")
        self.assert_private_text_absent()
        return new_revision


class SQLiteStoreTests(TemporaryStorageTest):
    def setUp(self) -> None:
        super().setUp()
        self.cipher = mock_cipher()
        self.store = SQLiteStore(self.path, self.cipher)

    def execute_sql(self, sql: str, parameters=()) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(sql, parameters)

    def test_initial_state_schema_and_no_extra_files(self) -> None:
        self.assertEqual(self.store.load(), (0, None))
        self.cipher.protect.assert_not_called()
        self.cipher.unprotect.assert_not_called()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone(), (1,))
            self.assertEqual(
                connection.execute("SELECT id, revision, payload FROM workspace").fetchall(),
                [(1, 0, None)],
            )
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_save_load_and_encrypt_every_revision(self) -> None:
        self.assertEqual(self.store.save(PAYLOAD, 0), 1)
        self.assertEqual(self.store.load(), (1, PAYLOAD))
        self.assertEqual(self.store.save(PAYLOAD, 1), 2)
        self.assertEqual(self.store.load(), (2, PAYLOAD))
        self.assertEqual(self.cipher.protect.call_count, 2)
        with closing(sqlite3.connect(self.path)) as connection:
            revision, ciphertext = connection.execute(
                "SELECT revision, payload FROM workspace"
            ).fetchone()
        self.assertEqual(revision, 2)

        self.assertNotEqual(ciphertext, PAYLOAD)
        self.assertIsInstance(ciphertext, bytes)
        self.cipher.unprotect.assert_called_with(ciphertext)
        self.assert_private_text_absent()

    def test_live_journal_contains_no_private_text(self) -> None:
        self.store.save(PAYLOAD, 0)
        self.assertEqual(self.observe_encrypted_transaction(self.store, 1), 2)
        self.assertEqual(self.store.load(), (2, PAYLOAD + b"\n"))

    def test_persistence_across_store_instances(self) -> None:
        self.store.save(PAYLOAD, 0)
        reopened = SQLiteStore(self.path, self.cipher)
        self.assertEqual(reopened.load(), (1, PAYLOAD))
        self.assertEqual(reopened.save(b"updated", 1), 2)
        self.assertEqual(self.store.load(), (2, b"updated"))

    def test_stale_revision_is_a_conflict_without_changes(self) -> None:
        self.store.save(PAYLOAD, 0)
        before = self.disk_snapshot()
        with self.assertRaises(ConflictError):
            self.store.save(b"stale update", 0)
        self.assertTrue(issubclass(ConflictError, StorageError))
        self.assertEqual(self.disk_snapshot(), before)
        self.assertEqual(self.store.load(), (1, PAYLOAD))
        self.cipher.protect.assert_called_once_with(PAYLOAD)

    def test_concurrent_threads_and_instances_have_one_cas_winner(self) -> None:
        count = 6
        stores = [self.store, SQLiteStore(self.path, self.cipher)]
        barrier = threading.Barrier(count, timeout=10)

        def save(index: int):
            candidate = PAYLOAD + str(index).encode()
            barrier.wait()
            try:
                revision = stores[index % len(stores)].save(candidate, 0)
                return revision, candidate
            except ConflictError:
                return None

        with ThreadPoolExecutor(max_workers=count) as executor:
            futures = [executor.submit(save, index) for index in range(count)]
            results = [future.result(timeout=15) for future in futures]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0][0], 1)
        self.assertEqual(self.store.load(), winners[0])
        self.assertEqual(results.count(None), count - 1)
        self.cipher.protect.assert_called_once()

    def test_empty_binary_and_maximum_size_payloads_round_trip(self) -> None:
        for revision, payload in enumerate((b"", b"\x00\xff\x00\xfe", b"a" * MAX_PAYLOAD_BYTES)):
            with self.subTest(size=len(payload)):
                self.assertEqual(self.store.save(payload, revision), revision + 1)
                self.assertEqual(self.store.load(), (revision + 1, payload))

    def test_oversized_or_nonbytes_payload_rejected_before_encryption(self) -> None:
        for payload in (b"a" * (MAX_PAYLOAD_BYTES + 1), "text", None, bytearray(b"text")):
            with self.subTest(type=type(payload).__name__):
                self.assert_safe_error(lambda payload=payload: self.store.save(cast(Any, payload), 0))
        self.assertEqual(self.store.load(), (0, None))
        self.cipher.protect.assert_not_called()

    def test_invalid_expected_revisions_are_rejected(self) -> None:
        for revision in (-1, True, 0.0, "0", None, 1 << 63):
            with self.subTest(revision=revision):
                self.assert_safe_error(lambda revision=revision: self.store.save(PAYLOAD, cast(Any, revision)))
        self.assertEqual(self.store.load(), (0, None))
        self.cipher.protect.assert_not_called()

    def test_revision_overflow_does_not_overwrite_data(self) -> None:
        self.store.save(PAYLOAD, 0)
        maximum = (1 << 63) - 1
        self.execute_sql("UPDATE workspace SET revision = ?", (maximum,))
        before = self.disk_snapshot()
        self.assert_safe_error(lambda: self.store.save(b"replacement", maximum))
        self.assertEqual(self.store.load(), (maximum, PAYLOAD))
        self.assertEqual(self.disk_snapshot(), before)

    def test_wrong_cipher_cannot_load_or_overwrite_existing_data(self) -> None:
        self.store.save(PAYLOAD, 0)
        wrong = mock_cipher()
        wrong.unprotect.side_effect = ValueError(PRIVATE_TEXT.decode())
        other = SQLiteStore(self.path, wrong)
        error = self.assert_safe_error(other.load)
        self.assertIsNone(error.__context__)
        self.assert_rejected_without_changes(other, 1)
        wrong.protect.assert_not_called()
        self.assertEqual(self.store.load(), (1, PAYLOAD))

    def test_corrupted_ciphertext_cannot_be_overwritten(self) -> None:
        self.store.save(PAYLOAD, 0)
        self.execute_sql("UPDATE workspace SET payload = ?", (b"not-a-valid-ciphertext",))
        self.assert_rejected_without_changes(self.store, 1)
        reopened = SQLiteStore(self.path, self.cipher)
        self.assert_rejected_without_changes(reopened, 1)
        self.cipher.protect.assert_called_once()

    def test_cipher_failure_is_sanitized_and_preserves_previous_payload(self) -> None:
        self.store.save(PAYLOAD, 0)
        before = self.disk_snapshot()
        self.cipher.protect.side_effect = ValueError(PRIVATE_TEXT.decode())
        error = self.assert_safe_error(lambda: self.store.save(PAYLOAD, 1))
        self.assertIsNone(error.__context__)
        self.assertEqual(self.disk_snapshot(), before)
        self.assertEqual(self.store.load(), (1, PAYLOAD))

    def test_invalid_protect_results_are_never_stored(self) -> None:
        self.cipher.protect.side_effect = None
        for result in (None, PRIVATE_TEXT.decode(), b"", b"a" * (MAX_PAYLOAD_BYTES + 65537)):
            with self.subTest(type=type(result).__name__):
                self.cipher.protect.return_value = result
                before = self.disk_snapshot()
                self.assert_safe_error(lambda: self.store.save(PAYLOAD, 0))
                self.assertEqual(self.disk_snapshot(), before)
                self.assertEqual(self.store.load(), (0, None))

    def test_invalid_unprotect_results_are_not_treated_as_an_empty_workspace(self) -> None:
        self.store.save(PAYLOAD, 0)
        self.cipher.unprotect.side_effect = None
        for result in (None, PRIVATE_TEXT.decode(), b"a" * (MAX_PAYLOAD_BYTES + 1)):
            with self.subTest(type=type(result).__name__):
                self.cipher.unprotect.return_value = result
                self.assert_rejected_without_changes(self.store, 1)
        self.cipher.protect.assert_called_once()

    def test_failure_after_sql_update_rolls_back_revision_and_ciphertext(self) -> None:
        self.store.save(PAYLOAD, 0)
        before = self.disk_snapshot()
        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                result = super().execute(sql, parameters)
                if sql.startswith("UPDATE workspace"):
                    raise sqlite3.OperationalError(PRIVATE_TEXT.decode())
                return result

        def failing_connect(*args, **kwargs):
            return connect(*args, **kwargs, factory=FailingConnection)

        with patch.object(storage.sqlite3, "connect", side_effect=failing_connect):
            self.assert_safe_error(lambda: self.store.save(b"replacement", 1))
        self.assertEqual(self.store.load(), (1, PAYLOAD))
        self.assertEqual(self.disk_snapshot(), before)

    def test_missing_database_after_open_is_not_recreated(self) -> None:
        self.store.save(PAYLOAD, 0)
        backup = self.root / "moved.sqlite"
        self.path.rename(backup)
        self.assert_rejected_without_changes(self.store, 1)
        self.assertFalse(self.path.exists())
        self.assertEqual(SQLiteStore(backup, self.cipher).load(), (1, PAYLOAD))

    def test_future_version_is_rejected_by_constructor_load_and_save(self) -> None:
        self.store.save(PAYLOAD, 0)
        self.execute_sql("PRAGMA user_version=2")
        self.assert_rejected_without_changes(self.store, 1)
        before = self.disk_snapshot()
        error = self.assert_safe_error(lambda: SQLiteStore(self.path, self.cipher))
        self.assertIn("newer", str(error))
        self.assertEqual(self.disk_snapshot(), before)

    def test_missing_record_is_never_reinitialized(self) -> None:
        self.store.save(PAYLOAD, 0)
        self.execute_sql("DELETE FROM workspace")
        self.assert_rejected_without_changes(self.store, 1)
        before = self.disk_snapshot()
        self.assert_safe_error(lambda: SQLiteStore(self.path, self.cipher))
        self.assertEqual(self.disk_snapshot(), before)

    def test_missing_schema_is_never_recreated(self) -> None:
        self.execute_sql("DROP TABLE workspace")
        self.assert_rejected_without_changes(self.store, 0)
        before = self.disk_snapshot()
        self.assert_safe_error(lambda: SQLiteStore(self.path, self.cipher))
        self.assertEqual(self.disk_snapshot(), before)

    def test_unexpected_trigger_is_rejected_before_writing(self) -> None:
        self.execute_sql(
            "CREATE TRIGGER unexpected AFTER UPDATE ON workspace "
            "BEGIN DELETE FROM workspace; END"
        )
        self.assert_rejected_without_changes(self.store, 0)

    def test_truncated_database_is_not_reset(self) -> None:
        self.store.save(PAYLOAD, 0)
        self.path.write_bytes(b"")
        self.assert_rejected_without_changes(self.store, 1)
        self.assert_safe_error(lambda: SQLiteStore(self.path, self.cipher))
        self.assertEqual(self.path.read_bytes(), b"")

    def test_unreadable_database_errors_are_sanitized(self) -> None:
        self.store.save(PAYLOAD, 0)
        before = self.disk_snapshot()
        with patch.object(storage.sqlite3, "connect", side_effect=sqlite3.OperationalError(PRIVATE_TEXT.decode())):
            self.assert_safe_error(self.store.load)
            self.assert_safe_error(lambda: self.store.save(b"replacement", 1))
            self.assert_safe_error(lambda: SQLiteStore(self.path, self.cipher))
        self.assertEqual(self.disk_snapshot(), before)
        self.assertEqual(self.store.load(), (1, PAYLOAD))

    def test_connection_pragmas_are_conservative(self) -> None:
        with self.store._connection() as connection:
            for pragma, expected in (
                ("secure_delete", 1), ("synchronous", 2), ("journal_mode", "delete"),
                ("temp_store", 2), ("trusted_schema", 0),
            ):
                with self.subTest(pragma=pragma):
                    self.assertEqual(connection.execute("PRAGMA " + pragma).fetchone(), (expected,))

    def test_unexpected_wal_mode_is_rejected_not_silently_changed(self) -> None:
        self.execute_sql("PRAGMA journal_mode=WAL")
        self.assert_rejected_without_changes(self.store, 0)
        self.assert_safe_error(lambda: SQLiteStore(self.path, self.cipher))
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))


class InitializationAndPlatformTests(TemporaryStorageTest):
    def test_existing_empty_or_non_sqlite_files_are_not_initialized(self) -> None:
        for content in (b"", b"not a database: " + PRIVATE_TEXT):
            with self.subTest(size=len(content)):
                path = self.root / "invalid.sqlite"
                path.write_bytes(content)
                before = self.disk_snapshot()
                self.assert_safe_error(lambda path=path: SQLiteStore(path, mock_cipher()))
                self.assertEqual(self.disk_snapshot(), before)

    def test_unversioned_sqlite_database_is_not_adopted(self) -> None:
        path = self.root / "unrelated.sqlite"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        before = self.disk_snapshot()
        self.assert_safe_error(lambda: SQLiteStore(path, mock_cipher()))
        self.assertEqual(self.disk_snapshot(), before)

    def test_directory_cannot_be_used_as_a_database(self) -> None:
        self.path.mkdir(parents=True)
        self.assert_safe_error(lambda: SQLiteStore(self.path, mock_cipher()))
        self.assertTrue(self.path.is_dir())

    def test_orphan_recovery_files_are_preserved(self) -> None:
        self.path.parent.mkdir(parents=True)
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                sidecar = Path(str(self.path) + suffix)
                sidecar.write_bytes(b"synthetic recovery data")
                before = self.disk_snapshot()
                self.assert_safe_error(lambda: SQLiteStore(self.path, mock_cipher()))
                self.assertEqual(self.disk_snapshot(), before)
                self.assertFalse(self.path.exists())
                sidecar.unlink()

    def test_off_windows_defaults_fail_before_creating_files(self) -> None:
        with patch.object(storage.sys, "platform", "linux"):
            self.assert_safe_error(WindowsDPAPICipher)
            self.assert_safe_error(default_data_dir)
            self.assert_safe_error(lambda: SQLiteStore(self.path))
        self.assertFalse(self.path.parent.exists())

    def test_default_cipher_failure_has_no_plaintext_fallback(self) -> None:
        with patch.object(storage.sys, "platform", "win32"), patch.object(
            storage.ctypes, "WinDLL", create=True, side_effect=OSError(PRIVATE_TEXT.decode())
        ):
            self.assert_safe_error(lambda: SQLiteStore(self.path))
        self.assertFalse(self.path.parent.exists())

    def test_default_directory_uses_localappdata_without_creating_it(self) -> None:
        with patch.object(storage.sys, "platform", "win32"), patch.dict(
            os.environ, {"LOCALAPPDATA": str(self.root)}
        ):
            self.assertEqual(default_data_dir(), self.root / "JobWolfBench")
        self.assertFalse((self.root / "JobWolfBench").exists())

    def test_default_directory_rejects_missing_empty_or_relative_environment(self) -> None:
        for value in (None, "", "relative-directory"):
            with self.subTest(value=value), patch.object(storage.sys, "platform", "win32"), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = value
                self.assert_safe_error(default_data_dir)

    def test_public_import_does_not_require_windows_ctypes_symbols(self) -> None:
        # Run a fresh import, also simulating missing Windows-only ctypes symbols
        # when the test host itself is Windows. Real non-Windows runs use this too.
        code = """
import ctypes
import sys
sys.platform = 'linux'
if hasattr(ctypes, 'WinDLL'):
    del ctypes.WinDLL
from jobwolf.storage import (
    StorageError, ConflictError, PayloadCipher, WindowsDPAPICipher,
    default_data_dir, SQLiteStore,
)
assert issubclass(ConflictError, StorageError)
for operation in (WindowsDPAPICipher, default_data_dir):
    try:
        operation()
    except StorageError:
        pass
    else:
        raise AssertionError('A Windows-only default was accepted off Windows')
"""
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], capture_output=True, text=True,
            timeout=15, check=False, env={**os.environ, "PYTHONPATH": str(source)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class DPAPIBindingTests(TemporaryStorageTest):
    def make_native_stub(self, result: bytes, *, success=True):
        # These native function slots are replaced by mocks in this fixture.
        cipher = cast(Any, object.__new__(WindowsDPAPICipher))
        output_buffer = ctypes.create_string_buffer(result, max(1, len(result)))
        calls = []

        def operation(source, description, entropy, reserved, prompt, flags, output):
            blob = ctypes.cast(source, ctypes.POINTER(storage._DataBlob)).contents
            calls.append(ctypes.string_at(blob.pbData, blob.cbData))
            self.assertIsNone(description)
            self.assertIsNone(entropy)
            self.assertIsNone(reserved)
            self.assertIsNone(prompt)
            self.assertEqual(flags, 1)  # UI_FORBIDDEN only; never LOCAL_MACHINE.
            destination = ctypes.cast(output, ctypes.POINTER(storage._DataBlob)).contents
            destination.cbData = len(result)
            destination.pbData = ctypes.cast(output_buffer, ctypes.POINTER(storage.wintypes.BYTE))
            return success

        def free(handle):
            self.assertTrue(handle.value)
            self.assertEqual(output_buffer.raw, b"\x00" * len(output_buffer))

        cipher._protect = Mock(side_effect=operation)
        cipher._unprotect = Mock(side_effect=operation)
        cipher._local_free = Mock(side_effect=free)
        return cipher, calls

    def test_native_protect_uses_binary_blob_and_frees_output(self) -> None:
        cipher, calls = self.make_native_stub(b"opaque-native-output")
        data = PAYLOAD + b"\x00\xff"
        self.assertEqual(cipher.protect(data), b"opaque-native-output")
        self.assertEqual(calls, [data])
        cipher._protect.assert_called_once()
        cipher._unprotect.assert_not_called()
        cipher._local_free.assert_called_once()

    def test_native_unprotect_uses_binary_blob_and_frees_output(self) -> None:
        cipher, calls = self.make_native_stub(PAYLOAD + b"\x00\xff")
        self.assertEqual(cipher.unprotect(b"opaque-input"), PAYLOAD + b"\x00\xff")
        self.assertEqual(calls, [b"opaque-input"])
        cipher._unprotect.assert_called_once()
        cipher._protect.assert_not_called()
        cipher._local_free.assert_called_once()

    def test_native_failure_frees_output_and_reports_only_error_code(self) -> None:
        cipher, _ = self.make_native_stub(PAYLOAD, success=False)
        with patch.object(storage.ctypes, "get_last_error", return_value=5, create=True):
            error = self.assert_safe_error(lambda: cipher.unprotect(b"invalid"))
        self.assertIn("error 5", str(error))
        cipher._local_free.assert_called_once()

    def test_local_free_failure_is_reported(self) -> None:
        cipher, _ = self.make_native_stub(b"opaque-output")
        cipher._local_free.side_effect = None
        cipher._local_free.return_value = 1
        self.assert_safe_error(lambda: cipher.protect(PAYLOAD))
        cipher._local_free.assert_called_once()

    def test_invalid_native_input_never_reaches_windows(self) -> None:
        cipher, _ = self.make_native_stub(b"opaque-output")
        self.assert_safe_error(lambda: cipher.protect(b"a" * (MAX_PAYLOAD_BYTES + 1)))
        self.assert_safe_error(lambda: cipher.protect(PRIVATE_TEXT.decode()))
        self.assert_safe_error(lambda: cipher.unprotect(b""))
        cipher._protect.assert_not_called()
        cipher._unprotect.assert_not_called()
        cipher._local_free.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Real DPAPI integration requires Windows")
class WindowsDPAPIIntegrationTests(TemporaryStorageTest):
    def test_real_dpapi_round_trips_empty_binary_and_maximum_payloads(self) -> None:
        cipher = WindowsDPAPICipher()
        for payload in (b"", PAYLOAD + b"\x00\xff", b"a" * MAX_PAYLOAD_BYTES):
            with self.subTest(size=len(payload)):
                ciphertext = cipher.protect(payload)
                self.assertNotEqual(ciphertext, payload)
                self.assertNotIn(PRIVATE_TEXT, ciphertext)
                self.assertEqual(WindowsDPAPICipher().unprotect(ciphertext), payload)

    def test_real_store_accepts_the_full_payload_size_limit(self) -> None:
        store = SQLiteStore(self.path)
        prefix, suffix = b'{"padding":"', b'"}'
        payload = prefix + b"a" * (MAX_PAYLOAD_BYTES - len(prefix) - len(suffix)) + suffix
        self.assertEqual(store.save(payload, 0), 1)
        self.assertEqual(SQLiteStore(self.path).load(), (1, payload))

    def test_default_store_reopens_with_no_plaintext_in_database_or_live_journal(self) -> None:
        store = SQLiteStore(self.path)
        self.assertEqual(store.load(), (0, None))
        self.assertEqual(store.save(PAYLOAD, 0), 1)
        reopened = SQLiteStore(self.path)
        self.assertEqual(reopened.load(), (1, PAYLOAD))
        self.assertEqual(self.observe_encrypted_transaction(reopened, 1), 2)
        self.assertEqual(store.load(), (2, PAYLOAD + b"\n"))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupted_real_dpapi_payload_is_never_overwritten(self) -> None:
        store = SQLiteStore(self.path)
        store.save(PAYLOAD, 0)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            ciphertext = bytearray(connection.execute("SELECT payload FROM workspace").fetchone()[0])
            ciphertext[len(ciphertext) // 2] ^= 1
            connection.execute("UPDATE workspace SET payload = ?", (bytes(ciphertext),))
        self.assert_rejected_without_changes(store, 1)
        self.assert_rejected_without_changes(SQLiteStore(self.path), 1)

    def test_real_dpapi_rejects_unprotected_input(self) -> None:
        cipher = WindowsDPAPICipher()
        self.assert_safe_error(lambda: cipher.unprotect(PAYLOAD))


if __name__ == "__main__":
    unittest.main()
