#!/usr/bin/env python3
"""
fleet watchdog — checks that Fleet Control's CONTROL PLANE is actually alive.

This is NOT a model health check. The daily refresh LaunchAgent
(ai.hermes.fleet-refresh) already probes every free model and writes the
results to fleet.db. This script checks the things that job cannot see:

  1. The Fleet Control UI (app.py on 127.0.0.1:8787) is listening and
     serving /api/state. It has no LaunchAgent of its own, so it dies
     silently and the database keeps looking healthy.
  2. The daily refresh LaunchAgent is still succeeding (LastExitStatus 0).
  3. The catalogue is fresh (not older than STALE_AFTER_SECONDS).
  4. The health table has evidence in it, not just empty rows.
  5. Every profile's configured main model still exists in the catalogue.
     A dead model in a profile config is invisible to the dashboard until
     someone opens that profile's dropdown.

Exit code: 0 when healthy, 1 when a problem was found. Findings go to
STDOUT so a --no-agent cron job can deliver them; keep it short.

Run under the Hermes venv interpreter (needs PyYAML):
  /Users/user/.hermes/hermes-agent/venv/bin/python3 watchdog.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DASH_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DASH_DIR, "fleet.db")
HERMES_HOME = os.path.expanduser("~/.hermes")
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")
ROOT_CONFIG = os.path.join(HERMES_HOME, "config.yaml")

DASHBOARD_URL = "http://127.0.0.1:8787/api/state"
LAUNCHD_LABEL = "ai.hermes.fleet-refresh"

# The daily refresh runs at 07:12. Allow for a late run plus the ~6 minute
# sweep before we call the catalogue stale.
STALE_AFTER_SECONDS = 26 * 3600
# A fresh catalogue with no health rows means the sweep died on a new model.
MIN_HEALTH_ROWS = 20
# Profiles whose "model" is not a chat model (image generation via Workers AI /
# the Cloudflare plugin). The chat catalogue can never contain their slugs, so
# check 5 skips them instead of failing every run. Add a profile here only if
# it never takes chat or kanban work.
NON_CHAT_PROFILES = {"image-gen", "cf-image-gen"}


def check_dashboard():
    """The UI must be listening and answering. This is the gap that bit us."""
    if not port_open(8787):
        return False, (
            "Fleet Control UI is DOWN (nothing on 127.0.0.1:8787). "
            "Start it: cd ~/Projects/fleet-dashboard && "
            "/Users/user/.hermes/hermes-agent/venv/bin/python3 app.py 8787"
        )
    try:
        with urllib.request.urlopen(DASHBOARD_URL, timeout=8) as r:
            if r.status != 200:
                return False, f"Fleet Control UI returned HTTP {r.status} on /api/state"
            body = r.read()
    except urllib.error.URLError as e:
        return False, f"Fleet Control UI is listening but /api/state failed: {e.reason}"
    except Exception as e:
        return False, f"Fleet Control UI /api/state failed: {type(e).__name__}: {e}"

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False, "Fleet Control UI returned non-JSON from /api/state"

    items = (data.get("catalog") or {}).get("items") or []
    if not items:
        return False, "Fleet Control UI is up but its catalogue is EMPTY (refresh the lanes)"
    return True, f"Fleet Control UI up, {len(items)} catalogue models"


def port_open(port):
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False


def check_launchd():
    """The daily refresh must be exiting 0, not crashing like it did today."""
    try:
        out = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        return False, f"could not read launchd status: {e}"

    if out.returncode != 0 or not out.stdout.strip():
        return False, f"LaunchAgent {LAUNCHD_LABEL} is not loaded"

    last_exit = None
    for line in out.stdout.splitlines():
        if "LastExitStatus" in line:
            try:
                last_exit = int(line.split("=")[-1].strip().rstrip(";"))
            except ValueError:
                pass
    if last_exit is None:
        return True, f"{LAUNCHD_LABEL} loaded (no exit status yet - never ran)"
    if last_exit != 0:
        return False, (
            f"{LAUNCHD_LABEL} last exit status {last_exit} "
            f"(non-zero = the daily sweep crashed; check refresh.err.log)"
        )
    return True, f"{LAUNCHD_LABEL} last exit status 0"


def check_db():
    """Catalogue freshness and health evidence."""
    if not os.path.exists(DB):
        return False, f"fleet.db is missing at {DB}"

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        lane = c.execute(
            "SELECT MAX(checked_at) AS ts FROM catalog_lane"
        ).fetchone()
        ts = lane["ts"] if lane else None
        if not ts:
            return False, "no catalogue has ever been pulled (fleet.db catalog_lane is empty)"
        age = int(time.time()) - int(ts)
        if age > STALE_AFTER_SECONDS:
            return False, (
                f"catalogue is stale ({age // 3600}h old, limit {STALE_AFTER_SECONDS // 3600}h). "
                "Run refresh.py --catalogue-only"
            )

        rows = c.execute("SELECT COUNT(*) AS n FROM health").fetchone()["n"]
        if rows < MIN_HEALTH_ROWS:
            return False, (
                f"only {rows} health rows (expected >= {MIN_HEALTH_ROWS}). "
                "The sweep may be crashing on a new model."
            )
        with_tools = c.execute(
            "SELECT COUNT(*) AS n FROM health WHERE tools_yes > 0"
        ).fetchone()["n"]
        if with_tools == 0:
            return False, "no model has tool-call evidence - the health probe is broken"
        return True, f"catalogue fresh, {rows} health rows, {with_tools} with tool evidence"
    except sqlite3.OperationalError as e:
        return False, f"fleet.db query failed: {e}"
    finally:
        c.close()


def profile_main_models():
    """Every profile's configured main model -> (profile, provider, model)."""
    out = []
    paths = []
    if os.path.exists(ROOT_CONFIG):
        paths.append(("default", ROOT_CONFIG))
    if os.path.isdir(PROFILES_DIR):
        for name in sorted(os.listdir(PROFILES_DIR)):
            p = os.path.join(PROFILES_DIR, name, "config.yaml")
            if os.path.exists(p):
                paths.append((name, p))
    for name, path in paths:
        try:
            import yaml
            with open(path) as fh:
                d = yaml.safe_load(fh) or {}
        except Exception:
            continue
        m = d.get("model")
        # Some legacy profiles use a bare string ('model: any' or a
        # '@cf/...' Workers-AI id) instead of the {default, provider} dict.
        # Those are not chat models on our lanes, so skip them.
        if isinstance(m, str):
            continue
        if not isinstance(m, dict):
            continue
        model = m.get("default")
        if model and isinstance(model, str):
            out.append((name, m.get("provider") or "", model))
    return out


