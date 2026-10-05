# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows [Semantic Versioning](https://semver.org/) (0.x releases may change the API).

## 0.1.0 - Unreleased

Initial release of `koldera`, for koldera-core 0.1.

- Clients `KolderaClient` (blocking) and `AsyncKolderaClient` (asyncio) over httpx, built on one shared implementation:
  `base_url` and `token` arguments, or `KOLDERA_URL` and `KOLDERA_TOKEN` from the environment. Python 3.10 to 3.14,
  typed (`py.typed`), `mypy --strict` clean.
- Resources: `pdf` and `html_to_pdf` jobs (`analyze` and `fix`, `generate_analyze` and `generate_fix`), `web` scans,
  `documents` (structured documents with a typed `StructuredDocument` and image uploads), `jobs` (get, wait, findings,
  actions, artifacts, reading-order evidence and approved corrections), `artifacts`, `uploads`, `client_sessions`
  (scoped, short-lived browser sessions minted by your backend), `info()` and `health()`.
- `wait` (sent as `Prefer: wait=N`) and `until_finished` polling on every creation call; `jobs.wait` treats any state
  other than `queued` or `running` as final.
- Open enums: unknown states, kinds, modes and problem codes from a newer koldera-core do not crash the client.
- Typed errors per HTTP status with the problem `code`, per-problem `errors` and `retry_after`; network failures stay
  `httpx` exceptions with the token removed from their text.
- Idempotency keys on creation calls; network failures are repeated with the same key.
- Artifact downloads verify size and SHA-256 and raise `StorageError` on a mismatch. The async client hashes large
  payloads in a worker thread.
- Files and artifacts are held in memory while they are transferred.
- Apache-2.0 licence, CI on Python 3.10 to 3.14 (and Windows), contract check against koldera-core, licence check, secret
  scan, pdoc API reference, and a release workflow with PyPI trusted publishing and attestations.
