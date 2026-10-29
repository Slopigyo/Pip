"""Synthetic speech setup tests: tiny ZIPs, temporary directories, no downloads.

Run only this module with unittest discovery. No real workspace, model, archive,
GPU, runtime process, or network connection is used.
"""

from __future__ import annotations

import hashlib
import io
import os
import socket
import stat
import tempfile
import unittest
import warnings
import zipfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jobwolf import speech_assets as assets
from jobwolf import speech_setup as setup


def pinned(filename: str, body: bytes, *, runtime: bool = False) -> assets.Asset:
    base = "https://github.com/ggml-org/llama.cpp/releases/download/b11935" if runtime else (
        f"{assets.MODEL_SOURCE}/resolve/{assets.MODEL_COMMIT}"
    )
    return assets.Asset(filename, f"{base}/{filename}", len(body), hashlib.sha256(body).hexdigest())


def tiny_zip(extra=(), *, required=True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if required:
            archive.writestr("llama-server.exe", b"SYNTHETIC_NOT_EXECUTABLE")
            archive.writestr("ggml-vulkan.dll", b"SYNTHETIC_NOT_A_LIBRARY")
            archive.writestr("licenses/upstream.txt", b"Synthetic upstream notice - retain verbatim.")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, content in extra:
                archive.writestr(name, content)
    return output.getvalue()


class Response:
    def __init__(self, body=b"", *, status=200, headers=None, on_read=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.on_read = on_read
        self.reads = 0

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def isclosed(self):
        return self.body.closed

    def read1(self, amount):
        self.reads += 1
        if self.on_read is not None:
            self.on_read()
        result = self.body.read(amount)
        if not result:
            self.close()
        return result

    def close(self):
        self.body.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class IsolatedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="jobwolf-synthetic-speech-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "speech"
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(redirect_stderr(self.output))
        # Any unexpected use of the real transport fails before DNS/network.
        self.enterContext(patch.object(setup, "_PublicHTTPSConnection", side_effect=AssertionError("Network forbidden in tests")))
        self.enterContext(patch.object(setup, "_require_windows"))
        self.enterContext(patch.object(setup.shutil, "disk_usage", return_value=SimpleNamespace(free=20 * 1024**3)))


class ContractTests(IsolatedTests):
    def test_backend_constants_and_pins(self):
        self.assertEqual(assets.MODEL_LABEL, "Granite Speech 5.0 2B · Q8")
        self.assertEqual(assets.RUNTIME_DIR_NAME, "llama-b11935-vulkan")
        self.assertEqual(assets.MODEL_FILENAME, "granite-speech-5.0-2b-Q8_0.gguf")
        self.assertEqual(assets.PROJECTOR_FILENAME, "mmproj-model-f16.gguf")
        self.assertEqual(assets.RUNTIME_ASSET.size, 37_178_856)
        self.assertEqual(assets.RUNTIME_ASSET.sha256, "55f579da702fd47d0c1587589601e84b075541aafb4761046e135409f02428fc")
        self.assertEqual([asset.size for asset in assets.MODEL_ASSETS], [1_956_155_104, 1_159_354_752])
        self.assertEqual([asset.sha256 for asset in assets.MODEL_ASSETS], [
            "4972786fa455b71ee1f020e8ae1b147ca6cd2b1e5b0818048011d7f1da242790",
            "c0d108e1a3cf345b22c411245fc2762319f1c6b6e56bb15bdffe748c9342ca7d",
        ])
        for asset in assets.MODEL_ASSETS:
            self.assertIn("/resolve/945b4581e2b40cd9a3121de265fd8e387bdf782d/", asset.url)
        self.assertEqual(sum(asset.size for asset in assets.MODEL_ASSETS) + assets.RUNTIME_ASSET.size, 3_152_688_712)

    def test_asset_directory_is_side_effect_free(self):
        with patch.object(assets, "default_data_dir", return_value=self.parent / "JobWolfBench"):
            self.assertEqual(assets.asset_directory(), self.parent / "JobWolfBench" / "speech")
        self.assertFalse((self.parent / "JobWolfBench").exists())

    def test_default_status_does_not_create_hash_or_download(self):
        with (
            patch.object(setup, "asset_directory", return_value=self.root),
            patch.object(setup, "_matches_file", side_effect=AssertionError("Status must not hash files")),
            patch.object(setup, "_download") as download,
        ):
            self.assertEqual(setup.main([]), 0)
        download.assert_not_called()
        self.assertFalse(self.root.exists())
        self.assertIn("Status only", self.output.getvalue())
        self.assertIn("--install", self.output.getvalue())

    def test_directory_override_never_consults_default_workspace(self):
        with patch.object(setup, "asset_directory", side_effect=AssertionError("Default data path forbidden")):
            self.assertEqual(setup.main(["--directory", str(self.root)]), 0)
        self.assertFalse(self.root.exists())

    def test_status_labels_presence_as_unverified(self):
        (self.root / "models").mkdir(parents=True)
        (self.root / "models" / assets.MODEL_FILENAME).write_bytes(b"not a real model")
        self.assertEqual(setup.main(["--directory", str(self.root)]), 0)
        self.assertIn("present (not hash-verified)", self.output.getvalue())

    def test_network_device_and_parent_directory_paths_are_rejected(self):
        for name in ("//server/share", r"\\server\share", r"\\?\C:\speech", "../speech", "file:stream", "NUL", "trailing."):
            with self.subTest(name=name), self.assertRaises(setup.SetupError):
                setup._local_directory(Path(name))


class DownloadTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.root.mkdir()
        self.body = b"synthetic-model"
        self.asset = pinned("model.gguf", self.body)
        self.destination = self.root / self.asset.filename

    @contextmanager
    def response(self, response):
        with patch.object(setup, "_download_response", return_value=response):
            yield

    def test_hash_verified_download_is_atomically_renamed(self):
        self.destination.write_bytes(b"old")
        stale = self.root / ".model.gguf.someone-elses.part"
        stale.write_bytes(b"do not touch")

        def before_read():
            self.assertEqual(self.destination.read_bytes(), b"old")
            self.assertEqual(len(list(self.root.glob("*.part"))), 2)

        with self.response(Response(self.body, headers={"Content-Length": str(len(self.body))}, on_read=before_read)):
            setup._download(self.asset, self.destination)
        self.assertEqual(self.destination.read_bytes(), self.body)
        self.assertEqual(list(self.root.glob("*.part")), [stale])
        self.assertEqual(stale.read_bytes(), b"do not touch")

    def test_hash_truncation_oversize_and_header_failures_keep_existing_file(self):
        cases = [
            Response(b"x" * len(self.body)),
            Response(self.body[:-1]),
            Response(self.body + b"x"),
            Response(self.body, headers={"Content-Length": "1"}),
            Response(self.body, headers={"Content-Length": "unknown"}),
            Response(self.body, headers={"Content-Encoding": "gzip"}),
        ]
        for response in cases:
            with self.subTest(headers=response.headers):
                self.destination.write_bytes(b"original")
                with self.response(response), self.assertRaises(setup.SetupError):
                    setup._download(self.asset, self.destination)
                self.assertEqual(self.destination.read_bytes(), b"original")
                self.assertFalse(list(self.root.glob("*.part")))

    def test_read_timeout_is_safe_and_cleans_only_own_part(self):
        self.destination.write_bytes(b"original")

        def timeout():
            raise TimeoutError("SYNTHETIC_SECRET_SIGNED_URL")

        with self.response(Response(self.body, on_read=timeout)), self.assertRaises(setup.SetupError) as error:
            setup._download(self.asset, self.destination)
        self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))
        self.assertEqual(self.destination.read_bytes(), b"original")
        self.assertFalse(list(self.root.glob("*.part")))

    def test_total_deadline_rejects_trickling_body_without_sleep(self):
        now = [0.0]

        def slow_read():
            now[0] = 3.0

        with (
            patch.object(setup.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(setup, "DOWNLOAD_DEADLINE_SECONDS", 2),
            patch.object(setup.threading, "Timer"),
            self.response(Response(self.body, on_read=slow_read)),
            self.assertRaisesRegex(setup.SetupError, "deadline"),
        ):
            setup._download(self.asset, self.destination)
        self.assertFalse(self.destination.exists())
        self.assertFalse(list(self.root.glob("*.part")))

    def test_deadline_watchdog_interrupts_socket_even_during_headers(self):
        connection = Mock()
        with patch.object(setup.threading, "Timer") as timer:
            with setup._Transfer() as transfer:
                transfer.watch(connection)
                self.assertLessEqual(transfer.timeout(), setup.READ_TIMEOUT_SECONDS)
                seconds, callback = timer.call_args.args
                self.assertEqual(seconds, setup.DOWNLOAD_DEADLINE_SECONDS)
                timer.return_value.start.assert_called_once()
                callback()
                connection.shutdown.assert_called_with(socket.SHUT_RDWR)
                connection.close.assert_called_once()
            timer.return_value.cancel.assert_called_once()

    def test_interrupted_atomic_write_leaves_original_and_no_part(self):
        self.destination.write_bytes(b"original")
        with self.assertRaises(KeyboardInterrupt), setup._atomic_output(self.destination) as output:
            output.write(b"partial")
            raise KeyboardInterrupt
        self.assertEqual(self.destination.read_bytes(), b"original")
        self.assertFalse(list(self.root.glob("*.part")))


class NetworkPolicyTests(unittest.TestCase):
    def test_approved_cdns_and_signed_queries_are_allowed(self):
        for host in setup.DOWNLOAD_HOSTS:
            with self.subTest(host=host):
                self.assertEqual(setup._checked_url(f"https://{host}/public-file?Signature=synthetic"), (host, "/public-file?Signature=synthetic"))

    def test_private_credentials_unapproved_hosts_and_https_downgrades_are_rejected(self):
        for url in (
            "http://github.com/asset", "file:///private/file", "ftp://github.com/a",
            "https://127.0.0.1/a", "https://[::1]/a", "https://localhost/a",
            "https://huggingface.co.evil.test/a", "https://evil.hf.co/a",
            "https://user:password@huggingface.co/a", "https://@huggingface.co/a",
            "https://github.com:8443/a", "https://github.com/a#fragment",
            "https://github.com/line\nbreak", "https://github.com\\evil.test/a",
        ):
            with self.subTest(url=url), self.assertRaises(setup.SetupError):
                setup._checked_url(url)

    def test_dns_rejects_private_and_mixed_public_private_results(self):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1", "224.0.0.1", "::1", "fc00::1"):
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            addresses = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (family, socket.SOCK_STREAM, 6, "", (ip, 443)),
            ]
            with (
                self.subTest(ip=ip),
                patch.object(setup.socket, "getaddrinfo", return_value=addresses),
                self.assertRaisesRegex(setup.SetupError, "public"),
            ):
                setup._public_addresses("github.com", setup._Transfer())

    def test_dns_wait_is_bounded_even_when_os_resolver_stalls(self):
        transfer = Mock()
        transfer.timeout.return_value = 0.001
        with patch.object(setup.threading, "Thread") as thread, self.assertRaisesRegex(setup.SetupError, "DNS lookup timed out"):
            setup._public_addresses("github.com", transfer)
        self.assertTrue(thread.call_args.kwargs["daemon"])

    def test_https_uses_checked_numeric_address_and_original_tls_hostname(self):
        transfer = Mock()
        transfer.timeout.return_value = 30
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with (
            patch.object(setup, "_public_addresses", return_value=addresses),
            patch.object(setup.socket, "socket") as raw,
            patch.object(setup.ssl, "create_default_context") as context,
        ):
            connection = setup._PublicHTTPSConnection("github.com", transfer)
            connection.connect()
            raw.return_value.connect.assert_called_once_with(("8.8.8.8", 443))
            context.return_value.wrap_socket.assert_called_once_with(
                raw.return_value, server_hostname="github.com", do_handshake_on_connect=False,
            )
            context.return_value.wrap_socket.return_value.do_handshake.assert_called_once()
            connection.close()

    def test_redirects_send_no_environment_credentials_cookies_or_proxy(self):
        redirect = Response(status=302, headers={
            "Location": "https://cas-bridge.xethub.hf.co/public?Signature=synthetic",
            "Set-Cookie": "untrusted-cookie",
        })
        final = Response(b"model")
        connections = [Mock(), Mock()]
        for connection, response in zip(connections, (redirect, final)):
            connection.getresponse.return_value = response
        with (
            patch.dict(os.environ, {"HTTPS_PROXY": "http://private-proxy.invalid", "HF_TOKEN": "SYNTHETIC_TOKEN"}),
            patch.object(setup, "_PublicHTTPSConnection", side_effect=connections) as factory,
            setup._Transfer() as transfer,
            setup._download_response("https://huggingface.co/public", transfer) as response,
        ):
            self.assertIs(response, final)
            self.assertEqual([call.args[0] for call in factory.call_args_list], ["huggingface.co", "cas-bridge.xethub.hf.co"])
        for connection in connections:
            self.assertEqual(connection.request.call_args.args[0], "GET")
            self.assertEqual(connection.request.call_args.kwargs["headers"], {
                "User-Agent": "JobWolf-optional-speech-setup/1", "Accept-Encoding": "identity",
            })
            connection.close.assert_called_once()
        self.assertEqual(redirect.reads, 0)

    def test_redirect_to_private_host_is_rejected_before_connecting(self):
        connection = Mock()
        connection.getresponse.return_value = Response(status=302, headers={"Location": "http://127.0.0.1/private"})
        with (
            patch.object(setup, "_PublicHTTPSConnection", return_value=connection) as factory,
            setup._Transfer() as transfer,
            self.assertRaises(setup.SetupError),
            setup._download_response("https://github.com/public", transfer),
        ):
            self.fail("Unsafe redirect accepted")
        factory.assert_called_once()
        connection.close.assert_called_once()

    def test_redirect_count_is_bounded_and_http_errors_do_not_echo_bodies(self):
        connections = []

        def redirect_connection(*_args):
            connection = Mock()
            connection.getresponse.return_value = Response(status=302, headers={"Location": "/again"})
            connections.append(connection)
            return connection

        with (
            patch.object(setup, "_PublicHTTPSConnection", side_effect=redirect_connection),
            setup._Transfer() as transfer,
            self.assertRaisesRegex(setup.SetupError, "redirect limit"),
            setup._download_response("https://github.com/public", transfer),
        ):
            self.fail("Unbounded redirects")
        self.assertEqual(len(connections), setup.MAX_REDIRECTS + 1)
        denied = Mock()
        denied.getresponse.return_value = Response(b"SYNTHETIC_PRIVATE_BODY", status=403)
        with (
            patch.object(setup, "_PublicHTTPSConnection", return_value=denied),
            setup._Transfer() as transfer,
            self.assertRaisesRegex(setup.SetupError, "HTTP 403") as error,
            setup._download_response("https://github.com/public", transfer),
        ):
            self.fail("HTTP failure accepted")
        self.assertNotIn("SYNTHETIC_PRIVATE", str(error.exception))


