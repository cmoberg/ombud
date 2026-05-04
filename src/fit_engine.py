"""
Rule-based fit signal computation. No external dependencies.
Assesses the structured dimensions of candidate-role fit using
explicit profile data only. Marks dimensions as unknown when
data is insufficient rather than inferring.
"""

from __future__ import annotations


def compute_fit_signal(profile: dict, role: dict) -> dict:
    search = profile.get("search", {})
    target = search.get("target", {})
    culture = profile.get("culture", {})
    skills = profile.get("skills", [])
    experience = profile.get("experience", [])

    readiness = _readiness(search)
    interest, interest_score = _interest(role, target)
    constraints = _constraints(role, search, culture)

    dims = {
        "skills": _skills_fit(role, skills),
        "experience": _experience_fit(role, experience),
        "seniority": _seniority_fit(role, target),
        "culture": _culture_fit(role, culture),
    }

    scores = [d["score"] for d in dims.values() if d.get("score") is not None]
    fit_score = round(sum(scores) / len(scores), 2) if scores else 0.5

    overall = _overall(fit_score, interest_score, readiness, constraints["unmet"])

    strengths = [
        item
        for key in ("matched", "evidence", "signals")
        for d in dims.values()
        for item in (d.get(key) or [])
        if item
    ]
    gaps = [item for d in dims.values() for item in (d.get("gaps") or []) if item]

    return {
        "overall": overall,
        "fit": {
            "score": fit_score,
            "dimensions": {
                "skills": {k: v for k, v in dims["skills"].items()},
                "experience": dims["experience"],
                "seniority": dims["seniority"],
                "culture": dims["culture"],
            },
            "blockers": constraints["unmet"],
            "strengths": strengths[:5],
            "gaps": gaps[:5],
        },
        "interest": interest,
        "readiness": readiness,
        "constraints": constraints,
        "recommended_action": _action(overall, constraints["unmet"]),
    }


# ── Dimensions ───────────────────────────────────────────────────────────────

def _readiness(search: dict) -> dict:
    return {
        "status": search.get("status", "unknown"),
        "available_from": search.get("available_from"),
        "notice_period": search.get("notice_period"),
    }


def _interest(role: dict, target: dict) -> tuple[dict, float]:
    signals: list[str] = []
    stated: list[str] = []
    hits = 0
    checks = 0

    # Role title
    role_title = (role.get("title") or "").lower()
    target_roles = [r.lower() for r in (target.get("roles") or [])]
    if target_roles:
        checks += 1
        if any(t in role_title or role_title in t for t in target_roles):
            hits += 1
            signals.append(f"role title matches candidate's target roles")
        else:
            signals.append(f"role title '{role.get('title')}' not in candidate's target roles")
        stated.append(f"candidate targets: {', '.join(target.get('roles', []))}")

    # Seniority
    seniority = role.get("seniority_level") or ""
    target_seniority = target.get("seniority") or []
    if target_seniority and seniority:
        checks += 1
        if seniority in target_seniority:
            hits += 1
            signals.append("seniority level matches candidate's targets")
        else:
            signals.append(f"seniority '{seniority}' not in candidate's target levels")

    # Company stage
    stage = role.get("company_stage") or ""
    target_stages = target.get("company_stages") or []
    if target_stages and stage:
        checks += 1
        if stage in target_stages:
            hits += 1
            signals.append(f"company stage '{stage}' aligns with candidate's preferences")
            stated.append(f"candidate prefers stages: {', '.join(target_stages)}")
        else:
            signals.append(f"company stage '{stage}' outside candidate's preferred stages")

    # Company size
    size = role.get("company_size")
    size_range = target.get("company_size_range")
    if size is not None and size_range:
        checks += 1
        lo, hi = size_range[0], size_range[1]
        if lo <= size <= hi:
            hits += 1
            signals.append(f"company size {size} within candidate's preferred range ({lo}–{hi})")
        else:
            signals.append(f"company size {size} outside candidate's preferred range ({lo}–{hi})")

    # Industry — positive match adds weight, miss is neutral (not a hard filter)
    industry = (role.get("industry") or "").lower().replace(" ", "_").replace("-", "_")
    target_industries = [i.lower() for i in (target.get("industries") or [])]
    if target_industries and industry:
        checks += 1
        if any(t in industry or industry in t for t in target_industries):
            hits += 1
            signals.append("industry aligns with candidate's targets")

    score = hits / checks if checks else 0.5

    if checks == 0:
        level = "unknown"
    elif score >= 0.7:
        level = "high"
    elif score >= 0.4:
        level = "moderate"
    else:
        level = "low"

    return (
        {"level": level, "basis": "stated", "signals": signals, "stated_preferences": stated},
        score,
    )


def _constraints(role: dict, search: dict, culture: dict) -> dict:
    met: list[str] = []
    unmet: list[str] = []
    unknown: list[str] = []

    geo = search.get("geography") or {}

    # Location exclusions
    location = role.get("location") or ""
    excluded = geo.get("relocation_excluded") or []
    if location:
        if excluded and any(e.lower() in location.lower() for e in excluded):
            unmet.append(f"role location excluded by candidate")
        else:
            met.append("location not excluded by candidate")
    else:
        unknown.append("role location not specified")

    # Remote policy
    role_remote = role.get("remote_policy") or ""
    cand_remote = geo.get("remote_policy") or ""
    if role_remote and cand_remote:
        conflict = (
            (cand_remote == "remote_only" and role_remote == "onsite")
            or (cand_remote == "onsite" and role_remote == "remote")
        )
        if conflict:
            unmet.append(f"remote policy conflict: candidate '{cand_remote}', role '{role_remote}'")
        else:
            met.append("remote policy compatible")
    else:
        unknown.append("remote policy not fully specified")

    # Compensation and equity — stripped from profile, can't verify
    unknown.append("compensation details withheld — contact candidate to confirm")
    unknown.append("equity requirements not verified from role description alone")

    return {"met": met, "unmet": unmet, "unknown": unknown}


