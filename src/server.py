import contextlib
import os
import secrets
import time
from typing import Annotated, Optional

from pydantic import Field

import yaml
from mangum import Mangum
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

import log_store
from fit_engine import compute_fit_signal
from logger import log_tool_call, source_ip as _source_ip
from profile import apply_withheld, load_profile, read_raw_profile, save_raw_profile


class _SourceIpMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            forwarded = headers.get(b"x-forwarded-for", b"").decode()
            ip = forwarded.split(",")[0].strip() if forwarded else ""
            if not ip:
                client = scope.get("client")
                ip = client[0] if client else "unknown"
            _source_ip.set(ip)
        await self.app(scope, receive, send)

_DEFAULT_CANDIDATE = os.environ.get("CANDIDATE_ID", "candidate")
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_USER_TOKEN = os.environ.get("USER_TOKEN") or _ADMIN_TOKEN
_AUTH_COOKIE = "ombud_auth"
_PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

_PROFILE_REQUIRED_KEYS = {"identity", "experience", "education", "skills", "search", "culture", "consent"}


def _load_employer_profile() -> dict:
    profile = load_profile(_DEFAULT_CANDIDATE)
    if not profile.get("consent", {}).get("employer_visible", True):
        raise ValueError("candidate_not_visible")
    return apply_withheld(profile)


def _not_visible_response() -> dict:
    return {
        "error": "candidate_not_visible",
        "message": "This candidate is not currently visible to employer queries.",
    }


def _is_admin(request: Request) -> bool:
    if not _ADMIN_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {_ADMIN_TOKEN}":
        return True
    cookie = request.cookies.get(_AUTH_COOKIE, "")
    return bool(cookie) and secrets.compare_digest(cookie, _ADMIN_TOKEN)


def _is_authenticated(request: Request) -> bool:
    if not _USER_TOKEN:
        return True

    bearer = request.headers.get("Authorization", "")
    if bearer == f"Bearer {_USER_TOKEN}" or bearer == f"Bearer {_ADMIN_TOKEN}":
        return True

    cookie_token = request.cookies.get(_AUTH_COOKIE)
    if not cookie_token:
        return False

    return secrets.compare_digest(cookie_token, _USER_TOKEN) or (
        bool(_ADMIN_TOKEN) and secrets.compare_digest(cookie_token, _ADMIN_TOKEN)
    )


def _unauthorized_response() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _loggable_fit_inputs(role_title: str, company_name: Optional[str]) -> dict:
    inputs = {"role_title": role_title}
    if company_name:
        inputs["has_company_name"] = True
    return inputs


def _public_base_url(request: Request) -> str:
    if _PUBLIC_BASE_URL:
        return _PUBLIC_BASE_URL

    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        return f"{proto}://{host}"

    return str(request.base_url).rstrip("/")


# ── MCP tool logic ────────────────────────────────────────────────────────────

def get_profile() -> dict:
    """
    Returns the candidate's structured professional profile: identity, work history,
    education, and skills. Use this to understand who the candidate is.
    """
    t0 = time.monotonic()
    try:
        profile = _load_employer_profile()
    except ValueError:
        return _not_visible_response()
    result = {
        "identity": profile["identity"],
        "experience": profile["experience"],
        "education": profile["education"],
        "skills": profile["skills"],
    }
    log_tool_call("get_profile", _DEFAULT_CANDIDATE, {}, (time.monotonic() - t0) * 1000)
    return result


def get_availability() -> dict:
    """
    Returns the candidate's current search status, availability timeline, target role
    preferences, and geographic constraints. Does not include compensation details.
    """
    t0 = time.monotonic()
    try:
        profile = _load_employer_profile()
    except ValueError:
        return _not_visible_response()
    search = profile["search"]
    result = {
        "status": search["status"],
        "available_from": search.get("available_from"),
        "notice_period": search.get("notice_period"),
        "target": search["target"],
        "geography": search["geography"],
    }
    log_tool_call("get_availability", _DEFAULT_CANDIDATE, {}, (time.monotonic() - t0) * 1000)
    return result