def check_profile_models():
    """A profile pointed at a model that no longer exists is a silent landmine."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return True, "SKIPPED (no PyYAML - run with the Hermes venv interpreter)"

    if not os.path.exists(DB):
        return True, "SKIPPED (no fleet.db to check against)"

    c = sqlite3.connect(DB)
    try:
        catalog = {
            (r[0], r[1]) for r in c.execute("SELECT lane, model FROM catalog")
        }
    except sqlite3.OperationalError:
        return True, "SKIPPED (catalogue table unreadable)"
    finally:
        c.close()

    if not catalog:
        return True, "SKIPPED (catalogue empty)"

    bad, skipped = [], []
    for prof, provider, model in profile_main_models():
        if prof in NON_CHAT_PROFILES:
            skipped.append(prof)
            continue
        lane = provider or ""
        # The catalogue stores the bare model id for most lanes; openrouter
        # stores it bare too. Accept a match on model id alone, or lane+model.
        bare = model.split("/")[-1] if "/" in model else model
        hit = (lane, model) in catalog or any(m == model or m == bare for _, m in catalog)
        if not hit:
            bad.append(f"{prof} -> {provider}/{model}")
    if bad:
        return False, (
            f"{len(bad)} profile(s) point at models not in the catalogue: "
            + "; ".join(bad[:6])
        )
    note = f" (non-chat, not checked: {', '.join(skipped)})" if skipped else ""
    return True, "all profile main models exist in the catalogue" + note


def _liveness_probe(lane, model, key, timeout=90):
    """Minimal real inference call. Returns the HTTP status (or None).

    Deliberately tiny ("Reply OK", max_tokens 16): a 402/404 comes back in
    about a second, which is the case worth alerting on. NVIDIA's slow models
    (74-273s cold) would blow the timeout, so a timeout here means INCONCLUSIVE
    - not a failure. Only a hard HTTP status is treated as a verdict.
    """
    try:
        import refresh as refresh_mod
    except ImportError:
        return None
    url = refresh_mod.LANES.get(lane)
    if not url:
        return None
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply OK"}],
        "max_tokens": 16,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", refresh_mod.UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def check_primary_health():
    """A profile whose PRIMARY model is currently blocked is a silent failure.

    The fallback chain hides this: when the primary 402s, Hermes quietly
    answers on a fallback and the profile looks alive. That happened on
    6 Nov 26 - apinex's daily check-in gate killed coder's primary
    (free/qwen-3.8-max) and the bot answered on NVIDIA instead, so nothing
    alerted. The dashboard badge had the same blind spot (it checked
    cumulative tools_yes before the live http), so the UI said green too.

    This PROBES each profile's primary live, because the health table is only
    refreshed once a day at 07:12 - a lane that dies at 08:00 would otherwise
    look healthy until tomorrow. A hard error needs a HUMAN (top up, check in,
    pick another model) and will not clear on its own, so it is worth a live
    request on every watchdog run.

    Probes run in parallel and are deduplicated by (lane, model): 17 profiles
    share only 9 distinct primaries, and NVIDIA calls are slow. Only a hard
    HTTP status fails the check - a timeout is inconclusive, because slow is
    normal on that lane and a false alarm would train Stu to ignore the alert.
    """
    try:
        import yaml  # noqa: F401
    except ImportError:
        return True, "SKIPPED (no PyYAML - run with the Hermes venv interpreter)"

    try:
        import refresh as refresh_mod
    except ImportError:
        return True, "SKIPPED (refresh.py not importable from this directory)"

    try:
        keys = refresh_mod.load_keys()
    except Exception:
        keys = {}

    # Hard failures that a fallback can mask but only a human can fix.
    HARD_FAILS = {402: "billing / daily check-in required",
                  404: "model no longer on the provider's live list"}

    # Deduplicate: many profiles share one primary.
    primaries = []
    seen = set()
    for prof, provider, model in profile_main_models():
        if prof in NON_CHAT_PROFILES:
            continue
        key = (provider or "", model)
        if key in seen:
            continue
        seen.add(key)
        primaries.append((prof, provider or "", model))

    to_probe, skipped = [], []
    for prof, lane, model in primaries:
        key = keys.get(lane)
        if not key:
            skipped.append(f"{prof} ({lane}: no key)")
            continue
        to_probe.append((prof, lane, model, key))

    def run(item):
        prof, lane, model, key = item
        return prof, lane, model, _liveness_probe(lane, model, key)

    results = []
    if to_probe:
        with ThreadPoolExecutor(max_workers=min(9, len(to_probe))) as pool:
            for out in pool.map(run, to_probe):
                results.append(out)

    bad, inconclusive = [], []
    for prof, lane, model, http in results:
        if http in HARD_FAILS:
            bad.append(f"{prof} -> {lane}/{model} [{http} {HARD_FAILS[http]}]")
        elif http is None:
            inconclusive.append(f"{prof} -> {lane}/{model}")
        elif http not in (200, 403):
            # 403 is harness-gating (Hermes qualifies). Anything else - 429,
            # 5xx - is worth a mention but usually recovers on its own.
            bad.append(f"{prof} -> {lane}/{model} [live http={http}]")

    if bad:
        return False, (
            f"{len(bad)} profile primary model(s) not healthy on a live probe: "
            + "; ".join(bad[:6])
            + ". The fallback chain will answer, but it is masking a dead primary."
        )
    note = ""
    if inconclusive or skipped:
        bits = []
        if inconclusive:
            bits.append(f"{len(inconclusive)} timed out (slow lane, inconclusive)")
        if skipped:
            bits.append(f"{len(skipped)} skipped: {', '.join(skipped[:2])}")
        note = " (" + "; ".join(bits) + ")"
    return True, "all profile primaries answered a live probe" + note


def main():
    results = []
    for fn in (check_dashboard, check_launchd, check_db, check_profile_models,
               check_primary_health):
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"{fn.__name__} raised {type(e).__name__}: {e}"
        results.append((fn.__name__, ok, msg))

    problems = [(n, m) for n, ok, m in results if not ok]

    # SILENT WHEN HEALTHY, AND ALWAYS EXIT 0.
    # This runs as a --no-agent cron job every 30m. Two rules of the runner:
    #   - empty stdout sends nothing at all (the watchdog pattern)
    #   - a NON-ZERO exit ALSO sends an error alert
    # So exiting 1 with a message would notify Stu TWICE for one problem.
    # Exit 0 always and let stdout carry the finding. Empty stdout = silence.
    if not problems:
        return 0

    for name, ok, msg in results:
        if not ok:
            print(f"[FAIL] {name}: {msg}")
    print()
    print(f"FLEET WATCHDOG: {len(problems)} problem(s) found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
