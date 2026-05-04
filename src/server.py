import contextlib
import os
import secrets
import time
from typing import Optional

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
from logger import log_tool_call
from profile import apply_withheld, load_profile, read_raw_profile, save_raw_profile

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
    if not _is_authenticated(request):
        return HTMLResponse(_render_login_ui(), status_code=401)
    try:
        profile = load_profile(_DEFAULT_CANDIDATE)
        name = profile.get("identity", {}).get("name", _DEFAULT_CANDIDATE)
    except Exception:
        name = _DEFAULT_CANDIDATE
    base = _public_base_url(request)
    return HTMLResponse(_render_ui(name, _DEFAULT_CANDIDATE, f"{base}/mcp"))


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
        auth = request.headers.get("Authorization", "")
        if not _ADMIN_TOKEN or auth != f"Bearer {_ADMIN_TOKEN}":
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
    )
    # streamable_http_path defaults to "/mcp" — endpoint is {base}/mcp

    @mcp.tool()
    def mcp_get_profile() -> dict:
        return get_profile()

    @mcp.tool()
    def mcp_get_availability() -> dict:
        return get_availability()

    @mcp.tool()
    def mcp_get_fit_signal(
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


app = create_app()


def handler(event, context):
    return Mangum(create_app(), lifespan="on")(event, context)


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
  --panel:#f5efe4;
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
body{{
  background:var(--paper);
  color:var(--ink);
  font-family:var(--sans);
  min-height:100vh;
}}
a{{color:var(--link);text-decoration:none}}
a:hover{{text-decoration:underline}}
.site-header{{
  border-bottom:1px solid var(--line);
  background:rgba(252,251,247,.92);
  backdrop-filter:blur(10px);
}}
.header-inner{{
  width:min(860px,calc(100% - 32px));
  margin:0 auto;
  min-height:60px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
}}
.brand{{
  display:flex;
  align-items:center;
  gap:10px;
  font-size:.95rem;
  font-weight:500;
}}
.mark{{
  width:18px;
  height:18px;
  border-radius:999px;
  border:1px solid var(--line-strong);
  background:radial-gradient(circle at 30% 30%, #f8f3ea 0, #d7c7ac 45%, #9b8d76 100%);
  box-shadow:0 1px 0 rgba(255,255,255,.9) inset;
}}
.top-link{{
  font-size:.75rem;
  color:var(--muted);
  letter-spacing:.08em;
  text-transform:uppercase;
}}
.page{{
  width:min(860px,calc(100% - 32px));
  margin:18px auto 48px;
}}
.tabs{{
  margin:0 0 0 120px;
  display:flex;
  gap:18px;
}}
.tabs button{{
  border:none;
  background:none;
  padding:0 0 12px;
  color:var(--muted);
  font:inherit;
  font-size:.78rem;
  letter-spacing:.08em;
  text-transform:uppercase;
  cursor:pointer;
}}
.tabs button.active{{
  color:var(--ink);
  text-decoration:underline;
  text-decoration-color:var(--line-strong);
  text-underline-offset:6px;
}}
.pane{{display:none}}
.pane.active{{display:block}}
.editor-wrap,.log-wrap{{
  margin-left:120px;
  max-width:600px;
}}
.mcp-wrap{{
  margin-left:120px;
  max-width:600px;
  padding-top:22px;
}}
.editor-meta{{
  display:flex;
  justify-content:space-between;
  gap:16px;
  align-items:flex-end;
  padding:22px 0 10px;
  border-bottom:1px solid var(--line);
}}
.editor-meta h2,.log-head h2{{
  font-size:1.65rem;
  font-weight:500;
  line-height:1.1;
}}
.editor-meta p,.log-head p{{
  margin-top:8px;
  color:var(--muted);
  font-size:.9rem;
  line-height:1.5;
}}
.meta-code{{
  color:var(--muted);
  font-size:.7rem;
  line-height:1.5;
  text-transform:uppercase;
  letter-spacing:.08em;
}}
textarea{{
  width:100%;
  height:640px;
  margin-top:20px;
  background:#fff;
  border:1px solid var(--line);
  padding:18px 20px;
  color:var(--ink);
  font-family:var(--mono);
  font-size:.8rem;
  line-height:1.72;
  resize:vertical;
  outline:none;
  box-shadow:0 1px 0 rgba(255,255,255,.9) inset;
}}
textarea:focus{{border-color:var(--line-strong)}}
input[type=password]{{
  width:220px;
  background:#fff;
  border:1px solid var(--line);
  padding:10px 12px;
  color:var(--ink);
  font-family:var(--mono);
  font-size:.78rem;
  outline:none;
}}
input[type=password]:focus{{border-color:var(--line-strong)}}
input[type=password]::placeholder{{color:#9b938a}}
.bar{{
  display:flex;
  flex-wrap:wrap;
  align-items:center;
  gap:12px;
  margin-top:14px;
}}
.btn{{
  padding:9px 16px;
  border:1px solid var(--line-strong);
  background:transparent;
  color:var(--ink);
  font-family:var(--sans);
  font-size:.78rem;
  letter-spacing:.08em;
  text-transform:uppercase;
  cursor:pointer;
}}
.btn:hover{{background:#f7f2e8}}
.btn-link{{border-color:transparent;color:var(--muted)}}
.msg{{font-size:.82rem;color:var(--muted);min-height:1.2rem}}
.msg.error{{color:var(--danger)}}
.msg.success{{color:var(--success)}}
.log-wrap{{padding-top:22px}}
.mcp-head{{
  padding-bottom:12px;
  border-bottom:1px solid var(--line);
}}
.mcp-head h2{{
  font-size:1.65rem;
  font-weight:500;
  line-height:1.1;
}}
.mcp-head p{{
  margin-top:8px;
  color:var(--muted);
  font-size:.9rem;
  line-height:1.5;
}}
.mcp-card{{
  margin-top:14px;
  padding:18px 20px;
  border:1px solid var(--line);
  background:linear-gradient(180deg, rgba(246,239,227,.8), rgba(242,233,218,.95));
}}
.mcp-card + .mcp-card{{margin-top:12px}}
.mcp-card h3{{
  font-size:.78rem;
  font-weight:600;
  letter-spacing:.08em;
  text-transform:uppercase;
  color:var(--muted);
  margin-bottom:10px;
}}
.mcp-card p,.mcp-card li{{
  font-size:.9rem;
  line-height:1.6;
}}
.mcp-card ul{{
  padding-left:18px;
}}
.mcp-card code,.meta-code code{{
  font-family:var(--mono);
  font-size:.78rem;
}}
.endpoint-box{{
  display:block;
  width:100%;
  padding:12px 14px;
  border:1px solid var(--line);
  background:#fff;
  color:var(--ink);
  overflow:auto;
}}
pre.endpoint-box{{
  white-space:pre-wrap;
  word-break:break-word;
}}
.log-head{{
  padding-bottom:12px;
  border-bottom:1px solid var(--line);
}}
.log-status{{
  margin-top:8px;
  display:flex;
  align-items:center;
  gap:8px;
  color:var(--muted);
  font-size:.72rem;
  letter-spacing:.06em;
  text-transform:uppercase;
}}
.dot{{
  display:inline-block;
  width:7px;
  height:7px;
  background:var(--success);
  border-radius:999px;
  animation:blink 2.4s ease-in-out infinite;
}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
table{{
  width:100%;
  margin-top:14px;
  border-collapse:collapse;
  font-size:.8rem;
}}
th{{
  text-align:left;
  padding:8px 0;
  color:var(--muted);
  font-weight:500;
  font-size:.68rem;
  text-transform:uppercase;
  letter-spacing:.08em;
  border-bottom:1px solid var(--line);
}}
td{{
  padding:14px 0;
  border-bottom:1px solid var(--line);
  font-family:var(--mono);
  vertical-align:middle;
}}
.badge{{
  display:inline-block;
  padding:2px 7px;
  border:1px solid var(--line);
  font-size:.68rem;
  background:#fff;
}}
.strong,.likely,.possible{{color:var(--link)}}
.poor{{color:var(--danger)}}
.empty{{color:var(--muted);text-align:center;padding:52px 0;font-size:.84rem}}
footer{{
  margin-top:72px;
  border-top:1px solid var(--line);
}}
.footer-inner{{
  width:min(860px,calc(100% - 32px));
  margin:0 auto;
  padding:18px 0 28px;
  display:flex;
  justify-content:space-between;
  gap:16px;
  color:var(--muted);
  font-size:.72rem;
}}
@media (max-width: 760px) {{
  .tabs,.editor-wrap,.log-wrap,.mcp-wrap{{margin-left:0;max-width:none}}
  .header-inner,.footer-inner{{width:min(100% - 24px, 680px)}}
  .page{{width:min(100% - 24px, 680px);margin-top:12px}}
  .editor-meta{{display:block}}
  textarea{{height:520px}}
  input[type=password]{{width:100%}}
}}
</style>
</head>
<body>
<header class="site-header">
  <div class="header-inner">
    <div class="brand"><span class="mark"></span><span>{name}</span></div>
    <a class="top-link" href="#" onclick="logout(); return false;">Sign out</a>
  </div>
</header>

<main class="page">
  <nav class="tabs">
    <button class="active" data-tab="profile" onclick="show(this)">Profile</button>
    <button data-tab="mcp" onclick="show(this)">MCP Endpoint</button>
    <button data-tab="log" onclick="show(this)">Request Log</button>
  </nav>

  <section id="profile" class="pane active">
    <div class="editor-wrap">
      <div class="editor-meta">
        <div>
          <h2>Candidate profile</h2>
          <p>Edit the source YAML directly. The MCP tools and fit engine read from this profile.</p>
        </div>
        <div class="meta-code">Source:<br>{candidate_id}.yaml</div>
      </div>
      <textarea id="editor" spellcheck="false"></textarea>
      <div class="bar">
        <button class="btn" onclick="save()">Save</button>
        <input type="password" id="token" placeholder="Admin token" autocomplete="current-password">
        <button class="btn btn-link" onclick="logout()">Sign out</button>
        <span class="msg" id="msg"></span>
      </div>
    </div>
  </section>

  <section id="mcp" class="pane">
    <div class="mcp-wrap">
      <div class="mcp-head">
        <h2>MCP endpoint</h2>
        <p>Use this endpoint from an MCP-compatible client to inspect the profile, read availability, and request fit signals.</p>
      </div>
      <div class="mcp-card">
        <h3>Endpoint URL</h3>
        <code class="endpoint-box">{mcp_url}</code>
      </div>
      <div class="mcp-card">
        <h3>How to connect</h3>
        <ul>
          <li>Configure your MCP client to use the Streamable HTTP transport.</li>
          <li>Point it at <code>{mcp_url}</code>.</li>
          <li>No browser login is required for MCP tool calls.</li>
        </ul>
      </div>
      <div class="mcp-card">
        <h3>Local .mcp.json example</h3>
        <p>Keep this file local to your machine. It is intended as client configuration, not repository state.</p>
        <pre class="endpoint-box">{{
  "mcpServers": {{
    "ombud": {{
      "type": "http",
      "url": "{mcp_url}"
    }}
  }}
}}</pre>
      </div>
      <div class="mcp-card">
        <h3>Available tools</h3>
        <ul>
          <li><code>get_profile</code> for identity, experience, education, and skills</li>
          <li><code>get_availability</code> for search status, timing, target roles, and geography</li>
          <li><code>get_fit_signal</code> for deterministic role-fit evaluation</li>
        </ul>
      </div>
    </div>
  </section>

  <section id="log" class="pane">
    <div class="log-wrap">
      <div class="log-head">
        <h2>Request log</h2>
        <p>Recent MCP traffic captured in-memory for this running instance.</p>
        <div class="log-status"><span class="dot"></span><span>Live refresh every 5 seconds</span></div>
      </div>
      <table>
        <thead>
          <tr><th>Time</th><th>Tool</th><th>Input</th><th>Outcome</th><th>ms</th></tr>
        </thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </section>
</main>

<footer>
  <div class="footer-inner">
    <div>Ombud</div>
    <div><a href="{mcp_url}">{mcp_url}</a></div>
  </div>
</footer>

<script>
const CID = {repr(candidate_id)};

function show(btn) {{
  document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".pane").forEach(p => p.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById(btn.dataset.tab).classList.add("active");
  if (btn.dataset.tab === "log") refreshLog();
}}

async function load() {{
  const r = await fetch("/api/profile/" + CID);
  const body = await r.text();
  if (!r.ok) {{
    const msg = document.getElementById("msg");
    let error = body;
    try {{
      error = JSON.parse(body).error || body;
    }} catch (_err) {{}}
    document.getElementById("editor").value = "";
    msg.textContent = "Load failed: " + error;
    return;
  }}
  document.getElementById("editor").value = body;
}}

async function save() {{
  const msg = document.getElementById("msg");
  const token = document.getElementById("token").value;
  sessionStorage.setItem("ombud_token", token);
  msg.className = "msg";
  msg.textContent = "Saving…";
  try {{
    const r = await fetch("/api/profile/" + CID, {{
      method: "PUT",
      headers: {{"Content-Type": "text/plain", "Authorization": "Bearer " + token}},
      body: document.getElementById("editor").value,
    }});
    const j = await r.json();
    msg.textContent = r.ok ? "Saved." : "Error: " + j.error;
    msg.className = r.ok ? "msg success" : "msg error";
  }} catch(e) {{
    msg.textContent = "Error: " + e.message;
    msg.className = "msg error";
  }}
  setTimeout(() => msg.textContent = "", 3000);
}}

async function logout() {{
  await fetch("/api/logout", {{method: "POST"}});
  sessionStorage.removeItem("ombud_token");
  window.location.reload();
}}

function ago(ts) {{
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  return Math.floor(s / 3600) + "h ago";
}}

async function refreshLog() {{
  const entries = await (await fetch("/api/logs")).json();
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  if (!entries.length) {{
    const tr = tbody.insertRow();
    const td = tr.insertCell();
    td.colSpan = 5;
    td.className = "empty";
    td.textContent = "No requests yet.";
    return;
  }}
  for (const e of entries) {{
    const tr = tbody.insertRow();

    tr.insertCell().textContent = ago(e.timestamp);

    const tdTool = tr.insertCell();
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = e.tool;
    tdTool.appendChild(badge);

    tr.insertCell().textContent = (e.inputs && (e.inputs.role_title || e.inputs.company_name)) || "—";

    const tdSig = tr.insertCell();
    const sig = (e.outcome && e.outcome.signal) || "—";
    tdSig.textContent = sig;
    if (e.outcome && e.outcome.signal) tdSig.className = e.outcome.signal;

    tr.insertCell().textContent = e.duration_ms;
  }}
}}

const saved = sessionStorage.getItem("ombud_token");
if (saved) document.getElementById("token").value = saved;

load();
refreshLog();
setInterval(refreshLog, 5000);
</script>
</body>
</html>"""


def _render_login_ui() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ombud Login</title>
<style>
:root {
  --paper:#fcfbf7;
  --panel:#f5efe4;
  --line:#e9dfcf;
  --line-strong:#d8cab2;
  --ink:#181511;
  --muted:#6c655d;
  --link:#0b46ff;
  --danger:#c3312f;
  --mono:"JetBrains Mono","IBM Plex Mono","SFMono-Regular",monospace;
  --sans:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  min-height:100vh;
  background:var(--paper);
  color:var(--ink);
  font-family:var(--sans);
  padding:24px;
}
.shell{
  width:min(860px,100%);
  margin:0 auto;
}
.site-header{
  min-height:60px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  border-bottom:1px solid var(--line);
}
.brand{
  display:flex;
  align-items:center;
  gap:10px;
  font-size:.95rem;
  font-weight:500;
}
.mark{
  width:18px;
  height:18px;
  border-radius:999px;
  border:1px solid var(--line-strong);
  background:radial-gradient(circle at 30% 30%, #f8f3ea 0, #d7c7ac 45%, #9b8d76 100%);
}
.card{
  width:min(600px,100%);
  margin:18px 0 0 120px;
  padding:18px 20px;
  background:linear-gradient(180deg, rgba(246,239,227,.8), rgba(242,233,218,.95));
  border:1px solid var(--line);
}
h1{
  font-size:1.65rem;
  font-weight:500;
  margin-bottom:8px;
}
p{font-size:.95rem;line-height:1.5;color:var(--muted);margin-bottom:18px}
input{
  width:100%;
  background:#fff;
  border:1px solid var(--line);
  padding:12px 14px;
  color:var(--ink);
  font-family:var(--mono);
  font-size:.85rem;
  outline:none;
}
input:focus{border-color:var(--line-strong)}
button{
  margin-top:12px;
  padding:9px 16px;
  border:1px solid var(--line-strong);
  background:transparent;
  color:var(--ink);
  font-size:.78rem;
  letter-spacing:.08em;
  text-transform:uppercase;
  cursor:pointer;
}
.msg{min-height:1.2rem;font-size:.82rem;margin-top:12px;color:var(--muted)}
.msg.error{color:var(--danger)}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
@media (max-width: 760px) {
  .card{margin-left:0}
}
</style>
</head>
<body>
  <div class="shell">
    <div class="site-header">
      <div class="brand"><span class="mark"></span><span>Ombud</span></div>
      <div style="font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;">Login</div>
    </div>
    <div class="card">
      <h1>Enter access token</h1>
      <p>This editor is protected. Use the token configured for the deployed profile to review and update the candidate YAML.</p>
      <input type="password" id="token" placeholder="Access token" autocomplete="current-password">
      <button onclick="login()">Continue</button>
      <div class="msg" id="msg"></div>
    </div>
  </div>
<script>
async function login() {
  const msg = document.getElementById("msg");
  msg.className = "msg";
  msg.textContent = "Signing in…";
  const token = document.getElementById("token").value;
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({token}),
    });
    const j = await r.json();
    if (!r.ok) {
      msg.textContent = "Error: " + (j.error || "unauthorized");
      msg.className = "msg error";
      return;
    }
    window.location.reload();
  } catch (e) {
    msg.textContent = "Error: " + e.message;
    msg.className = "msg error";
  }
}
</script>
</body>
</html>"""
