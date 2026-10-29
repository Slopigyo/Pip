"""Run the Windows-local test bench; never bind to a network interface."""

import argparse
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .providers import OllamaProvider, ProviderError
from .speech import GraniteSpeech
from .speech_assets import asset_directory
from .storage import SQLiteStore, StorageError, default_data_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Pip: Personal Insight Pathfinder — private Windows bench")
    parser.add_argument("--version", action="version", version=f"Pip {__version__}")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, help="Private local storage directory; default: LOCALAPPDATA/JobWolfBench")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Loopback-only Ollama endpoint")
    parser.add_argument("--speech-dir", type=Path, help="Optional speech assets directory; independent of --data-dir, defaults to LOCALAPPDATA/JobWolfBench/speech")
    parser.add_argument("--no-browser", action="store_true", help="Print the local startup link without opening a browser")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        directory = args.data_dir if args.data_dir is not None else default_data_dir()
        store = SQLiteStore(directory / "workspace.sqlite3")
        provider = OllamaProvider(args.ollama_url)
        token = secrets.token_urlsafe(32)
        speech = GraniteSpeech(args.speech_dir if args.speech_dir is not None else asset_directory())
        app = create_app(store=store, provider=provider, token=token, port=args.port, speech=speech)
        app.state.bench.load()  # Fail visibly on corrupt or inaccessible data before launching UI.
    except (StorageError, ProviderError) as error:
        print(f"Pip could not start: {error}", file=sys.stderr)
        raise SystemExit(1) from None

    # Reserve the socket before opening the page, so an occupied port cannot
    # accidentally receive the new session's browser link.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", args.port))
        sock.listen(128)
    except OSError:
        sock.close()
        print("Pip could not open that local port. Stop the other bench or choose --port 8766.", file=sys.stderr)
        raise SystemExit(1) from None
    url = f"http://127.0.0.1:{args.port}/#token={token}"
    print(f"Pip {__version__} — Personal Insight Pathfinder. Windows-local storage, no automatic cloud calls.")
    print(f"Storage: {store.path}")
    print("Open this private startup link (do not share it):")
    print(url)
    print("Press Ctrl+C in this window to stop. Send starts a local chat reply; opening the app does not run a model.")
    if not args.no_browser:
        timer = threading.Timer(1.0, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    try:
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    main()