def _skills_fit(role: dict, skills: list) -> dict:
    role_text = " ".join(filter(None, [
        role.get("title") or "",
        role.get("functional_area") or "",
        role.get("industry") or "",
        role.get("description") or "",
    ])).lower()

    matched = [
        s["label"]
        for s in skills
        if any(
            word in role_text
            for word in (s.get("label") or "").lower().split()
            if len(word) > 3
        )
    ]

    # Score relative to a reasonable expectation (30% of skills relevant to any role)
    score = min(1.0, round(len(matched) / max(len(skills) * 0.3, 1), 2)) if skills else 0.5

    return {"score": score, "matched": matched, "gaps": []}


def _experience_fit(role: dict, experience: list) -> dict:
    if not experience:
        return {"score": 0.5, "evidence": []}

    evidence: list[str] = []
    recent = experience[0]
    scope = recent.get("scope") or {}

    if scope.get("team_size"):
        evidence.append(f"led team of {scope['team_size']} at most recent role")
    if scope.get("geography"):
        evidence.append(f"operated at {scope['geography']} scope")
    if scope.get("p_and_l_usd"):
        evidence.append(f"P&L responsibility: ${scope['p_and_l_usd']:,}")
    if len(experience) > 1:
        evidence.append(f"{len(experience)} distinct roles across career")

    return {"score": 0.75 if evidence else 0.5, "evidence": evidence}


_SENIORITY_HIERARCHY = ["ic", "manager", "director", "vp", "svp", "c_suite", "board"]


def _seniority_fit(role: dict, target: dict) -> dict:
    role_level = role.get("seniority_level") or ""
    target_levels = target.get("seniority") or []

    if not role_level or not target_levels:
        return {"score": 0.5, "calibration": "unknown"}

    role_idx = _SENIORITY_HIERARCHY.index(role_level) if role_level in _SENIORITY_HIERARCHY else -1
    target_indices = [
        _SENIORITY_HIERARCHY.index(s) for s in target_levels if s in _SENIORITY_HIERARCHY
    ]

    if role_idx == -1 or not target_indices:
        return {"score": 0.5, "calibration": "unknown"}

    if min(target_indices) <= role_idx <= max(target_indices):
        return {"score": 1.0, "calibration": "calibrated"}
    elif role_idx < min(target_indices):
        return {"score": 0.3, "calibration": "underleveled"}
    else:
        return {"score": 0.3, "calibration": "overleveled"}


def _culture_fit(role: dict, culture: dict) -> dict:
    role_text = " ".join(filter(None, [
        role.get("title") or "",
        role.get("description") or "",
        role.get("context") or "",
    ])).lower()

    signals: list[str] = []

    # Deal-breakers: keyword match (best-effort; marks as potential conflict)
    for db in (culture.get("deal_breakers") or []):
        significant_words = [w for w in db.lower().split() if len(w) > 4]
        if sum(1 for w in significant_words if w in role_text) >= 2:
            signals.append(f"potential deal-breaker match: '{db}'")
            return {"score": 0.3, "signals": signals}

    # Motivators: positive keyword match
    hits = sum(
        1 for m in (culture.get("motivators") or [])
        if any(w in role_text for w in m.lower().split() if len(w) > 4)
    )
    if hits:
        signals.append("role text aligns with stated motivators")

    return {"score": round(min(1.0, 0.6 + hits * 0.1), 2), "signals": signals}


# ── Overall signal ────────────────────────────────────────────────────────────

def _overall(fit_score: float, interest_score: float, readiness: dict, blockers: list) -> dict:
    if blockers:
        return {
            "signal": "poor",
            "confidence": 0.8,
            "summary": f"Hard constraint violated: {blockers[0]}.",
        }

    combined = fit_score * 0.6 + interest_score * 0.4

    status = readiness.get("status", "unknown")
    if status == "not_looking":
        combined *= 0.5
    elif status == "passive":
        combined *= 0.8

    if combined >= 0.75:
        signal, summary = "strong", "Strong alignment across fit and stated preferences. Recommend direct engagement."
    elif combined >= 0.55:
        signal, summary = "likely", "Good alignment on most dimensions. Candidate is likely to be interested."
    elif combined >= 0.35:
        signal, summary = "possible", "Partial alignment. Some dimensions need clarification before proceeding."
    else:
        signal, summary = "poor", "Limited alignment with candidate's stated preferences and profile."

    confidence = round(min(0.9, 0.4 + (fit_score + interest_score) / 4), 2)
    return {"signal": signal, "confidence": confidence, "summary": summary}


def _action(overall: dict, blockers: list) -> dict:
    signal = overall.get("signal")
    if blockers or signal == "poor":
        return {"action": "do_not_contact", "rationale": "Hard constraint violated or poor fit."}
    if signal == "strong":
        return {"action": "engage", "rationale": "Strong fit and interest — proceed with direct outreach."}
    if signal == "likely":
        return {"action": "request_intro", "rationale": "Good fit — warm outreach or introduction recommended."}
    return {"action": "monitor", "rationale": "Partial fit — add to watchlist pending more role detail."}
