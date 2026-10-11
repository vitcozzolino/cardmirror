# Security Review

Review date: 2026-10-11

## Scope

The review covers the Python application, the local interactive picker, the
SQLite catalog/session caches, generated order files, and the locked runtime
dependencies in `uv.lock`.

## Severity Summary

| Severity | Open | Fixed in this review | Notes |
| --- | ---: | ---: | --- |
| Critical | 0 | 0 | No remote code execution, credential handling, or exposed service found. |
| High | 0 | 0 | No high-impact issue identified. |
| Medium | 0 | 2 | Remote URL fetching and local session mutations are now constrained. |
| Low | 0 | 3 | CSV formula injection, MIME hardening, and request bounds were addressed. |
| Informational | 1 | 0 | The picker intentionally runs as a loopback-only ephemeral HTTP server. |

## Fixed Findings

### Medium — SSRF through catalog image links

Catalog hyperlinks are external input and are fetched for thumbnails. Fetching
arbitrary URLs could reach loopback, private, link-local, or reserved network
addresses, including through redirects. Remote requests now require HTTP(S),
reject credentials, resolve to public addresses, cap redirects, and use bounded
timeouts.

### Medium — Unauthenticated selection mutations

The picker server is local-only, but its mutation endpoints previously did not
verify the session identifier. Selection submission now uses POST, validates the
random session token, rejects oversized bodies, and protects recap mutations.

### Low — Spreadsheet formula injection

Vendor-controlled card names or sets could begin with spreadsheet formula
characters in `order.csv`. Formula-leading cells are now prefixed with an
apostrophe and written through the CSV module.

### Low — Remote MIME/header trust

Remote image content types are now normalized and restricted to GIF, JPEG, PNG,
or WebP before being served. Browser responses include `nosniff`, no-store
headers for picker data, and a restrictive referrer policy.

### Low — Request exhaustion

Search queries are capped at 200 characters and selection request bodies at 256
KiB. Thumbnail downloads remain capped at 12 MiB and the cache remains size
bounded.

## Validation

- `uv lock --check` — passed
- `bandit` over `main.py`, `selector.py`, and `utils.py` — 0 findings
- `pip-audit` over the locked export — no known vulnerabilities
- `ruff check` — passed
- Python compilation and targeted SSRF/CSV smoke checks — passed
- Manual credential-pattern review of application source — no credentials found

Semgrep, Gitleaks, and Trivy were not installed in the workspace, so the report
does not claim results from those scanners.

## Residual Risk

The interactive picker is intentionally a local HTTP server. Other processes
running as the same user can still inspect its public search/version endpoints
while it is open; no sensitive credentials are stored or exposed by those
endpoints. A future multi-user or remotely hosted version would need real user
authentication, CSRF protection, and a production web server.
