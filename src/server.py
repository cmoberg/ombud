import contextlib
import os
import secrets
import time
from typing import Optional

import yaml
from mangum import Mangum
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

import log_store
from fit_engine import compute_fit_signal
from logger import log_tool_call
from profile import apply_withheld, load_profile, read_raw_profile, save_raw_profile

_DEFAULT_CANDIDATE = os.environ.get("CANDIDATE_ID", "cmoberg")
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
_USER_TOKEN = os.environ.get("USER_TOKEN") or _ADMIN_TOKEN
_AUTH_COOKIE = "ombud_auth"

_PROFILE_REQUIRED_KEYS = {"identity", "experience", "education", "skills", "search", "culture", "consent"}

mcp = FastMCP("Ombud — Carl Moberg", stateless_http=True, json_response=True)
# streamable_http_path defaults to "/mcp" — endpoint is {base}/mcp


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


# ── MCP tools ────────────────────────────────────────────────────────────────

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
    base = str(request.base_url).rstrip("/")
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

@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp.session_manager.run():
        yield


app = Starlette(
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

handler = Mangum(app, lifespan="on")


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
  --bg:#0f0f0f; --surface:#1a1a1a; --border:#272727;
  --text:#e0e0e0; --muted:#555; --accent:#4ade80;
  --warn:#fbbf24; --danger:#f87171;
  --mono:"JetBrains Mono","Fira Code","Cascadia Code",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans)}}
header{{
  padding:18px 28px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
}}
header h1{{font-size:.75rem;font-weight:700;letter-spacing:.12em;color:var(--accent)}}
header code{{font-family:var(--mono);font-size:.75rem;color:var(--muted)}}
nav{{display:flex;border-bottom:1px solid var(--border);padding:0 28px}}
nav button{{
  padding:10px 18px;font-size:.8125rem;cursor:pointer;
  color:var(--muted);background:none;border:none;
  border-bottom:2px solid transparent;font-family:var(--sans);
}}
nav button.active{{color:var(--text);border-bottom-color:var(--accent)}}
.pane{{display:none;padding:28px}}.pane.active{{display:block}}
.editor-wrap{{max-width:840px}}
textarea{{
  width:100%;height:640px;background:var(--surface);border:1px solid var(--border);
  border-radius:6px;padding:16px;color:var(--text);
  font-family:var(--mono);font-size:.8rem;line-height:1.65;
  resize:vertical;outline:none;
}}
textarea:focus{{border-color:#3a3a3a}}
input[type=password]{{
  background:var(--surface);border:1px solid var(--border);border-radius:4px;
  padding:6px 10px;color:var(--text);font-family:var(--mono);font-size:.8rem;
  outline:none;width:220px;
}}
input[type=password]:focus{{border-color:#3a3a3a}}
input[type=password]::placeholder{{color:var(--muted)}}
.bar{{display:flex;align-items:center;gap:14px;margin-top:14px}}
.btn{{
  padding:7px 18px;border-radius:4px;font-size:.8125rem;cursor:pointer;
  font-family:var(--sans);border:none;background:var(--accent);
  color:#000;font-weight:600;
}}
.btn:hover{{opacity:.88}}
.msg{{font-size:.8rem;color:var(--muted)}}
.log-bar{{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}}
.log-bar span{{font-size:.75rem;color:var(--muted)}}
.dot{{
  display:inline-block;width:6px;height:6px;background:var(--accent);
  border-radius:50%;animation:blink 2.4s ease-in-out infinite;margin-right:6px;
}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
table{{width:100%;border-collapse:collapse;font-size:.8rem}}
th{{
  text-align:left;padding:7px 12px;color:var(--muted);font-weight:500;
  font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;
  border-bottom:1px solid var(--border);
}}
td{{padding:9px 12px;border-bottom:1px solid var(--border);font-family:var(--mono);vertical-align:middle}}
tr:hover td{{background:var(--surface)}}
.badge{{
  display:inline-block;padding:2px 8px;border-radius:3px;
  background:var(--surface);border:1px solid var(--border);
  font-size:.7rem;font-family:var(--mono);
}}
.strong{{color:var(--accent)}}.likely{{color:#86efac}}
.possible{{color:var(--warn)}}.poor{{color:var(--danger)}}
.empty{{color:var(--muted);text-align:center;padding:52px;font-size:.8rem}}
</style>
</head>
<body>
<header>
  <h1>OMBUD</h1>
  <code>MCP → {mcp_url}</code>
</header>
<nav>
  <button class="active" data-tab="profile" onclick="show(this)">Profile</button>
  <button data-tab="log" onclick="show(this)">Request Log</button>
</nav>

<div id="profile" class="pane active">
  <div class="editor-wrap">
    <textarea id="editor" spellcheck="false"></textarea>
    <div class="bar">
      <button class="btn" onclick="save()">Save</button>
      <input type="password" id="token" placeholder="Admin token" autocomplete="current-password">
      <span class="msg" id="msg"></span>
    </div>
  </div>
</div>

<div id="log" class="pane">
  <div class="log-bar">
    <span><span class="dot"></span>live · refreshes every 5s</span>
  </div>
  <table>
    <thead>
      <tr><th>Time</th><th>Tool</th><th>Input</th><th>Outcome</th><th>ms</th></tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

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
  document.getElementById("editor").value = await r.text();
}}

async function save() {{
  const msg = document.getElementById("msg");
  const token = document.getElementById("token").value;
  sessionStorage.setItem("ombud_token", token);
  msg.textContent = "Saving…";
  try {{
    const r = await fetch("/api/profile/" + CID, {{
      method: "PUT",
      headers: {{"Content-Type": "text/plain", "Authorization": "Bearer " + token}},
      body: document.getElementById("editor").value,
    }});
    const j = await r.json();
    msg.textContent = r.ok ? "Saved." : "Error: " + j.error;
  }} catch(e) {{
    msg.textContent = "Error: " + e.message;
  }}
  setTimeout(() => msg.textContent = "", 3000);
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
  --bg:#0f0f0f; --surface:#1a1a1a; --border:#272727;
  --text:#e0e0e0; --muted:#777; --accent:#4ade80; --danger:#f87171;
  --mono:"JetBrains Mono","Fira Code","Cascadia Code",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  min-height:100vh;display:grid;place-items:center;background:var(--bg);
  color:var(--text);font-family:var(--sans);padding:24px;
}
.card{
  width:min(420px,100%);background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:24px;
}
h1{font-size:.85rem;letter-spacing:.12em;color:var(--accent);margin-bottom:14px}
p{font-size:.9rem;line-height:1.5;color:var(--muted);margin-bottom:18px}
input{
  width:100%;background:#111;border:1px solid var(--border);border-radius:6px;
  padding:12px 14px;color:var(--text);font-family:var(--mono);font-size:.85rem;outline:none;
}
input:focus{border-color:#3a3a3a}
button{
  margin-top:12px;width:100%;padding:10px 14px;border:none;border-radius:6px;
  background:var(--accent);color:#000;font-weight:700;cursor:pointer;
}
.msg{min-height:1.2rem;font-size:.82rem;margin-top:12px;color:var(--muted)}
.msg.error{color:var(--danger)}
</style>
</head>
<body>
  <div class="card">
    <h1>OMBUD</h1>
    <p>Enter the access token to view and edit the profile.</p>
    <input type="password" id="token" placeholder="Access token" autocomplete="current-password">
    <button onclick="login()">Continue</button>
    <div class="msg" id="msg"></div>
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
