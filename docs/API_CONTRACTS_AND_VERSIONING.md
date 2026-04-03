# API Contracts and Versioning Policy

This document defines request/response contract expectations and compatibility guarantees for AIGM management and DB APIs.

## Versioning

- Management API base: `/api/v1/*`
- DB API base: `/db/v1/*`
- Major version (`v1` -> `v2`) is required for breaking changes.

## Envelope Convention

Most endpoints return JSON with:

- `ok` (boolean) required
- `error` (string) on failure
- endpoint-specific payload fields (`rows`, `row`, `message`, `details`, etc.)

## Idempotency for Mutations

- Management API supports idempotency keys on mutating routes (`POST`/`PUT`/`DELETE`).
- Header accepted:
  - `Idempotency-Key` (preferred)
  - `X-Idempotency-Key` (fallback)
- Repeating the same method/path/key/payload returns the previously stored response.
- Reusing a key with a different payload on the same method/path returns conflict (`409`).
- Retention and size limits are configurable:
  - `AIGM_MANAGEMENT_API_IDEMPOTENCY_TTL_S`
  - `AIGM_MANAGEMENT_API_IDEMPOTENCY_MAX_ENTRIES`

## Compatibility Rules

Non-breaking changes allowed in `v1`:

- adding new optional response fields
- adding new endpoints
- adding optional request fields
- adding new enum values when clients are expected to tolerate unknown values

Breaking changes requiring new major version:

- removing/renaming required fields
- changing field types
- changing endpoint semantics in incompatible ways
- removing endpoints

## Deprecation Policy

- Deprecated fields/endpoints should remain available for at least one minor release cycle.
- Deprecation notices should be documented in `README.md` and relevant docs.
- New replacement fields/endpoints should be published before removing deprecated ones.

## OpenAPI

- Management API publishes OpenAPI at:
  - `GET /api/v1/openapi.json`
- This document is the canonical machine-readable contract for current `v1` routes.