def get_fit_signal(
    role_title: str,
    role_description: Optional[str] = None,
    company_name: Optional[str] = None,
    company_stage: Optional[str] = None,
    company_size: Optional[int] = None,
    industry: Optional[str] = None,
    location: Optional[str] = None,
    remote_policy: Optional[str] = None,
    compensation_base_min: Optional[int] = None,
    compensation_base_max: Optional[int] = None,
    seniority_level: Optional[str] = None,
    functional_area: Optional[str] = None,
    context: Optional[str] = None,
) -> dict:
    """
    Returns a calibrated fit assessment for the candidate against a described role.
    Evaluates skills match, experience fit, seniority calibration, culture alignment,
    candidate interest, search readiness, and hard constraints. Schema version 1.0.

    At minimum, provide role_title. Add known role details for a more precise signal.
    """
    t0 = time.monotonic()
    profile = load_profile(_DEFAULT_CANDIDATE)
    try:
        employer_profile = _load_employer_profile()
    except ValueError:
        return _not_visible_response()

    role = {k: v for k, v in {
        "title": role_title,
        "description": role_description,
        "company_name": company_name,
        "company_stage": company_stage,
        "company_size": company_size,
        "industry": industry,
        "location": location,
        "remote_policy": remote_policy,
        "compensation": (
            {"base_range": [compensation_base_min, compensation_base_max]}
            if compensation_base_min is not None or compensation_base_max is not None else None
        ),
        "seniority_level": seniority_level,
        "functional_area": functional_area,
        "context": context,
    }.items() if v is not None}

    signal = compute_fit_signal(employer_profile, role)
    signal["schema_version"] = "1.0"
    signal["consent"] = {
        "withheld": profile["consent"].get("withheld_fields", []),
        "profile_completeness": profile.get("completeness_score", 0.0),
    }

    log_tool_call(
        "get_fit_signal",
        _DEFAULT_CANDIDATE,
        _loggable_fit_inputs(role_title, company_name),
        (time.monotonic() - t0) * 1000,
        outcome={"signal": signal.get("overall", {}).get("signal")},
    )
    return signal


# ── Web UI routes ─────────────────────────────────────────────────────────────

async def homepage(request: Request) -> HTMLResponse:
    base = _public_base_url(request)
    mcp_url = f"{base}/mcp"
    try:
        profile = load_profile(_DEFAULT_CANDIDATE)
        identity = profile.get("identity", {})
        name = identity.get("name", _DEFAULT_CANDIDATE)
        headline = identity.get("headline", "")
    except Exception:
        name = _DEFAULT_CANDIDATE
        headline = ""
    if not _is_authenticated(request):
        return HTMLResponse(_render_landing_ui(mcp_url, name, headline))
    return HTMLResponse(_render_ui(name, _DEFAULT_CANDIDATE, mcp_url))


