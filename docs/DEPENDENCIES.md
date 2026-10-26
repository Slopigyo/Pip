# Dependency and licensing choices

The project license is **not selected by this prototype**. There is no license grant for JobWolf's own code in this document. These dependency choices are intended to preserve future permissive, copyleft, or proprietary application licensing options, subject to normal third-party notices and distribution obligations. This is an engineering inventory, not a legal guarantee for every future distribution.

## Runtime

Only the base packages are requested, not FastAPI/uvicorn `standard` or `all` extras. No paid backend, hosted database, commercial encryption extension, proprietary agent middleware, npm framework, or CDN is required.

| Component | License / terms | Use |
|---|---|---|
| Python | PSF license | Runtime and standard library |
| SQLite | Public domain | Canonical local persistence |
| FastAPI | MIT | Local HTTP routes |
| Pydantic / pydantic-core | MIT | Typed proposals and validated state |
| pypdf 6.21.2 | BSD-3-Clause | Local text-based PDF résumé extraction |
| uvicorn / Starlette | BSD-3-Clause | Local server and HTTP implementation |
| anyio | MIT | Runtime concurrency support |
| annotated-types / annotated-doc / typing-inspection | MIT | Typing support |
| typing-extensions | PSF-2.0 | Typing support |
| h11 | MIT | HTTP protocol |
| click / idna / colorama | BSD-3-Clause | CLI, hostname and Windows terminal support |
| Ollama application | MIT upstream | Optional separately installed local-model runtime |
| Windows DPAPI | Windows operating-system API | Account-scoped at-rest encryption; not an added FOSS or commercial redistributable library |

Retain required third-party notices when packaging/distributing dependencies. `pyproject.toml` pins the five direct Python dependencies; `constraints.txt` records the additional versions used on the Windows/Python 3.14 bench. Recheck dependencies and advisories when updating or packaging a release.

The DPAPI choice intentionally makes this first runtime Windows-specific. It avoids adding a paid SQLite encryption product or writing custom cryptography. A future cross-platform product needs another reviewed storage/key-management adapter; the provider-independent application logic does not need a rewrite.

Résumé parsing requires no hosted service or paid SDK. DOCX uses Python's standard-library ZIP/XML readers; TXT/Markdown use standard text decoding. pypdf is installed without optional image/OCR/crypto extras. No OCR engine, document converter, macro runtime, or executable PDF helper is bundled. Parsing runs in a time-bounded child process with resource checks; that process is **not an OS sandbox or a strict total-memory limit**.

## Development only

Setuptools (MIT) builds the package. HTTPX (BSD-3-Clause) is an optional test dependency for FastAPI's TestClient; its dependencies include httpcore (BSD-3-Clause) and certifi (MPL-2.0). **Certifi is not a runtime dependency of the bench.** MPL-2.0 is file-level copyleft, not a requirement to relicense the whole application, but distributions containing it must still satisfy its terms. The stable HTTPX test path currently emits a Starlette deprecation warning; it works without adopting another client library for this prototype.

The browser interface is plain project-authored HTML/CSS/JavaScript. Node, if installed, is used only for syntax checks or optional browser smoke tests, not to run the app.

## Important distinction: model and service licenses

Ollama's software license does **not** cover every model it can run. User-supplied model weights (including custom model names such as Lyra), their base models, fine-tunes, and datasets have separate terms. This project does not download, bundle, redistribute, or certify the license of those weights. GPT, Claude, Gemma, and other model/service names do not imply a common license or subscription entitlement.

Manual handoff uses the external chat tool the user chooses, under that tool's terms. It neither embeds proprietary service code nor grants API rights. Future one-click account sign-in must use an officially supported integration and review its redistribution, branding, OAuth, and subscription-use rules; there is no universal subscription connector today in this app.
