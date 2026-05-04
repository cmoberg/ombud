import copy
import os
import re
from pathlib import Path

import yaml

PROFILE_BUCKET = os.environ.get("PROFILE_BUCKET")
PROFILE_KEY_PREFIX = os.environ.get("PROFILE_KEY_PREFIX", "profiles")
_PROFILE_DIR_CANDIDATES = (
    Path(__file__).parent / "profiles",
    Path(__file__).parent.parent / "profiles",
)

# Module-level client — avoids per-call credential resolution overhead.
_s3 = None
if PROFILE_BUCKET:
    import boto3
    _s3 = boto3.client("s3")

_CANDIDATE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def _validate_candidate_id(candidate_id: str) -> None:
    if not _CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ValueError(f"Invalid candidate_id: {candidate_id!r}")


# ── Load (parsed) ────────────────────────────────────────────────────────────

def load_profile(candidate_id: str) -> dict:
    return yaml.safe_load(read_raw_profile(candidate_id))


# ── Load (raw YAML string, preserves comments) ────────────────────────────────

def read_raw_profile(candidate_id: str) -> str:
    _validate_candidate_id(candidate_id)
    if PROFILE_BUCKET:
        return _read_s3(candidate_id)
    return _read_local(candidate_id)


def _read_local(candidate_id: str) -> str:
    for profile_dir in _PROFILE_DIR_CANDIDATES:
        path = profile_dir / f"{candidate_id}.yaml"
        if path.exists():
            return path.read_text()
    raise FileNotFoundError(candidate_id)


def _read_s3(candidate_id: str) -> str:
    obj = _s3.get_object(
        Bucket=PROFILE_BUCKET,
        Key=f"{PROFILE_KEY_PREFIX}/{candidate_id}.yaml",
    )
    return obj["Body"].read().decode("utf-8")


# ── Save (raw YAML string) ────────────────────────────────────────────────────

def save_raw_profile(candidate_id: str, content: str) -> None:
    _validate_candidate_id(candidate_id)
    if PROFILE_BUCKET:
        _save_s3(candidate_id, content)
    else:
        _save_local(candidate_id, content)


def _save_local(candidate_id: str, content: str) -> None:
    existing_path = next(
        (profile_dir / f"{candidate_id}.yaml" for profile_dir in _PROFILE_DIR_CANDIDATES if (profile_dir / f"{candidate_id}.yaml").exists()),
        None,
    )
    target = existing_path or (_PROFILE_DIR_CANDIDATES[0] / f"{candidate_id}.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def _save_s3(candidate_id: str, content: str) -> None:
    _s3.put_object(
        Bucket=PROFILE_BUCKET,
        Key=f"{PROFILE_KEY_PREFIX}/{candidate_id}.yaml",
        Body=content.encode("utf-8"),
        ContentType="application/x-yaml",
    )


# ── Consent filtering ─────────────────────────────────────────────────────────

def apply_withheld(profile: dict) -> dict:
    """Return a deep copy of the profile with withheld_fields removed."""
    withheld = profile.get("consent", {}).get("withheld_fields", [])
    if not withheld:
        return profile
    result = copy.deepcopy(profile)
    for path in withheld:
        parts = path.split(".")
        obj = result
        for part in parts[:-1]:
            if not isinstance(obj, dict) or part not in obj:
                break
            obj = obj[part]
        else:
            obj.pop(parts[-1], None)
    return result
