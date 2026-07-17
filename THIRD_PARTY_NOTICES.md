# Third-party notices

Syndicate does not vendor or modify any third-party source. It depends on
the following external, separately-licensed projects at runtime:

## OpenHands agent-server

- **What it is:** the sandboxed code-execution backend Syndicate delegates
  all bash/file-editing actions to (`syndicate/runtime/openhands.py`).
- **How it's used:** as an external Docker container
  (`ghcr.io/openhands/agent-server`), called only over its published REST
  API (`POST /api/bash/execute_bash_command`, `/api/file/*`). No OpenHands
  source is copied into this repository.
- **License:** MIT. See https://github.com/OpenHands/OpenHands/blob/main/LICENSE
- **Project:** https://github.com/OpenHands/OpenHands

## Python dependencies

All Python dependencies declared in `pyproject.toml` (LangGraph,
LangChain-core, FastAPI, httpx, Anthropic's SDK, etc.) are installed from
PyPI under their own licenses, not vendored. See each package's own
distribution for its license text.
