# Ombud

An MCP server that represents a candidate to employer agents.

The candidate layer in the job market is structurally underserved — every ATS, every sourcing tool, every recruiting workflow is built for employers. Ombud flips that. It exposes a structured, consent-gated profile via the Model Context Protocol so employer agents (AI sourcing tools, executive search firm assistants, ATS integrations) get a calibrated fit signal grounded in what the candidate has actually stated.

---

## How it works

Three MCP tools, one candidate profile:

**`get_profile`** — Returns structured professional history: identity, work history, education, skills.

**`get_availability`** — Returns current search status, timeline, target role preferences, and geographic constraints.

**`get_fit_signal`** — Returns a calibrated fit assessment for a described role. Evaluates skills match, experience fit, seniority calibration, culture alignment, candidate interest, readiness, and hard constraints using a deterministic rule-based engine. No LLM calls. Compensation details are never exposed to callers — constraint violations surface as plain language.

If `consent.employer_visible` is `false`, all three MCP tools return `candidate_not_visible`.

---

## Getting started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

### Local development

```bash
git clone <this-repo>
cd ombud

# Install dependencies
make install

# Start the server (sets ADMIN_TOKEN=dev-token for local UI/API access)
make dev
# → http://localhost:8000
# → MCP endpoint: http://localhost:8000/mcp
```

The web UI at `http://localhost:8000` requires a token before it will show the editor. By default, use `dev-token` for local login and profile edits.

### Run tests

```bash
.venv/bin/python -m pytest -q
```

### Add your profile

Copy `profiles/cmoberg.yaml` to `profiles/{your-id}.yaml` and edit it. Set `CANDIDATE_ID={your-id}` when running. The profile has six sections:

- `identity` — name, headline, location, links
- `experience` — work history with team size, P&L, geography scope
- `education` — degrees
- `skills` — proficiency levels, years of experience
- `search` — current status, target roles, geography, compensation (withheld from callers by default)
- `culture` — values, motivators, deal-breakers

Keep compensation values in `profiles/{your-id}.private.yaml` (gitignored) — the committed profile uses `null` placeholders.

### Profile schema

```yaml
schema_version: "1.0"
candidate_id: "your-id"
completeness_score: 0.85        # 0–1; set manually

identity:
  name: "..."
  headline: "..."
  summary: |
    Multi-sentence professional narrative.
  location:
    city: "..."
    country: "SE"               # ISO 3166-1 alpha-2
    timezone: "Europe/Stockholm"
  links:
    linkedin: "https://linkedin.com/in/..."
    website: null
    github: null

experience:
  - company: "..."
    title: "..."
    start_date: "YYYY-MM"
    end_date: "YYYY-MM"         # null if current
    current: false
    industry: "..."
    company_stage: "series_b"   # seed|series_a|series_b|series_c|growth|public|pe_backed|enterprise
    company_size_at_departure: 35
    scope:
      team_size: 35
      direct_reports: 8
      p_and_l_usd: null         # budget/revenue owned; null if not applicable
      geography: "global"       # local|regional|national|global
    highlights:
      - "..."

education:
  - institution: "..."
    degree: "..."
    field_of_study: "..."
    start_date: "YYYY"
    end_date: "YYYY"

skills:
  - label: "Distributed Systems"
    esco_uri: null              # resolved from ESCO API at write time
    proficiency: "expert"       # beginner|intermediate|advanced|expert
    years: 15
    basis: "stated"             # stated|inferred

search:
  status: "active_search"       # active_search|open|passive|not_looking
  available_from: "YYYY-MM-DD"
  notice_period: null           # e.g. "3 months"

  target:
    roles: ["CEO", "CTO"]
    seniority: ["c_suite", "vp"]
    functional_areas: ["engineering", "product"]
    industries: ["infrastructure_software"]
    company_stages: ["series_b", "series_c"]
    company_size_range: [10, 300]

  geography:
    preferred_locations: ["Stockholm, Sweden"]
    remote_policy: "hybrid"     # remote_only|hybrid|onsite|flexible
    willing_to_relocate: true
    relocation_excluded: []     # hard-stop locations

  compensation:                 # withheld from employer queries by default
    base_minimum_usd: null      # store in profiles/{id}.private.yaml
    equity_required: true
    notes: null

culture:
  values: ["..."]
  motivators: ["..."]
  leadership_style: |
    One paragraph.
  deal_breakers:                # map to hard blockers in get_fit_signal output
    - "..."

consent:
  employer_visible: true        # false = profile not queryable
  withheld_fields:
    - "search.compensation"     # dot-notation paths stripped before any response
```

