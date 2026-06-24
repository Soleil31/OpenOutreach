# linkedin/conf.py
from __future__ import annotations

import os
from pathlib import Path

from linkedin.tz_detect import system_timezone


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent

PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

DIAGNOSTICS_DIR = Path("/tmp/openoutreach-diagnostics")

FASTEMBED_CACHE_DIR = ROOT_DIR / ".cache" / "fastembed"

FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures"
FIXTURE_PROFILES_DIR = FIXTURE_DIR / "profiles"
FIXTURE_PAGES_DIR = FIXTURE_DIR / "pages"
DUMP_PAGES = False

MIN_DELAY = 5
MAX_DELAY = 8

# ----------------------------------------------------------------------
# Browser config
# ----------------------------------------------------------------------
BROWSER_SLOW_MO = 200
BROWSER_DEFAULT_TIMEOUT_MS = 30_000
BROWSER_LOGIN_TIMEOUT_MS = 40_000
BROWSER_NAV_TIMEOUT_MS = 10_000
HUMAN_TYPE_MIN_DELAY_MS = 50
HUMAN_TYPE_MAX_DELAY_MS = 200

# ----------------------------------------------------------------------
# Onboarding defaults (shown to user during interactive setup)
# ----------------------------------------------------------------------
DEFAULT_CONNECT_DAILY_LIMIT = 20
DEFAULT_CONNECT_WEEKLY_LIMIT = 100
DEFAULT_FOLLOW_UP_DAILY_LIMIT = 25

# ----------------------------------------------------------------------
# Active-hours schedule (daemon pauses outside this window).
# All knobs are overridable via env so the schedule can be tuned without a
# rebuild: ENABLE_ACTIVE_HOURS=false runs 24/7, ACTIVE_START_HOUR/
# ACTIVE_END_HOUR shift the window, REST_DAYS="5,6" sets the days off
# ("" = no rest days, run every day).
# ----------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


ENABLE_ACTIVE_HOURS = _env_bool("ENABLE_ACTIVE_HOURS", True)
ACTIVE_START_HOUR = _env_int("ACTIVE_START_HOUR", 9)   # inclusive, local time
ACTIVE_END_HOUR = _env_int("ACTIVE_END_HOUR", 19)      # exclusive, local time
# Prefer the explicit account timezone (LINKEDIN_TIMEZONE — the same env that
# drives the browser fingerprint) so the active-hours window tracks the
# account's local time. Falls back to the container clock, which is UTC under
# Docker — the bug that had DXB working 13-23 local instead of 9-19.
ACTIVE_TIMEZONE = os.environ.get("LINKEDIN_TIMEZONE", "").strip() or system_timezone()
# REST_DAYS: "5,6" = Sat+Sun off (default). Set REST_DAYS="" to run every day.
_rest_raw = os.environ.get("REST_DAYS")
if _rest_raw is None:
    REST_DAYS = (5, 6)
else:
    REST_DAYS = tuple(int(x) for x in _rest_raw.split(",") if x.strip().isdigit())

# ----------------------------------------------------------------------
# Campaign config (timing + ML defaults — hardcoded, no YAML)
# ----------------------------------------------------------------------
CAMPAIGN_CONFIG = {
    "check_pending_recheck_after_hours": 24,
    "min_action_interval": 120,
    "qualification_n_mc_samples": 100,
    "min_ready_to_connect_prob": 0.9,
    "min_positive_pool_prob": 0.20,
    "embedding_model": "BAAI/bge-small-en-v1.5",
    "connect_delay_seconds": 10,
    "connect_no_candidate_delay_seconds": 300,
    "enrich_min_delay_seconds": 6,
    "enrich_max_delay_seconds": 10,
    "enrich_max_per_page": 10,
    "burst_min_seconds": 2700,   # 45 min
    "burst_max_seconds": 3900,   # 65 min
    "break_min_seconds": 600,    # 10 min
    "break_max_seconds": 1200,   # 20 min
}