class ZipValidationTests(unittest.TestCase):
    def validate(self, data):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return setup._zip_members(archive)

    def test_flat_runtime_layout_and_nested_licenses_are_retained(self):
        entries, paths = self.validate(tiny_zip())
        self.assertEqual(len(entries), 3)
        self.assertFalse(paths["llama-server.exe"])
        self.assertFalse(paths["ggml-vulkan.dll"])
        self.assertTrue(paths["licenses"])
        self.assertFalse(paths["licenses/upstream.txt"])

    def test_zip_traversal_windows_aliases_and_absolute_paths_are_rejected(self):
        for name in (
            "../outside", "a/../../outside", "/absolute", "C:/absolute", r"..\outside",
            "file:stream", "CON", "dir/NUL.txt", "aux.log", "trailing.", "trailing ",
            "a//b", "./file", "dir/../file", r"dir\file", "SHORT~1.DLL", "bad\nfile",
        ):
            # ZipInfo normalizes backslashes when writing on Windows. Patch the
            # serialized name so this tests an actual untrusted archive instead.
            serialized = name.replace("\\", "X")
            data = tiny_zip([(serialized, b"bad")]).replace(serialized.encode(), name.encode())
            with self.subTest(name=name), self.assertRaises(setup.SetupError):
                self.validate(data)
        data = tiny_zip([("badXname", b"bad")]).replace(b"badXname", b"bad\x00name")
        with self.assertRaises(setup.SetupError):
            self.validate(data)

    def test_symlinks_special_files_and_reparse_entries_are_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK):
            info = zipfile.ZipInfo("special")
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            with self.subTest(mode=mode), self.assertRaises(setup.SetupError):
                self.validate(tiny_zip([(info, b"target")]))
        info = zipfile.ZipInfo("reparse")
        info.external_attr = 0x400
        with self.assertRaises(setup.SetupError):
            self.validate(tiny_zip([(info, b"target")]))

    def test_duplicates_case_collisions_and_file_directory_conflicts_are_rejected(self):
        for extra in (
            [("llama-server.exe", b"duplicate")],
            [("LLAMA-SERVER.EXE", b"case collision")],
            [("licenses", b"file where implicit directory exists")],
            [("Licenses/other.txt", b"directory case collision")],
            [(setup.RUNTIME_MARKER_FILENAME, b"forged marker")],
        ):
            with self.subTest(extra=extra), self.assertRaises(setup.SetupError):
                self.validate(tiny_zip(extra))

    def test_entry_size_total_size_and_count_are_bounded(self):
        for name, value in (("MAX_ZIP_ENTRIES", 1), ("MAX_ZIP_FILE_BYTES", 1), ("MAX_RUNTIME_BYTES", 1)):
            with self.subTest(limit=name), patch.object(setup, name, value), self.assertRaises(setup.SetupError):
                self.validate(tiny_zip())

    def test_missing_nested_or_empty_runtime_executable_is_rejected(self):
        for entries in (
            [("nested/llama-server.exe", b"exe"), ("ggml-vulkan.dll", b"dll")],
            [("llama-server.exe", b"exe")],
            [("llama-server.exe", b""), ("ggml-vulkan.dll", b"dll")],
        ):
            with self.subTest(entries=entries), self.assertRaises(setup.SetupError):
                self.validate(tiny_zip(entries, required=False))

    def test_encryption_and_unexpected_compression_are_rejected(self):
        with zipfile.ZipFile(io.BytesIO(tiny_zip())) as archive:
            archive.infolist()[0].flag_bits |= 1
            with self.assertRaises(setup.SetupError):
                setup._zip_members(archive)
        with zipfile.ZipFile(io.BytesIO(tiny_zip())) as archive:
            archive.infolist()[0].compress_type = zipfile.ZIP_LZMA
            with self.assertRaises(setup.SetupError):
                setup._zip_members(archive)