async def login_api(request: Request) -> JSONResponse:
    if not _USER_TOKEN:
        return JSONResponse({"status": "disabled"}, status_code=400)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    token = payload.get("token", "")
    if token not in {_USER_TOKEN, _ADMIN_TOKEN}:
        return _unauthorized_response()

    response = JSONResponse({"status": "ok"})
    response.set_cookie(
        _AUTH_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


async def logout_api(_request: Request) -> JSONResponse:
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(_AUTH_COOKIE, path="/")
    return response


async def profile_api(request: Request) -> Response:
    if not _is_authenticated(request):
        return _unauthorized_response()

    candidate_id = request.path_params["candidate_id"]
    if request.method == "GET":
        try:
            return Response(read_raw_profile(candidate_id), media_type="text/plain; charset=utf-8")
        except FileNotFoundError:
            return JSONResponse({"error": "not found"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    elif request.method == "PUT":
        if not _is_admin(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        content = (await request.body()).decode("utf-8")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as e:
            return JSONResponse({"error": f"invalid YAML: {e}"}, status_code=400)
        if not isinstance(parsed, dict) or not _PROFILE_REQUIRED_KEYS.issubset(parsed.keys()):
            missing = sorted(_PROFILE_REQUIRED_KEYS - set(parsed.keys() if isinstance(parsed, dict) else []))
            return JSONResponse({"error": f"profile missing required keys: {missing}"}, status_code=400)
        try:
            save_raw_profile(candidate_id, content)
            return JSONResponse({"status": "saved"})
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    else:
        return Response(status_code=405)


async def logs_api(_request: Request) -> JSONResponse:
    if not _is_authenticated(_request):
        return _unauthorized_response()
    return JSONResponse(log_store.all_entries())


# ── App ───────────────────────────────────────────────────────────────────────

def create_app() -> Starlette:
    # This app mounts FastMCP inside Starlette and serves it through Lambda/CloudFront.
    # FastMCP's default localhost transport security is correct for local dev servers,
    # but rejects real Host headers in this deployment shape.
    mcp = FastMCP(
        "Ombud",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        instructions=(
            "This server represents Carl Moberg — a technology executive and co-founder "
            "available for CTO, CEO, VP Engineering, or co-founder roles.\n\n"
            "Recommended call sequence:\n"
            "1. mcp_get_profile — fetch identity, work history, education, and skills. "
            "Call this first to understand who the candidate is.\n"
            "2. mcp_get_availability — fetch current search status, available-from date, "
            "target roles, seniority, industries, company stage preferences, and geography. "
            "Call this to determine whether the candidate is actively looking and what they want.\n"
            "3. mcp_get_fit_signal — submit a role and receive a structured fit assessment "
            "covering skills match, experience, seniority calibration, culture alignment, "
            "candidate interest, and hard constraints. Only call this when you have a specific "
            "role to evaluate. Provide as many role details as available — especially "
            "role_description — to improve signal accuracy.\n\n"
            "All tools are read-only and require no authentication."
        ),
    )
    # streamable_http_path defaults to "/mcp" — endpoint is {base}/mcp

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def mcp_get_profile() -> dict:
        """
        Returns Carl Moberg's structured professional profile: identity (name, headline,
        location, links), full work history with scope and highlights, education, and
        a skill inventory with proficiency levels and years of experience.

        Call this first to establish who the candidate is before evaluating fit or
        checking availability.
        """
        return get_profile()

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def mcp_get_availability() -> dict:
        """
        Returns the candidate's current job search status, availability timeline,
        target role preferences, and geographic constraints.

        Includes: search status (active/passive/closed), available-from date, notice
        period, target titles and seniority levels, preferred industries, company stage
        range, company size range, preferred locations, and remote policy.

        Does not include compensation details (withheld by candidate consent).
        Call mcp_get_fit_signal to check whether a specific role's compensation is
        likely to be in range.
        """
        return get_availability()

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    def mcp_get_fit_signal(
        role_title: Annotated[str, Field(description="Job title for the role being evaluated.")],
        role_description: Annotated[Optional[str], Field(description="Full role description or JD text. Providing this significantly improves skill-match accuracy.")] = None,
        company_name: Annotated[Optional[str], Field(description="Name of the hiring company.")] = None,
        company_stage: Annotated[Optional[str], Field(description="Funding or growth stage: seed, series_a, series_b, series_c, growth, or enterprise.")] = None,
        company_size: Annotated[Optional[int], Field(description="Current headcount of the company.")] = None,
        industry: Annotated[Optional[str], Field(description="Industry or sector, e.g. infrastructure_software, developer_tools, agentic_ai, fintech, saas.")] = None,
        location: Annotated[Optional[str], Field(description="Role location, e.g. 'Stockholm, Sweden' or 'San Francisco, CA'. Use 'Remote' for fully remote.")] = None,
        remote_policy: Annotated[Optional[str], Field(description="Remote work policy: remote, hybrid, or onsite.")] = None,
        compensation_base_min: Annotated[Optional[int], Field(description="Minimum base salary offered, in USD.")] = None,
        compensation_base_max: Annotated[Optional[int], Field(description="Maximum base salary offered, in USD.")] = None,
        seniority_level: Annotated[Optional[str], Field(description="Seniority level: c_suite, vp, director, senior, mid, or junior.")] = None,
        functional_area: Annotated[Optional[str], Field(description="Functional area: engineering, product, design, sales, marketing, operations, etc.")] = None,
        context: Annotated[Optional[str], Field(description="Any additional context about the role, team, or company not captured in other fields.")] = None,
    ) -> dict:
        """
        Returns a calibrated fit assessment for the candidate against a described role.

        Evaluates: skills match, experience fit, seniority calibration, culture alignment,
        candidate interest, search readiness, and hard constraints (geography, company stage,
        equity requirements). Returns an overall signal (strong/likely/possible/poor), a
        numeric fit score, dimension-level breakdowns, blockers, strengths, gaps, and a
        recommended action (contact/explore/do_not_contact).

        At minimum provide role_title. Each additional field improves precision — especially
        role_description (skill matching), company_stage (stage fit), and location (geography
        constraints). Compensation fields are evaluated against withheld candidate preferences;
        unknown values are flagged in the constraints.unknown list rather than counted as unmet.
        """
        return get_fit_signal(
            role_title=role_title,
            role_description=role_description,
            company_name=company_name,
            company_stage=company_stage,
            company_size=company_size,
            industry=industry,
            location=location,
            remote_policy=remote_policy,
            compensation_base_min=compensation_base_min,
            compensation_base_max=compensation_base_max,
            seniority_level=seniority_level,
            functional_area=functional_area,
            context=context,
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/", homepage),
            Route("/api/login", login_api, methods=["POST"]),
            Route("/api/logout", logout_api, methods=["POST"]),
            Route("/api/profile/{candidate_id}", profile_api, methods=["GET", "PUT"]),
            Route("/api/logs", logs_api),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )


app = _SourceIpMiddleware(create_app())


def handler(event, context):
    # Set source IP from the raw Lambda event before Mangum's asyncio loop starts,
    # so the ContextVar is visible in every Task spawned within that loop.
    hdrs = event.get("headers") or {}
    forwarded = hdrs.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not ip:
        ip = ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp", "unknown")
    _source_ip.set(ip)
    return Mangum(_SourceIpMiddleware(create_app()), lifespan="on")(event, context)


# ── UI template ───────────────────────────────────────────────────────────────

def _render_ui(name: str, candidate_id: str, mcp_url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ombud — {name}</title>
<style>
:root {{
  --paper:#fcfbf7;
  --line:#e9dfcf;
  --line-strong:#d8cab2;
  --ink:#181511;
  --muted:#6c655d;
  --danger:#c3312f;
  --success:#326b2c;
  --mono:"JetBrains Mono","IBM Plex Mono","SFMono-Regular",monospace;
  --sans:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:var(--sans);min-height:100vh}}
.site-header{{
  border-bottom:1px solid var(--line);
  background:rgba(252,251,247,.92);
  backdrop-filter:blur(10px);
  position:sticky;top:0;z-index:10;
}}
.header-inner{{
  width:min(1100px,calc(100% - 32px));
  margin:0 auto;
  min-height:56px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
}}
.brand{{display:flex;align-items:center;gap:10px;font-size:.95rem;font-weight:500}}
.mark{{
  width:16px;height:16px;border-radius:999px;
  border:1px solid var(--line-strong);
  background:radial-gradient(circle at 30% 30%,#f8f3ea 0,#d7c7ac 45%,#9b8d76 100%);
}}
.header-right{{display:flex;align-items:center;gap:16px}}
.url-chip{{
  display:flex;align-items:center;gap:8px;
  border:1px solid var(--line);
  padding:5px 10px;
  font-family:var(--mono);font-size:.72rem;color:var(--muted);
}}
.chip-copy{{
  border:none;background:none;cursor:pointer;
  color:var(--muted);font-size:.72rem;font-family:var(--sans);
  padding:0;letter-spacing:.06em;text-transform:uppercase;
}}
.chip-copy:hover{{color:var(--ink)}}
.sign-out{{
  font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;
  color:var(--muted);background:none;border:none;cursor:pointer;font-family:var(--sans);
}}
.sign-out:hover{{color:var(--ink)}}
.workspace{{
  width:min(1100px,calc(100% - 32px));
  margin:0 auto;
  padding:24px 0 64px;
  display:grid;
  grid-template-columns:1fr 360px;
  gap:32px;
  align-items:start;
}}
.panel-head{{
  padding-bottom:12px;
  border-bottom:1px solid var(--line);
  display:flex;
  align-items:baseline;
  justify-content:space-between;
  gap:12px;
}}
.panel-head h2{{font-size:1rem;font-weight:600;letter-spacing:.04em}}
.panel-head .hint{{font-size:.72rem;color:var(--muted);font-family:var(--mono)}}
textarea{{
  width:100%;
  height:calc(100vh - 200px);
  min-height:480px;
  margin-top:14px;
  background:#fff;
  border:1px solid var(--line);
  padding:16px 18px;
  color:var(--ink);
  font-family:var(--mono);
  font-size:.8rem;
  line-height:1.72;
  resize:vertical;
  outline:none;
}}
textarea:focus{{border-color:var(--line-strong)}}
.save-bar{{
  display:flex;align-items:center;gap:12px;margin-top:12px;
}}
.btn{{
  padding:8px 16px;
  border:1px solid var(--line-strong);
  background:transparent;color:var(--ink);
  font-family:var(--sans);font-size:.75rem;
  letter-spacing:.08em;text-transform:uppercase;cursor:pointer;
}}
.btn:hover{{background:#f7f2e8}}
.msg{{font-size:.8rem;color:var(--muted);min-height:1.2rem}}
.msg.error{{color:var(--danger)}}
.msg.success{{color:var(--success)}}
.log-panel{{position:sticky;top:72px}}
.log-status{{
  display:flex;align-items:center;gap:6px;
  font-size:.68rem;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;
}}
.dot{{
  width:6px;height:6px;border-radius:999px;
  background:var(--success);
  animation:blink 2.4s ease-in-out infinite;
}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
table{{width:100%;margin-top:14px;border-collapse:collapse;font-size:.78rem}}
th{{
  text-align:left;padding:6px 0;
  color:var(--muted);font-weight:500;font-size:.65rem;
  text-transform:uppercase;letter-spacing:.08em;
  border-bottom:1px solid var(--line);
}}
td{{
  padding:10px 0;border-bottom:1px solid var(--line);
  font-family:var(--mono);vertical-align:middle;
}}
td:first-child{{color:var(--muted);white-space:nowrap;padding-right:12px}}
.badge{{
  display:inline-block;padding:2px 6px;
  border:1px solid var(--line);font-size:.65rem;background:#fff;
  white-space:nowrap;
}}
.strong,.likely,.possible{{color:#0b46ff}}
.poor{{color:var(--danger)}}
.empty{{color:var(--muted);text-align:center;padding:40px 0;font-size:.82rem}}
.data-row{{cursor:pointer}}
.data-row:hover td{{background:rgba(0,0,0,.02)}}
.detail-row td{{padding:0;border-bottom:1px solid var(--line)}}
.detail-inner{{
  display:none;
  padding:12px 0 16px;
  display:none;
}}
.detail-row.open .detail-inner{{display:grid;grid-template-columns:100px 1fr;gap:4px 16px}}
.dl{{color:var(--muted);font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;padding-top:3px;font-family:var(--sans)}}
.dv{{font-family:var(--mono);font-size:.75rem;white-space:pre-wrap;word-break:break-all;line-height:1.5}}
@media(max-width:780px){{
  .workspace{{grid-template-columns:1fr;gap:40px}}
  .log-panel{{position:static}}
  textarea{{height:480px}}
  .url-chip{{display:none}}
}}
</style>
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <div class="brand"><span class="mark"></span><span>{name}</span></div>
    <div class="header-right">
      <div class="url-chip">
        <span id="urlText">{mcp_url}</span>
        <button class="chip-copy" onclick="copyUrl(this)">Copy</button>
      </div>
      <button class="sign-out" onclick="logout()">Sign out</button>
    </div>
  </div>
</header>

<div class="workspace">
  <div class="editor-panel">
    <div class="panel-head">
      <h2>Profile</h2>
      <span class="hint">{candidate_id}.yaml</span>
    </div>
    <textarea id="editor" spellcheck="false"></textarea>
    <div class="save-bar">
      <button class="btn" onclick="save()">Save</button>
      <span class="msg" id="msg"></span>
    </div>
  </div>

  <div class="log-panel">
    <div class="panel-head">
      <h2>Request Log</h2>
      <div class="log-status"><span class="dot"></span><span>Live</span></div>
    </div>
    <table>
      <thead>
        <tr><th>Time</th><th>Tool</th><th>Input</th><th>Signal</th></tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<script>
const CID = {repr(candidate_id)};
const MCP_URL = {repr(mcp_url)};

async function copyUrl(btn) {{
  await navigator.clipboard.writeText(MCP_URL);
  const orig = btn.textContent;
  btn.textContent = "Copied";
  setTimeout(() => btn.textContent = orig, 1500);
}}

async function load() {{
  const r = await fetch("/api/profile/" + CID);
  const body = await r.text();
  const msg = document.getElementById("msg");
  if (!r.ok) {{
    let error = body;
    try {{ error = JSON.parse(body).error || body; }} catch(_) {{}}
    msg.textContent = "Load failed: " + error;
    msg.className = "msg error";
    return;
  }}
  document.getElementById("editor").value = body;
}}

async function save() {{
  const msg = document.getElementById("msg");
  msg.className = "msg";
  msg.textContent = "Saving…";
  try {{
    const r = await fetch("/api/profile/" + CID, {{
      method: "PUT",
      headers: {{"Content-Type": "text/plain"}},
      body: document.getElementById("editor").value,
    }});
    const j = await r.json();
    msg.textContent = r.ok ? "Saved." : "Error: " + j.error;
    msg.className = r.ok ? "msg success" : "msg error";
  }} catch(e) {{
    msg.textContent = "Error: " + e.message;
    msg.className = "msg error";
  }}
  setTimeout(() => {{ msg.textContent = ""; msg.className = "msg"; }}, 3000);
}}

async function logout() {{
  await fetch("/api/logout", {{method: "POST"}});
  window.location.reload();
}}

function ago(ts) {{
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  return Math.floor(s / 3600) + "h";
}}

async function refreshLog() {{
  let entries;
  try {{ entries = await (await fetch("/api/logs")).json(); }} catch(_) {{ return; }}
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  if (!entries.length) {{
    const td = tbody.insertRow().insertCell();
    td.colSpan = 4; td.className = "empty";
    td.textContent = "No requests yet.";
    return;
  }}
  function esc(s) {{
    return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  }}
  for (const e of entries) {{
    const tr = tbody.insertRow();
    tr.className = "data-row";
    tr.insertCell().textContent = ago(e.timestamp);
    const tdT = tr.insertCell();
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = e.tool.replace("get_", "");
    tdT.appendChild(badge);
    tr.insertCell().textContent = (e.inputs && (e.inputs.role_title || e.inputs.company_name)) || "—";
    const tdS = tr.insertCell();
    const sig = (e.outcome && e.outcome.signal) || "—";
    tdS.textContent = sig;
    if (e.outcome && e.outcome.signal) tdS.className = e.outcome.signal;

    const detailTr = tbody.insertRow();
    detailTr.className = "detail-row";
    const detailTd = detailTr.insertCell();
    detailTd.colSpan = 4;
    const ts = new Date((e.timestamp || 0) * 1000).toISOString();
    const inputsJson = JSON.stringify(e.inputs || {{}}, null, 2);
    const outcomeJson = JSON.stringify(e.outcome || {{}}, null, 2);
    detailTd.innerHTML = `<div class="detail-inner">
      <span class="dl">Time</span><span class="dv">${{esc(ts)}}</span>
      <span class="dl">Source IP</span><span class="dv">${{esc(e.source_ip || "—")}}</span>
      <span class="dl">Inputs</span><span class="dv">${{esc(inputsJson)}}</span>
      <span class="dl">Outcome</span><span class="dv">${{esc(outcomeJson)}}</span>
    </div>`;
    tr.addEventListener("click", () => detailTr.classList.toggle("open"));
  }}
}}

load();
refreshLog();
setInterval(refreshLog, 5000);
</script>
</body>
</html>"""


def _render_landing_ui(mcp_url: str, name: str, headline: str) -> str:
    # Build snippets outside f-string to avoid brace-escaping chaos
    snip_other = (
        '{\n'
        '  "mcpServers": {\n'
        '    "ombud": {\n'
        '      "type": "http",\n'
        f'      "url": "{mcp_url}"\n'
        '    }\n'
        '  }\n'
        '}'
    )
    snip_vscode = (
        '{\n'
        '  "servers": {\n'
        '    "ombud": {\n'
        '      "type": "http",\n'
        f'      "url": "{mcp_url}"\n'
        '    }\n'
        '  }\n'
        '}'
    )
    snip_cursor = (
        '{\n'
        '  "mcpServers": {\n'
        '    "ombud": {\n'
        f'      "url": "{mcp_url}"\n'
        '    }\n'
        '  }\n'
        '}'
    )
    snip_claudecode = f'claude mcp add --transport http ombud {mcp_url}'
    snip_desktop = (
        '{\n'
        '  "mcpServers": {\n'
        '    "ombud": {\n'
        '      "command": "npx",\n'
        '      "args": ["mcp-remote", '
        f'"{mcp_url}"]\n'
        '    }\n'
        '  }\n'
        '}'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ombud — {name}</title>
<style>
:root {{
  --paper:#fcfbf7;
  --line:#e9dfcf;
  --line-strong:#d8cab2;
  --ink:#181511;
  --muted:#6c655d;
  --link:#0b46ff;
  --danger:#c3312f;
  --success:#326b2c;
  --mono:"JetBrains Mono","IBM Plex Mono","SFMono-Regular",monospace;
  --sans:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:var(--sans);min-height:100vh}}
a{{color:var(--link);text-decoration:none}}
a:hover{{text-decoration:underline}}
.site-header{{
  border-bottom:1px solid var(--line);
  background:rgba(252,251,247,.92);
  backdrop-filter:blur(10px);
  position:sticky;top:0;z-index:10;
}}
.header-inner{{
  width:min(760px,calc(100% - 40px));margin:0 auto;
  min-height:56px;display:flex;align-items:center;justify-content:space-between;gap:16px;
}}
.brand{{display:flex;align-items:center;gap:10px;font-size:.9rem;font-weight:500}}
.mark{{
  width:16px;height:16px;border-radius:999px;flex-shrink:0;
  border:1px solid var(--line-strong);
  background:radial-gradient(circle at 30% 30%,#f8f3ea 0,#d7c7ac 45%,#9b8d76 100%);
}}
.admin-link{{
  font-size:.72rem;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;
  cursor:pointer;background:none;border:none;font-family:var(--sans);padding:0;
}}
.admin-link:hover{{color:var(--ink)}}
.page{{width:min(760px,calc(100% - 40px));margin:0 auto;padding:52px 0 88px}}
.identity{{margin-bottom:44px;padding-bottom:36px;border-bottom:1px solid var(--line)}}
.identity h1{{font-size:2rem;font-weight:500;line-height:1.15;letter-spacing:-.02em;margin-bottom:10px}}
.identity .hl{{color:var(--muted);font-size:.9rem;line-height:1.5}}
.section{{margin-bottom:44px;padding-bottom:44px;border-bottom:1px solid var(--line)}}
.section:last-of-type{{border-bottom:none;margin-bottom:0;padding-bottom:0}}
.section-label{{
  font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted);margin-bottom:14px;font-weight:500;
}}
.prose{{font-size:.95rem;line-height:1.7;color:var(--ink);max-width:600px}}
.prose + .prose{{margin-top:12px}}
.prose strong{{font-weight:600}}
.endpoint-row{{
  display:flex;align-items:stretch;
  border:1px solid var(--line);background:#fff;max-width:560px;margin-top:18px;
}}
.endpoint-url{{
  flex:1;padding:11px 14px;font-family:var(--mono);font-size:.83rem;color:var(--ink);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}}
.copy-btn{{
  padding:0 15px;border:none;border-left:1px solid var(--line);
  background:transparent;cursor:pointer;font-family:var(--sans);
  font-size:.7rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
  white-space:nowrap;flex-shrink:0;transition:color .15s;
}}
.copy-btn:hover{{background:#f7f2e8;color:var(--ink)}}
.copy-btn.ok{{color:var(--success)}}
.client-tabs{{display:flex;gap:0;border-bottom:1px solid var(--line);margin-bottom:0;max-width:560px}}
.client-tab{{
  padding:8px 16px;font-size:.78rem;cursor:pointer;
  color:var(--muted);background:none;border:none;border-bottom:2px solid transparent;
  font-family:var(--sans);letter-spacing:.04em;margin-bottom:-1px;
}}
.client-tab.active{{color:var(--ink);border-bottom-color:var(--ink)}}
.client-pane{{display:none;max-width:560px}}
.client-pane.active{{display:block}}
.pane-hint{{
  font-size:.75rem;color:var(--muted);padding:10px 0 8px;
  font-family:var(--mono);
}}
.code-wrap{{position:relative}}
pre.code-block{{
  padding:15px 18px;background:#fff;border:1px solid var(--line);
  font-family:var(--mono);font-size:.78rem;line-height:1.75;
  color:var(--ink);white-space:pre;overflow-x:auto;
  padding-right:72px;
}}
.code-copy{{
  position:absolute;top:8px;right:8px;
  padding:4px 10px;border:1px solid var(--line);background:var(--paper);
  font-size:.68rem;letter-spacing:.07em;text-transform:uppercase;
  color:var(--muted);cursor:pointer;font-family:var(--sans);
}}
.code-copy:hover{{border-color:var(--line-strong);color:var(--ink)}}
.code-copy.ok{{color:var(--success)}}
.admin-panel{{
  margin-top:44px;padding-top:36px;border-top:1px solid var(--line);
  display:none;max-width:380px;
}}
.admin-panel.open{{display:block}}
.admin-panel h2{{font-size:1rem;font-weight:500;margin-bottom:6px}}
.admin-panel p{{font-size:.85rem;color:var(--muted);margin-bottom:16px;line-height:1.5}}
input[type=password]{{
  width:100%;background:#fff;border:1px solid var(--line);
  padding:10px 12px;color:var(--ink);font-family:var(--mono);font-size:.82rem;outline:none;display:block;
}}
input[type=password]:focus{{border-color:var(--line-strong)}}
.btn{{
  margin-top:10px;padding:9px 16px;
  border:1px solid var(--line-strong);background:transparent;color:var(--ink);
  font-family:var(--sans);font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;
}}
.btn:hover{{background:#f7f2e8}}
.msg{{min-height:1.2rem;font-size:.8rem;margin-top:10px;color:var(--muted)}}
.msg.error{{color:var(--danger)}}
footer{{border-top:1px solid var(--line)}}
.footer-inner{{
  width:min(760px,calc(100% - 40px));margin:0 auto;
  padding:16px 0 28px;display:flex;justify-content:space-between;
  color:var(--muted);font-size:.72rem;
}}
@media(max-width:540px){{
  .identity h1{{font-size:1.5rem}}
  .client-tab{{padding:8px 10px;font-size:.72rem}}
}}
</style>
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <div class="brand"><span class="mark"></span><span>Ombud</span></div>
    <button class="admin-link" onclick="toggleAdmin()">Admin</button>
  </div>
</header>

<main class="page">

  <div class="identity">
    <h1>{name}</h1>
  </div>

  <div class="section">
    <div class="section-label">What this is</div>
    <p class="prose">
      This page is an MCP server. If your recruiting application or AI assistant speaks the
      Model Context Protocol, you can connect it here and query {name.split()[0]}'s professional
      profile directly: who he is, what he's looking for, and whether he's a fit for a specific role.
      The server responds with structured data and a calibrated fit signal.
    </p>
    <p class="prose">
      The endpoint is public. No API key or account needed to read the profile.
    </p>
  </div>

  <div class="section">
    <div class="section-label">Connect your client</div>
    <div class="client-tabs">
      <button class="client-tab active" onclick="switchTab(this,'tab-vscode')">VS Code</button>
      <button class="client-tab" onclick="switchTab(this,'tab-claude-code')">Claude Code</button>
      <button class="client-tab" onclick="switchTab(this,'tab-cursor')">Cursor</button>
      <button class="client-tab" onclick="switchTab(this,'tab-claude-desktop')">Claude Desktop</button>
      <button class="client-tab" onclick="switchTab(this,'tab-other')">Other</button>
    </div>

    <div id="tab-vscode" class="client-pane active">
      <p class="pane-hint">.vscode/mcp.json</p>
      <div class="code-wrap">
        <pre class="code-block">{snip_vscode}</pre>
        <button class="code-copy" onclick="copyText(SNIPS.vscode, this)">Copy</button>
      </div>
    </div>

    <div id="tab-claude-code" class="client-pane">
      <p class="pane-hint">Run in your terminal</p>
      <div class="code-wrap">
        <pre class="code-block">{snip_claudecode}</pre>
        <button class="code-copy" onclick="copyText(SNIPS.claudecode, this)">Copy</button>
      </div>
    </div>

    <div id="tab-cursor" class="client-pane">
      <p class="pane-hint">~/.cursor/mcp.json</p>
      <div class="code-wrap">
        <pre class="code-block">{snip_cursor}</pre>
        <button class="code-copy" onclick="copyText(SNIPS.cursor, this)">Copy</button>
      </div>
    </div>

    <div id="tab-claude-desktop" class="client-pane">
      <p class="pane-hint">~/Library/Application Support/Claude/claude_desktop_config.json — requires Node.js 18+</p>
      <div class="code-wrap">
        <pre class="code-block">{snip_desktop}</pre>
        <button class="code-copy" onclick="copyText(SNIPS.desktop, this)">Copy</button>
      </div>
    </div>

    <div id="tab-other" class="client-pane">
      <p class="pane-hint">Add to your MCP client's server configuration (Streamable HTTP transport)</p>
      <div class="code-wrap">
        <pre class="code-block">{snip_other}</pre>
        <button class="code-copy" onclick="copyText(SNIPS.other, this)">Copy</button>
      </div>
    </div>
  </div>

  <div class="admin-panel" id="adminPanel">
    <h2>Admin sign in</h2>
    <p>Profile editing requires an access token.</p>
    <input type="password" id="token" placeholder="Access token" autocomplete="current-password">
    <button class="btn" onclick="login()">Continue</button>
    <div class="msg" id="msg"></div>
  </div>

</main>

<footer>
  <div class="footer-inner">
    <span>Ombud — candidate MCP server</span>
    <span>{mcp_url}</span>
  </div>
</footer>

<script>
const SNIPS = {{
  vscode: {repr(snip_vscode)},
  claudecode: {repr(snip_claudecode)},
  cursor: {repr(snip_cursor)},
  desktop: {repr(snip_desktop)},
  other: {repr(snip_other)},
}};
function switchTab(btn, id) {{
  document.querySelectorAll(".client-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".client-pane").forEach(p => p.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById(id).classList.add("active");
}}
function copyText(text, elOrId) {{
  const el = typeof elOrId === "string" ? document.getElementById(elOrId) : elOrId;
  navigator.clipboard.writeText(text).then(() => {{
    const orig = el.textContent;
    el.textContent = "Copied";
    el.classList.add("ok");
    setTimeout(() => {{ el.textContent = orig; el.classList.remove("ok"); }}, 2000);
  }});
}}
function toggleAdmin() {{
  const p = document.getElementById("adminPanel");
  p.classList.toggle("open");
  if (p.classList.contains("open")) document.getElementById("token").focus();
}}
document.addEventListener("keydown", e => {{
  if (e.key === "Enter" && document.getElementById("adminPanel").classList.contains("open")) login();
}});
async function login() {{
  const msg = document.getElementById("msg");
  msg.className = "msg";
  msg.textContent = "Signing in…";
  const token = document.getElementById("token").value;
  try {{
    const r = await fetch("/api/login", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{token}}),
    }});
    const j = await r.json();
    if (!r.ok) {{ msg.textContent = j.error || "unauthorized"; msg.className = "msg error"; return; }}
    window.location.reload();
  }} catch(e) {{ msg.textContent = e.message; msg.className = "msg error"; }}
}}
</script>
</body>
</html>"""
