# Contributing

Instructions for people **maintaining** this package. If you only want to
*use* the SDK, read the [User Guide](docs/user-guide.md) instead.

## Development setup

From the repository root:

```bash
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1     PowerShell
# POSIX:    source .venv/bin/activate
pip install -e ".[dev]"          # editable install + test toolchain
```

The `dev` extra pulls in `pytest`, `pytest-asyncio`, `respx`, `python-dotenv`.

### Running tests

```bash
pytest
```

- `pyproject.toml` sets `asyncio_mode = "auto"`, so `async def test_*` functions
  run without an explicit marker.
- Tests use **`respx`** to mock `httpx` — no live API needed. See
  `tests/test_client.py` for the pattern: mock the route, call the method, assert
  URL + parsed result. `tests/test_models.py` covers `from_api` mapping.
  `tests/conftest.py` has helpers like `make_doc`.

## Testing conventions

- **Mock at the HTTP layer** with `respx`, not by monkeypatching the client.
  This exercises path building, param cleaning, and error mapping for real.
- **Cover both paths:** the happy JSON response *and* at least one error status
  → typed exception. See `test_401_raises_authentication_error` and
  `test_422_maps_to_validation_failed_and_is_pagr_error`.
- **Assert the URL**, not just the parsed result — a route regression is easy to
  miss otherwise (`assert route.called`).
- **Binary branches** need their own test (see
  `test_render_pdf_streams_bytes_with_header_metadata` and the 422
  business-outcome test `test_render_pdf_422_is_business_outcome_not_exception`).
- Model tests should assert camelCase→snake_case mapping and default handling.

## Build & release

Build backend is **hatchling**; only the `pagr` package is included in the wheel.

```bash
pip install build twine
python -m build            # produces dist/*.whl and dist/*.tar.gz
twine check dist/*
# twine upload dist/*      # publish (needs credentials)
```

Version is derived dynamically from `pagr/__init__.py:__version__` (see
`[tool.hatch.version]` in `pyproject.toml`) — there is a single source of
truth, nothing to keep in sync.

Release checklist:

1. Bump `__version__` in `pagr/__init__.py` (SemVer).
2. Update `README.md` and the [User Guide](docs/user-guide.md) if the surface
   changed.
3. `pytest` green.
4. `python -m build` + `twine check`.
5. Tag and publish.

**Versioning policy.** The SDK follows SemVer: a breaking change to the public
surface (a removed/renamed export, a changed method signature or return type, or
a behavioural change consumers rely on — e.g. a field that used to be a `str`
becoming an enum) bumps the **major** version; additive, backward-compatible
changes bump **minor**; fixes bump **patch**. Record consumer-visible changes in
`CHANGELOG.md` per release so integrators can see what moved.