### `get_fit_signal` response schema

```
overall:
  signal:     strong | likely | possible | poor
  confidence: float 0–1
  summary:    string

fit:
  score: float 0–1
  dimensions:
    skills:     { score, matched: [string], gaps: [string] }
    experience: { score, evidence: [string] }
    seniority:  { score, calibration: underleveled | calibrated | overleveled }
    culture:    { score, signals: [string] }
  blockers:   [string]   # hard stops
  strengths:  [string]
  gaps:       [string]

interest:
  level:               high | moderate | low | unknown
  basis:               stated | inferred | unknown
  signals:             [string]
  stated_preferences:  [string]

readiness:
  status:         active_search | open | passive | not_looking
  available_from: date | null
  notice_period:  string | null

constraints:
  met:     [string]
  unmet:   [string]    # hard stops — role violates candidate constraint
  unknown: [string]

recommended_action:
  action:    engage | request_intro | monitor | do_not_contact
  rationale: string

consent:
  withheld:            [string]   # fields that exist but were not shared
  profile_completeness: float 0–1

schema_version: "1.0"
```

### Connect an MCP client

Point any MCP client at `http://localhost:8000/mcp` using the streamable HTTP transport.

Example with the MCP Python SDK:

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async with streamable_http_client("http://localhost:8000/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool("get_fit_signal", {
            "role_title": "CTO",
            "company_stage": "series_b",
            "company_size": 80,
            "industry": "infrastructure_software",
        })
        print(result)
```

---

## Deploy to AWS Lambda

The server runs on AWS Lambda + Function URL. Permanently free at this traffic level.

### Prerequisites

- [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- AWS credentials configured

### Deploy

```bash
make deploy
# Prompts for SAM parameters including AdminToken (set a strong random value),
# ProfileBucket (optional), CandidateId, and CorsAllowOrigin.
# Outputs: FunctionUrl — your public MCP server endpoint
```

The Function URL is your MCP server address. Configure it as an MCP server in any compatible client.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `CandidateId` | `cmoberg` | Profile ID — filename without `.yaml` |
| `AdminToken` | _(required)_ | Bearer token for profile write operations |
| `ProfileBucket` | _(empty)_ | S3 bucket for live profile updates without redeployment |
| `ProfileKeyPrefix` | `profiles` | S3 key prefix |
| `CorsAllowOrigin` | `*` | Restrict to your deployed URL in production |

### Profile storage

By default the server bundles `profiles/` into the Lambda package. For a live profile you update without redeploying, upload the YAML to S3 and set `ProfileBucket` during `sam deploy`.

### Request log

The `/api/logs` endpoint requires the same browser/API token as the web UI. In Lambda, the log is per-container and resets on cold start. For persistent audit logs, query CloudWatch Logs — all tool calls are emitted as structured JSON.

Fit-signal log entries store `role_title` and whether a company name was supplied, but they do not persist the raw company name.

---

## Project structure

```
src/
├── server.py        # FastMCP tools + Starlette app + Lambda handler
├── profile.py       # YAML loader (local or S3) + consent filter
├── fit_engine.py    # Rule-based fit signal — no external API calls
└── logger.py        # Structured JSON logging → CloudWatch

profiles/
└── cmoberg.yaml     # Candidate profile (compensation fields use null placeholders)

template.yaml        # AWS SAM deployment
```

---

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for component map, data flow, fit engine design, and consent model.

---

## Background

Built by [Carl Moberg](https://cmoberg.com) to demonstrate a thesis: that the abstracted interface between candidates and employers doesn't yet exist on the candidate's side, and that MCP is the right protocol to build it on.

The structural opening: every ATS, every sourcing tool, every recruiting workflow is built for the employer. A product that genuinely represents the candidate — one that can tell a recruiter agent "this role isn't right for this person" — runs against the grain of every incumbent in the space.
