# Architecture

## Overview

Ombud is a stateless MCP server deployed to AWS Lambda. Employer agents connect over the Model Context Protocol's streamable HTTP transport and call three tools to evaluate a candidate. All fit scoring is deterministic and rule-based — no external API calls.

---

## Component map

```
Employer agent
  │  MCP / Streamable HTTP
  │  POST {FunctionUrl}/mcp
  ▼
AWS Lambda (Python 3.12, arm64)
  ├── FastMCP server
  │     ├── get_profile
  │     ├── get_availability
  │     └── get_fit_signal → fit_engine.py (rule-based, no I/O)
  ├── Starlette routes
  │     ├── GET  /              web UI login + editor
  │     ├── POST /api/login
  │     ├── POST /api/logout
  │     ├── GET  /api/profile/{id}  ← token required
  │     ├── PUT  /api/profile/{id}  ← token required
  │     └── GET  /api/logs          ← token required
  └── profile.py
        ├── local filesystem  (dev)
        └── S3 bucket         (prod, if PROFILE_BUCKET set)

Structured logs → CloudWatch Logs
```

---

## Data flow

### `get_profile`
Loads the candidate YAML, checks `consent.employer_visible`, applies `withheld_fields`, and returns `identity`, `experience`, `education`, and `skills`.

### `get_availability`
Loads the candidate YAML, checks `consent.employer_visible`, applies `withheld_fields`, and returns `search.status`, `available_from`, `notice_period`, `target`, and `geography`. Compensation is excluded from this tool's scope regardless of consent settings.

### `get_fit_signal`
```
1. Load profile
2. Check consent.employer_visible — return error if false
3. apply_withheld(profile) — strip consent.withheld_fields (e.g. search.compensation)
4. compute_fit_signal(profile, role) — pure rule-based scoring
5. Attach schema_version + consent metadata
6. Log to CloudWatch
```

---

## Fit engine

`fit_engine.py` is a pure Python module with no external dependencies. It evaluates seven dimensions and combines them into an overall signal:

| Dimension | Method |
|---|---|
| Skills | Keyword match of skill labels against role text |
| Experience | Scope metadata from most recent role (team size, geography, P&L) |
| Seniority | Hierarchy comparison (ic → manager → director → vp → svp → c_suite → board) |
| Culture | Deal-breaker keyword detection + motivator alignment |
| Readiness | Direct from `search.status` |
| Interest | Compares role against candidate's target roles, stages, size range, industry |
| Constraints | Location exclusions + remote policy conflicts |

Overall signal = fit score × 0.6 + interest score × 0.4, with a readiness modifier. Hard constraint violations short-circuit to `poor` regardless of other scores.

Output signals: `strong` | `likely` | `possible` | `poor`

---

## Profile storage

One YAML file per candidate, resolved as `profiles/{candidate_id}.yaml`.

| Mode | Condition | Read | Write |
|---|---|---|---|
| Local | `PROFILE_BUCKET` unset | filesystem | filesystem |
| S3 | `PROFILE_BUCKET` set | `s3:GetObject` | `s3:PutObject` |

The S3 client is instantiated once at module load (not per request). The IAM policy scopes access to `{bucket}/{prefix}/*` only.

---

## Consent model

Two controls in `consent`:

**`employer_visible: bool`** — Master switch. If false, all three MCP tools return an error. Used to pause visibility without deleting the profile.

**`withheld_fields: [string]`** — Dot-notation paths stripped before data leaves the server. Applied by `apply_withheld()` in `profile.py`. Default: `["search.compensation"]`. Compensation constraint violations still surface in `get_fit_signal` output as plain language — the specific figures are never exposed.

---

## Authentication

MCP tools (employer-facing) are unauthenticated — public read access is intentional.

The browser-facing routes use a simple token gate. `POST /api/login` accepts either `USER_TOKEN` or `ADMIN_TOKEN` and sets an HTTP-only cookie. If `USER_TOKEN` is unset, it falls back to `ADMIN_TOKEN`.

The profile read/write endpoints (`GET/PUT /api/profile/{id}`) and `/api/logs` also accept `Authorization: Bearer <token>` directly. In the SAM deployment, `ADMIN_TOKEN` is the configured token; an empty or missing token leaves the UI/API effectively open unless you set one.

---

## Deployment

AWS SAM (`template.yaml`). Key parameters:

| Parameter | Purpose |
|---|---|
| `CandidateId` | Profile ID — matches `profiles/{id}.yaml` |
| `AdminToken` | Bearer token for profile write operations |
| `ProfileBucket` | S3 bucket for live updates without redeployment |
| `ProfileKeyPrefix` | S3 key prefix (default: `profiles`) |
| `CorsAllowOrigin` | Restrict in production to your deployed URL |

Lambda runtime: Python 3.12, arm64, 512 MB, 30s timeout.

---

## Request log

`/api/logs` returns the in-memory log of MCP tool calls to authenticated browser/API users. The store is a per-container `deque(maxlen=500)` — it resets on Lambda cold start and is not shared across concurrent containers. For persistent audit logs, query CloudWatch Logs directly; all tool calls are emitted as structured JSON.

Fit-signal entries store only `role_title` and whether a company name was present, not the company name itself.
