"""Explicit, optional Windows x64 speech asset setup; never executes a model.

Without --install this only stats the designated asset paths. Install uses public
HTTPS GETs, no proxies/cookies/credentials, and no private workspace reads. The
pinned ZIP is retained so repairs can be verified without trusting a local hash
manifest. Each replacement is atomic, not the entire multi-file installation.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import os
import platform
import queue
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Self
from urllib.parse import urljoin, urlsplit

from .speech_assets import (
    MODEL_ASSETS,
    MODEL_COMMIT,
    MODEL_LABEL,
    MODEL_SOURCE,
    RUNTIME_ASSET,
    RUNTIME_DIR_NAME,
    Asset,
    asset_directory,
)
from .storage import StorageError

CHUNK_BYTES = 1024 * 1024
READ_TIMEOUT_SECONDS = 30
DOWNLOAD_DEADLINE_SECONDS = 2 * 60 * 60  # Per file, including DNS and redirects.
MAX_REDIRECTS = 5
MAX_ZIP_ENTRIES = 512
MAX_ZIP_FILE_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_BYTES = 1024 * 1024 * 1024
DISK_RESERVE_BYTES = 256 * 1024 * 1024
MODEL_LICENSE_FILENAME = "granite-speech-5.0-2b-APACHE-2.0.txt"
MODEL_NOTICE_FILENAME = "granite-speech-5.0-2b-NOTICE.txt"
RUNTIME_LICENSE_FILENAME = "JobWolf-llama.cpp-b11935-LICENSE.txt"
RUNTIME_MARKER_FILENAME = ".jobwolf-speech-b11935"
LOCK_FILENAME = ".granite-b11935-setup.lock"

# Exact public download hosts, not arbitrary *.githubusercontent.com / *.hf.co.
# A new CDN host requires review; never silently broaden this on a redirect.
DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs.hf.co",
    "cdn-lfs-us-1.hf.co",
    "cdn-lfs-eu-1.hf.co",
    "cas-bridge.xethub.hf.co",
    "us.aws.cdn.hf.co",
    "eu.aws.cdn.hf.co",
})
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


class SetupError(Exception):
    """Safe-to-display setup error, without response bodies or signed URLs."""


def _path_info(path: Path, *, directory: bool) -> os.stat_result | None:
    """Reject links/reparse points before reading or writing any asset path.

    This is not a sandbox against another process running as the same OS user;
    use a local directory that other users/processes cannot modify during setup.
    """
    result = None
    for candidate in (*reversed(path.parents), path):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SetupError("Asset paths must not contain symlinks, junctions, or reparse points.")
        is_directory = candidate != path or directory
        if is_directory and not stat.S_ISDIR(info.st_mode):
            raise SetupError("An asset directory is occupied by a non-directory.")
        if not is_directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise SetupError("Pinned asset files must be regular, unlinked files.")
        if candidate == path:
            result = info
    return result


def _local_directory(directory: Path) -> Path:
    text = str(directory)
    if "\x00" in text or text.startswith(("\\\\", "//")) or ".." in directory.parts:
        raise SetupError("Choose a local directory without network/device paths or '..'.")
    if directory.drive and not directory.is_absolute():
        raise SetupError("Drive-relative asset paths are not supported.")
    result = directory.absolute()
    # Avoid Win32 device names, alternate data streams, and normalized aliases.
    for component in result.parts[1:]:
        if (
            any(character in component for character in '<>:"|?*')
            or any(ord(character) < 32 for character in component)
            or component.endswith((".", " "))
            or component.split(".")[0].upper() in _RESERVED_NAMES
        ):
            raise SetupError("Choose an ordinary local directory name.")
    _path_info(result, directory=True)
    return result


def _make_directory(path: Path) -> None:
    _path_info(path, directory=True)
    path.mkdir(parents=True, exist_ok=True)
    _path_info(path, directory=True)


def _matches_file(path: Path, size: int, digest: str) -> bool:
    info = _path_info(path, directory=False)
    if info is None or info.st_size != size:
        return False
    actual = hashlib.sha256()
    with path.open("rb") as source:
        remaining = size
        while remaining:
            block = source.read(min(CHUNK_BYTES, remaining))
            if not block:
                return False
            actual.update(block)
            remaining -= len(block)
        if source.read(1):
            return False
    return actual.hexdigest() == digest


def _matches_bytes(path: Path, content: bytes) -> bool:
    return _matches_file(path, len(content), hashlib.sha256(content).hexdigest())


@contextmanager
def _atomic_output(destination: Path) -> Iterator[IO[bytes]]:
    _path_info(destination, directory=False)
    # Unique, exclusively created .part files never truncate another install or
    # a pre-existing user file. Only this invocation's temporary file is removed.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", suffix=".part",
            dir=destination.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            yield output.file
            output.flush()
            os.fsync(output.fileno())
        _path_info(destination, directory=False)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_bytes(destination: Path, content: bytes) -> None:
    if not _matches_bytes(destination, content):
        with _atomic_output(destination) as output:
            output.write(content)


def _require_space(directory: Path, needed: int) -> None:
    _path_info(directory, directory=True)
    existing = directory
    while not existing.exists():
        existing = existing.parent
    if shutil.disk_usage(existing).free < needed:
        raise SetupError(f"Not enough disk space: need at least {needed:,} additional free bytes.")


@contextmanager
def _install_lock(directory: Path) -> Iterator[None]:
    path = directory / LOCK_FILENAME
    _path_info(path, directory=False)
    try:
        handle = path.open("xb")
    except FileExistsError:
        raise SetupError(
            f"Setup is already locked. If no setup is running, manually remove {LOCK_FILENAME} and retry."
        ) from None
    try:
        with handle:
            handle.write(b"Optional Granite speech setup in progress.\n")
        yield
    finally:
        path.unlink()


def _checked_url(url: str) -> tuple[str, str]:
    if not url.isascii() or any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise SetupError("Download URL contains invalid characters.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise SetupError("Invalid download URL.") from None
    if (
        parsed.scheme != "https" or parsed.hostname not in DOWNLOAD_HOSTS
        or port not in (None, 443) or parsed.username is not None
        or parsed.password is not None or parsed.fragment or "\\" in url
    ):
        raise SetupError("Download destination is not an approved public HTTPS host.")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return parsed.hostname, target


class _Transfer:
    """Bound the whole transfer, including slow headers and trickling bodies."""

    def __init__(self) -> None:
        self.end = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
        self.socket: socket.socket | None = None
        self.lock = threading.Lock()
        self.timer = threading.Timer(DOWNLOAD_DEADLINE_SECONDS, self._interrupt)
        self.timer.daemon = True

    def timeout(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise SetupError("Download exceeded its total deadline; retry --install when ready.")
        return min(READ_TIMEOUT_SECONDS, remaining)

    def watch(self, connection: socket.socket) -> None:
        with self.lock:
            self.socket = connection
            self.timeout()

    def _interrupt(self) -> None:
        with self.lock:
            connection = self.socket
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def __enter__(self) -> Self:
        self.timer.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.timer.cancel()
        self._interrupt()


def _public_addresses(host: str, transfer: _Transfer) -> list[tuple]:
    # getaddrinfo has no portable timeout. A daemon only resolves DNS (it never
    # opens an HTTP socket or writes files), so even a stuck OS resolver cannot
    # hold up setup past the read timeout/deadline.
    result: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=transfer.timeout())
    except queue.Empty:
        raise SetupError("Download DNS lookup timed out.") from None
    if not addresses:
        raise SetupError("Download host could not be resolved.")
    for family, _kind, _protocol, _name, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global or ip.is_multicast:
            raise SetupError("Download DNS must resolve only to public unicast addresses.")
    return addresses[:8]


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, transfer: _Transfer):
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=transfer.timeout(), context=self.tls_context)
        self.transfer = transfer

    def connect(self) -> None:
        # Connect to the checked numeric address, not a second DNS lookup; TLS
        # still verifies the original approved hostname (including SNI).
        addresses = _public_addresses(self.host, self.transfer)
        for family, kind, protocol, _name, address in addresses:
            connection = socket.socket(family, kind, protocol)
            self.sock = connection
            try:
                self.transfer.watch(connection)
                connection.settimeout(self.transfer.timeout())
                connection.connect(address)
                secured = self.tls_context.wrap_socket(
                    connection, server_hostname=self.host, do_handshake_on_connect=False,
                )
                self.sock = secured
                self.transfer.watch(secured)
                secured.settimeout(self.transfer.timeout())
                secured.do_handshake()
                return
            except ssl.SSLError:
                self.close()
                raise
            except OSError:
                self.close()
        raise SetupError("Could not connect to the public download host.")


@contextmanager
def _download_response(url: str, transfer: _Transfer) -> Iterator[http.client.HTTPResponse]:
    for hop in range(MAX_REDIRECTS + 1):
        host, target = _checked_url(url)
        connection = _PublicHTTPSConnection(host, transfer)
        try:
            # http.client does not load proxies, netrc, credentials, or cookies.
            # No response headers (especially signed CDN URLs) are logged.
            connection.request("GET", target, headers={
                "User-Agent": "JobWolf-optional-speech-setup/1",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            with response:
                transfer.timeout()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or hop == MAX_REDIRECTS:
                        raise SetupError("Download redirect limit reached or Location is missing.")
                    url = urljoin(url, location)
                    _checked_url(url)
                    continue
                if response.status != 200:
                    raise SetupError(f"Public download returned HTTP {response.status}; no credentials will be sent.")
                yield response
                return
        finally:
            connection.close()
    raise SetupError("Download redirect limit reached.")


def _download(asset: Asset, destination: Path) -> None:
    _checked_url(asset.url)
    _path_info(destination, directory=False)
    _require_space(destination.parent, asset.size + DISK_RESERVE_BYTES)
    print(f"Downloading {asset.filename} ({asset.size:,} bytes)...", flush=True)
    try:
        with (
            _atomic_output(destination) as output,
            _Transfer() as transfer,
            _download_response(asset.url, transfer) as response,
        ):
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise SetupError("Compressed HTTP responses are not accepted for pinned files.")
            declared = response.getheader("Content-Length")
            if declared is not None and (not declared.isdecimal() or int(declared) != asset.size):
                raise SetupError("Download Content-Length does not match the pinned size.")
            digest = hashlib.sha256()
            received = 0
            while not response.isclosed():
                timeout = transfer.timeout()
                if transfer.socket is not None:
                    transfer.socket.settimeout(timeout)
                # read1 does at most one underlying read rather than waiting
                # to fill a 1 MiB block while an upstream trickles bytes.
                block = response.read1(min(CHUNK_BYTES, asset.size - received + 1))
                transfer.timeout()
                if not block:
                    break
                received += len(block)
                if received > asset.size:
                    raise SetupError("Download exceeded the pinned size.")
                digest.update(block)
                output.write(block)
            if received != asset.size or digest.hexdigest() != asset.sha256:
                raise SetupError("Download failed pinned size/SHA-256 verification; existing file kept.")
            transfer.timeout()
    except (OSError, http.client.HTTPException, ValueError):
        raise SetupError("Download failed or timed out; existing file kept. Check the connection and retry --install.") from None


def _zip_members(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], dict[str, bool]]:
    entries = archive.infolist()
    if not entries or len(entries) > MAX_ZIP_ENTRIES:
        raise SetupError("Runtime ZIP exceeds the entry-count limit or is empty.")
    layout: dict[str, tuple[str, bool]] = {}
    explicit: set[str] = set()
    total = 0
    for entry in entries:
        name = entry.filename[:-1] if entry.is_dir() else entry.filename
        pieces = name.split("/")
        if (
            entry.orig_filename != entry.filename or len(name) > 200 or not name.isascii()
            or any(
                not piece or piece in (".", "..") or piece.endswith((".", " "))
                or any(character in piece for character in '<>:"\\|?*~')
                or any(ord(character) < 32 or ord(character) == 127 for character in piece)
                or piece.split(".")[0].upper() in _RESERVED_NAMES
                for piece in pieces
            )
        ):
            raise SetupError("Runtime ZIP contains an unsafe Windows path.")
        mode = stat.S_IFMT(entry.external_attr >> 16)
        if (
            mode not in (0, stat.S_IFDIR if entry.is_dir() else stat.S_IFREG)
            or entry.external_attr & 0x400 or entry.flag_bits & 1
            or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        ):
            raise SetupError("Runtime ZIP contains a link, special file, encryption, or unsupported compression.")
        if (
            entry.file_size < 0 or entry.file_size > MAX_ZIP_FILE_BYTES
            or (entry.is_dir() and entry.file_size != 0)
        ):
            raise SetupError("Runtime ZIP exceeds the per-file size limit.")
        total += entry.file_size
        if total > MAX_RUNTIME_BYTES:
            raise SetupError("Runtime ZIP exceeds the expanded-size limit.")
        key = name.casefold()
        if key in explicit or key in {RUNTIME_MARKER_FILENAME.casefold(), RUNTIME_LICENSE_FILENAME.casefold()}:
            raise SetupError("Runtime ZIP has duplicate or reserved installer paths.")
        explicit.add(key)
        for index in range(1, len(pieces) + 1):
            partial = "/".join(pieces[:index])
            is_directory = index < len(pieces) or entry.is_dir()
            previous = layout.get(partial.casefold())
            if previous is not None and previous != (partial, is_directory):
                raise SetupError("Runtime ZIP contains conflicting or case-colliding paths.")
            layout[partial.casefold()] = (partial, is_directory)
    # Verified against b11935 .github/workflows/release.yml (windows-cpu /
    # windows Pack artifacts) and release-publish.yml (Merge artifacts): 7z
    # packages Release/* at the root, then zip -r merges CPU files at that root.
    # https://github.com/ggml-org/llama.cpp/blob/b11935/.github/workflows/release.yml
    # https://github.com/ggml-org/llama.cpp/blob/b11935/.github/workflows/release-publish.yml
    paths = dict(layout.values())
    for required in ("llama-server.exe", "ggml-vulkan.dll"):
        if paths.get(required) is not False or archive.getinfo(required).file_size == 0:
            raise SetupError("Runtime ZIP does not have the verified flat Windows Vulkan layout.")
    return entries, paths


def _runtime_marker() -> bytes:
    return f"JobWolf optional speech runtime\n{RUNTIME_ASSET.filename}\nsha256:{RUNTIME_ASSET.sha256}\n".encode("ascii")


def _check_runtime_ownership(runtime: Path) -> None:
    if _path_info(runtime, directory=True) is None:
        return
    if next(runtime.iterdir(), None) is None:
        return
    if not _matches_bytes(runtime / RUNTIME_MARKER_FILENAME, _runtime_marker()):
        raise SetupError("Pinned runtime directory is not owned by this setup. Move it aside manually or use --directory.")


def _check_runtime_contents(runtime: Path, allowed: dict[str, bool]) -> None:
    allowed = {name.casefold(): is_directory for name, is_directory in allowed.items()}
    pending = [runtime]
    count = 0
    while pending:
        for path in pending.pop().iterdir():
            count += 1
            name = path.relative_to(runtime).as_posix().casefold()
            if name not in allowed or count > MAX_ZIP_ENTRIES * 16:
                raise SetupError("Pinned runtime contains unrecognized files/directories; nothing there will be replaced.")
            is_directory = allowed[name]
            _path_info(path, directory=is_directory)
            if is_directory:
                pending.append(path)


def _install_runtime(directory: Path, archive_path: Path) -> None:
    runtime = directory / RUNTIME_DIR_NAME
    _check_runtime_ownership(runtime)
    if not _matches_file(archive_path, RUNTIME_ASSET.size, RUNTIME_ASSET.sha256):
        raise SetupError("Runtime ZIP must pass pinned SHA-256 verification before extraction.")
    _require_space(directory, MAX_RUNTIME_BYTES + DISK_RESERVE_BYTES)
    with tempfile.TemporaryDirectory(prefix=f".{RUNTIME_DIR_NAME}-", suffix=".part", dir=directory) as temporary:
        staging = Path(temporary)
        hashes: dict[str, tuple[int, str]] = {}
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries, layout = _zip_members(archive)
                # Extract the entire verified archive, including every upstream
                # license/notice. Never extractall, flatten, or pick only DLLs.
                for entry in entries:
                    path = staging.joinpath(*entry.filename.rstrip("/").split("/"))
                    if entry.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    written = 0
                    with archive.open(entry) as source, path.open("xb") as output:
                        while block := source.read(CHUNK_BYTES):
                            written += len(block)
                            if written > entry.file_size:
                                raise SetupError("Runtime ZIP expanded beyond its declared size.")
                            output.write(block)
                            digest.update(block)
                    if written != entry.file_size:
                        raise SetupError("Runtime ZIP member is truncated.")
                    hashes[entry.filename] = (written, digest.hexdigest())
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
            raise SetupError("Runtime ZIP could not be validated safely.") from None
        license_bytes = _RUNTIME_LICENSE.encode("utf-8")
        (staging / RUNTIME_LICENSE_FILENAME).write_bytes(license_bytes)
        hashes[RUNTIME_LICENSE_FILENAME] = (len(license_bytes), hashlib.sha256(license_bytes).hexdigest())
        layout[RUNTIME_LICENSE_FILENAME] = False
        layout[RUNTIME_MARKER_FILENAME] = False
        _check_runtime_ownership(runtime)
        if _path_info(runtime, directory=True) is not None:
            _check_runtime_contents(runtime, layout)
        _make_directory(runtime)
        # Claim a new/empty directory before moving files so an interrupted
        # first install remains repairable. Unknown directories are never claimed.
        _write_bytes(runtime / RUNTIME_MARKER_FILENAME, _runtime_marker())
        for name, is_directory in sorted(layout.items(), key=lambda item: item[0].count("/")):
            if is_directory:
                _make_directory(runtime.joinpath(*name.split("/")))
        repaired = 0
        for name, (size, digest) in hashes.items():
            target = runtime.joinpath(*name.split("/"))
            if not _matches_file(target, size, digest):
                _path_info(target, directory=False)
                staging.joinpath(*name.split("/")).replace(target)
                repaired += 1
        print(f"Runtime verified ({repaired} pinned files installed/repaired); archive licenses retained.")


def _require_windows() -> None:
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise SetupError("This pinned runtime is for Windows x64 only.")


def install(directory: Path) -> None:
    """Explicit public downloads into a designated local asset directory only."""
    _require_windows()
    directory = _local_directory(directory)
    total = RUNTIME_ASSET.size + sum(asset.size for asset in MODEL_ASSETS)
    print(f"Optional {MODEL_LABEL}: about 3.2 GB ({total:,} bytes) of pinned downloads.")
    print(f"Resources: {directory}")
    for asset in (*MODEL_ASSETS, RUNTIME_ASSET):
        print(f"  {asset.filename}: {asset.size:,} bytes")
    print("Sources: GitHub / Hugging Face and their approved public HTTPS CDNs.")
    print("No credentials, proxies, private data, model execution, or driver/package installs.")
    print("Granite: Apache-2.0; llama.cpp: MIT plus bundled notices. Pip's own code: Apache-2.0; see LICENSE.", flush=True)
    _check_runtime_ownership(directory / RUNTIME_DIR_NAME)
    _make_directory(directory)
    with _install_lock(directory):
        destinations = [(RUNTIME_ASSET, directory / RUNTIME_ASSET.filename)] + [
            (asset, directory / "models" / asset.filename) for asset in MODEL_ASSETS
        ]
        missing = [
            (asset, path) for asset, path in destinations
            if not _matches_file(path, asset.size, asset.sha256)
        ]
        _require_space(directory, sum(asset.size for asset, _path in missing) + MAX_RUNTIME_BYTES + DISK_RESERVE_BYTES)
        _make_directory(directory / "models")
        _write_bytes(directory / "models" / MODEL_LICENSE_FILENAME, _MODEL_LICENSE.encode("utf-8"))
        notice = (
            "IBM Granite Speech 5.0 2B GGUF - optional third-party speech assets\n"
            f"Source: {MODEL_SOURCE}/tree/{MODEL_COMMIT}\n"
            f"License declaration: {MODEL_SOURCE}/raw/{MODEL_COMMIT}/README.md\n"
            "The pinned model card declares Apache-2.0. These downloaded GGUF files are unchanged.\n"
            "License text source: https://www.apache.org/licenses/LICENSE-2.0.txt\n"
            "This is an installer attribution, not an upstream NOTICE or an endorsement.\n"
            "Third-party terms do not choose or grant a license for JobWolf's own code.\n"
            "No guarantee is made about licensing obligations for a future combined distribution.\n"
        )
        _write_bytes(directory / "models" / MODEL_NOTICE_FILENAME, notice.encode("utf-8"))
        for asset, path in destinations:
            if (asset, path) in missing:
                _download(asset, path)
            else:
                print(f"Verified existing {asset.filename}; download skipped.")
            if asset == RUNTIME_ASSET:
                _install_runtime(directory, path)
        print("Speech assets verified. No model or runtime was started.")


def _status(directory: Path) -> None:
    print(f"Optional {MODEL_LABEL}\nResources: {directory}")
    paths = [directory / RUNTIME_DIR_NAME / "llama-server.exe"] + [
        directory / "models" / asset.filename for asset in MODEL_ASSETS
    ]
    for path in paths:
        info = _path_info(path, directory=False)
        print(f"  {path.relative_to(directory)}: {'present (not hash-verified)' if info else 'missing'}")
    print("Status only: no files created, no network requests, no model execution.")
    print("To explicitly download/verify about 3.2 GB: python -m jobwolf.speech_setup --install")
    print('For an isolated location add: --directory "C:\\path\\to\\speech"')
    print("Setup needs extra disk space for staging; it does not install GPU drivers.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Explicitly download and verify the pinned speech assets.")
    parser.add_argument("--directory", type=Path, help="Isolated local resources directory (default: LOCALAPPDATA/JobWolfBench/speech).")
    args = parser.parse_args(argv)
    try:
        directory = _local_directory(args.directory if args.directory is not None else asset_directory())
        if args.install:
            install(directory)
        else:
            _status(directory)
    except (SetupError, StorageError) as error:
        print(f"Speech setup: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Speech setup: local file operation failed. Check permissions, free space, and whether the runtime is in use.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Speech setup cancelled. Completed verified assets are retained; retry --install when ready.", file=sys.stderr)
        return 130
    return 0


# Verified upstream text, not a license grant for JobWolf:
# https://raw.githubusercontent.com/ggml-org/llama.cpp/b11935/LICENSE
_RUNTIME_LICENSE = """MIT License

Copyright (c) 2023-2026 The ggml authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# Verified from https://www.apache.org/licenses/LICENSE-2.0.txt; the pinned
# Granite GGUF model card declares apache-2.0 but has no separate LICENSE file.
_MODEL_LICENSE = """
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""


if __name__ == "__main__":
    raise SystemExit(main())
