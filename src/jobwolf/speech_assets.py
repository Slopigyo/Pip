"""Pinned optional speech assets; importing this module performs no I/O.

Only ``asset_directory`` consults the Windows data-directory setting. It does
not open the private workspace, create directories, or download anything.
"""

from dataclasses import dataclass
from pathlib import Path

from .storage import default_data_dir

MODEL_LABEL = "Granite Speech 5.0 2B · Q8"
RUNTIME_DIR_NAME = "llama-b11935-vulkan"
MODEL_FILENAME = "granite-speech-5.0-2b-Q8_0.gguf"
PROJECTOR_FILENAME = "mmproj-model-f16.gguf"
MODEL_COMMIT = "945b4581e2b40cd9a3121de265fd8e387bdf782d"
MODEL_REPOSITORY = "ibm-granite/granite-speech-5.0-2b-GGUF"
MODEL_SOURCE = f"https://huggingface.co/{MODEL_REPOSITORY}"


@dataclass(frozen=True)
class Asset:
    filename: str
    url: str
    size: int
    sha256: str


# Runtime digest: GitHub release API. Model digests: upstream LFS SHA-256.
RUNTIME_ASSET = Asset(
    "llama-b11935-bin-win-vulkan-x64.zip",
    "https://github.com/ggml-org/llama.cpp/releases/download/b11935/llama-b11935-bin-win-vulkan-x64.zip",
    37_178_856,
    "55f579da702fd47d0c1587589601e84b075541aafb4761046e135409f02428fc",
)
MODEL_ASSETS = (
    Asset(
        MODEL_FILENAME,
        f"{MODEL_SOURCE}/resolve/{MODEL_COMMIT}/{MODEL_FILENAME}",
        1_956_155_104,
        "4972786fa455b71ee1f020e8ae1b147ca6cd2b1e5b0818048011d7f1da242790",
    ),
    Asset(
        PROJECTOR_FILENAME,
        f"{MODEL_SOURCE}/resolve/{MODEL_COMMIT}/{PROJECTOR_FILENAME}",
        1_159_354_752,
        "c0d108e1a3cf345b22c411245fc2762319f1c6b6e56bb15bdffe748c9342ca7d",
    ),
)


def asset_directory() -> Path:
    """Default resources root: %LOCALAPPDATA%/JobWolfBench/speech."""
    return default_data_dir() / "speech"