class InstallTests(IsolatedTests):
    def setUp(self):
        super().setUp()
        self.zip_body = tiny_zip()
        self.runtime_asset = pinned(assets.RUNTIME_ASSET.filename, self.zip_body, runtime=True)
        self.model_bodies = (b"synthetic q8 model", b"synthetic projector")
        self.model_assets = tuple(pinned(name, body) for name, body in zip(
            (assets.MODEL_FILENAME, assets.PROJECTOR_FILENAME), self.model_bodies,
        ))
        self.enterContext(patch.object(setup, "RUNTIME_ASSET", self.runtime_asset))
        self.enterContext(patch.object(setup, "MODEL_ASSETS", self.model_assets))
        self.bodies = {self.runtime_asset.url: self.zip_body}
        self.bodies.update({asset.url: body for asset, body in zip(self.model_assets, self.model_bodies)})
        self.requests = []

        @contextmanager
        def download_response(url, _transfer):
            self.assertIn("about 3.2 GB", self.output.getvalue())
            self.requests.append(url)
            body = self.bodies[url]
            with Response(body, headers={"Content-Length": str(len(body))}) as response:
                yield response

        self.enterContext(patch.object(setup, "_download_response", side_effect=download_response))

    def test_isolated_install_retains_licenses_and_is_idempotent(self):
        self.assertEqual(setup.main(["--directory", str(self.root), "--install"]), 0)
        runtime = self.root / assets.RUNTIME_DIR_NAME
        self.assertEqual((runtime / "llama-server.exe").read_bytes(), b"SYNTHETIC_NOT_EXECUTABLE")
        self.assertEqual((runtime / "licenses" / "upstream.txt").read_bytes(), b"Synthetic upstream notice - retain verbatim.")
        self.assertIn("MIT License", (runtime / setup.RUNTIME_LICENSE_FILENAME).read_text())
        self.assertIn("END OF TERMS AND CONDITIONS", (self.root / "models" / setup.MODEL_LICENSE_FILENAME).read_text())
        notice = (self.root / "models" / setup.MODEL_NOTICE_FILENAME).read_text()
        self.assertIn(assets.MODEL_COMMIT, notice)
        self.assertIn("No guarantee", notice)
        for asset, body in zip(self.model_assets, self.model_bodies):
            self.assertEqual((self.root / "models" / asset.filename).read_bytes(), body)
        before = {path: path.stat().st_mtime_ns for path in self.root.rglob("*") if path.is_file()}
        setup.install(self.root)
        self.assertEqual(self.requests, [self.runtime_asset.url, *(asset.url for asset in self.model_assets)])
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertFalse((self.root / setup.LOCK_FILENAME).exists())

    def test_repairs_only_invalid_pinned_files_and_preserves_unrelated_data(self):
        setup.install(self.root)
        self.requests.clear()
        model = self.root / "models" / assets.MODEL_FILENAME
        model.write_bytes(b"broken model")
        executable = self.root / assets.RUNTIME_DIR_NAME / "llama-server.exe"
        executable.write_bytes(b"broken exe")
        unrelated = self.root / "models" / "my-other-model.gguf"
        unrelated.write_bytes(b"leave alone")
        other_runtime = self.root / "other-runtime"
        other_runtime.mkdir()
        (other_runtime / "my-file").write_bytes(b"also leave alone")
        setup.install(self.root)
        self.assertEqual(self.requests, [self.model_assets[0].url])
        self.assertEqual(model.read_bytes(), self.model_bodies[0])
        self.assertEqual(executable.read_bytes(), b"SYNTHETIC_NOT_EXECUTABLE")
        self.assertEqual(unrelated.read_bytes(), b"leave alone")
        self.assertEqual((other_runtime / "my-file").read_bytes(), b"also leave alone")

    def test_unowned_runtime_is_never_replaced_or_downloaded_over(self):
        runtime = self.root / assets.RUNTIME_DIR_NAME
        runtime.mkdir(parents=True)
        original = runtime / "llama-server.exe"
        original.write_bytes(b"user supplied executable")
        with self.assertRaisesRegex(setup.SetupError, "not owned"):
            setup.install(self.root)
        self.assertEqual(original.read_bytes(), b"user supplied executable")
        self.assertEqual(self.requests, [])
        self.assertFalse((runtime / setup.RUNTIME_MARKER_FILENAME).exists())

    def test_unknown_file_blocks_repair_even_in_owned_runtime(self):
        setup.install(self.root)
        self.requests.clear()
        runtime = self.root / assets.RUNTIME_DIR_NAME
        extra = runtime / "user-notes.txt"
        extra.write_bytes(b"leave alone")
        executable = runtime / "llama-server.exe"
        executable.write_bytes(b"still broken")
        with self.assertRaisesRegex(setup.SetupError, "unrecognized"):
            setup.install(self.root)
        self.assertEqual(extra.read_bytes(), b"leave alone")
        self.assertEqual(executable.read_bytes(), b"still broken")
        self.assertEqual(self.requests, [])
        self.assertFalse(list(self.root.glob("*.part")))

    def test_zip_hash_is_checked_before_parsing_or_extraction(self):
        self.root.mkdir()
        archive = self.root / self.runtime_asset.filename
        archive.write_bytes(b"not the pinned archive")
        with patch.object(setup.zipfile, "ZipFile") as parse, self.assertRaisesRegex(setup.SetupError, "SHA-256"):
            setup._install_runtime(self.root, archive)
        parse.assert_not_called()
        self.assertFalse((self.root / assets.RUNTIME_DIR_NAME).exists())

    def test_unsafe_zip_never_publishes_runtime_or_escapes_staging(self):
        self.root.mkdir()
        body = tiny_zip([("../outside.txt", b"bad")])
        asset = pinned(self.runtime_asset.filename, body, runtime=True)
        archive = self.root / asset.filename
        archive.write_bytes(body)
        with patch.object(setup, "RUNTIME_ASSET", asset), self.assertRaises(setup.SetupError):
            setup._install_runtime(self.root, archive)
        self.assertFalse((self.root / assets.RUNTIME_DIR_NAME).exists())
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertFalse((self.parent / "outside.txt").exists())
        self.assertFalse(list(self.root.glob("*.part")))

    def test_low_disk_space_fails_before_any_download(self):
        with patch.object(setup.shutil, "disk_usage", return_value=SimpleNamespace(free=0)):
            self.assertEqual(setup.main(["--directory", str(self.root), "--install"]), 1)
        self.assertIn("Not enough disk space", self.output.getvalue())
        self.assertEqual(self.requests, [])
        self.assertFalse((self.root / setup.LOCK_FILENAME).exists())

    def test_existing_lock_is_not_deleted_or_overwritten(self):
        self.root.mkdir()
        lock = self.root / setup.LOCK_FILENAME
        lock.write_bytes(b"existing owner")
        with self.assertRaisesRegex(setup.SetupError, "locked"):
            setup.install(self.root)
        self.assertEqual(lock.read_bytes(), b"existing owner")
        self.assertEqual(self.requests, [])

    def test_hardlinked_pinned_file_is_refused_without_modifying_target(self):
        (self.root / "models").mkdir(parents=True)
        original = self.parent / "synthetic-private-file"
        original.write_bytes(b"leave alone")
        destination = self.root / "models" / assets.MODEL_FILENAME
        try:
            os.link(original, destination)
        except OSError:
            self.skipTest("Hard links unavailable on this filesystem")
        with self.assertRaisesRegex(setup.SetupError, "unlinked"):
            setup.install(self.root)
        self.assertEqual(original.read_bytes(), b"leave alone")
        self.assertEqual(self.requests, [])

    def test_symlinked_parent_is_refused(self):
        target = self.parent / "synthetic-outside"
        target.mkdir()
        try:
            self.root.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symlink creation requires Windows developer mode/privileges")
        with self.assertRaisesRegex(setup.SetupError, "symlinks"):
            setup.install(self.root)
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.requests, [])

    def test_windows_reparse_point_is_refused_without_following_it(self):
        original_lstat = Path.lstat

        def lstat(path):
            if path == self.root:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
            return original_lstat(path)

        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(setup.SetupError, "reparse"):
            setup.install(self.root)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
