#!/usr/bin/env python3
"""
Fleet Dashboard — local control surface.

Stdlib only. Serves the UI and a small JSON API on 127.0.0.1:8787.

SAFETY MODEL
  - GET  /api/state      read-only: profiles + health + roles + spread advice
  - GET  /api/catalog    read-only: every free/direct inference endpoint we can route to
  - POST /api/plan       builds a proposed change set, returns a DIFF. Writes nothing.
  - POST /api/apply      applies a previously-planned change set. Backs up every
                         config.yaml it touches first, and refuses to run unless
                         the payload carries apply=true.
  - POST /api/refresh    kick a health + catalogue sweep (writes fleet.db only)
  - GET  /api/wiring     lanes a profile routes to but cannot use (no stanza / no key)
  - POST /api/wiring/fix close those gaps (see wiring.py). Apply does this per profile too.
  - GET  /api/gateway    is the shared gateway busy? state of any pending restart
  - POST /api/gateway/restart {mode: now|when-idle|cancel}

ENCODING (one contract everywhere)
  A model choice is the string "<lane>/<model>", split on the FIRST slash.
  e.g. "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
       -> provider=openrouter, model=nvidia/nemotron-3-super-120b-a12b:free
  Main model and fallbacks use the same form, so plan/apply/inventory agree.

There is deliberately no code path that changes a profile config without an
explicit POST /api/apply.
"""
import glob, json, os, shutil, subprocess, sys, threading, time, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wiring
DB = os.path.join(HERE, "fleet.db")
UI = os.path.join(HERE, "ui", "index.html")
PROFILES = "/Users/user/.hermes/profiles"
DEFAULT_CFG = "/Users/user/.hermes/config.yaml"   # the 'default' profile
HOME_HERMES = "/Users/user/.hermes"
BACKUP = os.path.join(HERE, "backups")
PY = "/Users/user/.hermes/hermes-agent/venv/bin/python3"


def port_open(port):
    """Check if a TCP port is listening on localhost."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        return bool(out.stdout.strip())
    except Exception:
        return False

# ---------------------------------------------------------------- role guide
ROLES = {
  "default":      ("overlord", "Orchestrator. Telegram @MacHermin_bot + desktop default. Stu chats here.", "big",
                   "NVIDIA direct via nvidia-guard (minimax-m3.1-flash primary, Nemotron 3 Ultra fallbacks). Keep fallbacks free."),
  "quill":        ("worker", "Direct-API tool caller. Primary NVIDIA lane, routed through nvidia-guard (10k req/day).", "big",
                   "1M ctx Nemotron. Highest-value worker - keep on the guarded direct lane."),
  "planner":      ("worker", "Long-horizon plans, reversibility tags, Risks: blocks.", "big",
                   "1M ctx ultra-550b is a good fit; this role reads a lot of context."),
  "threat-model": ("worker", "Security scout, 7-step runbook, file + web eyes.", "med",
                   "NVIDIA direct via nvidia-guard. Super-120b is plenty; don't burn ultra on this."),
  "archivist":    ("worker", "Snapshots, TTL, compaction, RAG housekeeping.", "med",
                   "NVIDIA direct via nvidia-guard. 10k req/day, no token cap."),
  "messenger":    ("worker", "Envelope semantics: query / broadcast / subscription.", "med",
                   "Groq direct - gpt-oss-120b. Keeps OR headroom free."),
  "coder":        ("worker", "Implementation from packaged briefs.", "big",
                   "Code generation wants a big brain. Laguna is free but has TOOL-FAILed before - re-test on a real task."),
  "validator":    ("worker", "[APPROVE] / [ISSUES] code verdicts. Proposer is never approver.", "med",
                   "Groq direct - gpt-oss-120b. Adversarial reading."),
  "auditor":      ("worker", "Pre-screen proposals, rejection taxonomy.", "small",
                   "Cheap and fast is correct here. Bulk screening, not deep reasoning."),
  "metrics":      ("worker", "Counts only, NEVER rankings.", "small",
                   "Deliberately dumb. A big model invites it to editorialize."),
  "qa":           ("worker", "Builder-tester. Evidence or it didn't happen.", "med",
                   "Must ground in evidence. Groq gpt-oss-120b or NVIDIA via guard."),
  "ledger":       ("worker", "Costs, revenue maths, aggregates only.", "small",
                   "Bookkeeping. Current slug ling-3.0-flash-fin:free is DEAD - repoint to NVIDIA via guard."),
  "scope":        ("worker", "Cloud eyes, visual QA, cross-checks Jasper.", "med",
                   "Needs a vision-capable model. Check catalogue for a vision slug before reassigning."),
  "wrench":       ("worker", "Terminal tasks, mechanical edits, second opinions.", "small",
                   "NVIDIA direct via nvidia-guard. Nemotron 3.5 Lightning is fast."),
  "writer":       ("worker", "Prose, docs, summaries.", "med",
                   "Groq direct - gpt-oss-120b. Voice matters more than reasoning depth."),
  "gust":         ("worker", "Reviews, summaries, second verdicts.", "med",
                   "Groq direct - gpt-oss-120b. Keeps OR headroom free."),
  "forge":        ("worker", "Heavy build/coding muscle. Inception mercury-2 (unverified quota).", "big",
                   "Quota unverifiable - ration. Bulk drafting only."),
  "fable-slim":   ("reserve", "Extra muscle for big jobs. Ask Stu before every use.", "big",
                   "Stu's rule: occasional only, ask first."),
  "system-bot":   ("desktop", "Desktop experiments profile. Stu's config sandbox.", "big",
                   "Anthropic Haiku direct. Not the orchestrator - that is 'default'."),
  "astra-slim":   ("reserve", "Occasional review of finished work only.", "big",
                   "Stu's rule: ask first."),
}

# Lane budget - how much free inference each lane actually has.
LANE_BUDGET = {
  "nvidia":     {"free_headroom": "10,000 req/day, 40 rpm, NO token cap", "bottleneck": "requests", "quality": "high"},
  "groq":       {"free_headroom": "200k tokens/day per model, 30 rpm",    "bottleneck": "tokens",   "quality": "med"},
  "openrouter": {"free_headroom": "1,000 req/day SHARED across all :free", "bottleneck": "requests", "quality": "varies"},
  "inception":  {"free_headroom": "unverifiable (no balance endpoint)",   "bottleneck": "unknown",  "quality": "med"},
  "anthropic":  {"free_headroom": "PAID direct (Stu's key)",              "bottleneck": "dollars",  "quality": "high"},
  "jasper_gptoss": {"free_headroom": "LOCAL rig, 192.168.55.52 (when powered)", "bottleneck": "uptime", "quality": "med"},
  "apinex":     {"free_headroom": "20M weighted tok/day (resets 00:00 UTC), 15 rpm, DAILY CHECK-IN required", "bottleneck": "tokens + check-in", "quality": "med"},
}

EXTERNAL = [
  {"name": "Antigravity (agy)", "what": "Claude Opus 4.6 / Sonnet 4.6 / Gemini 3.1 Pro, FREE",
   "best_for": "Big coding tasks, long refactors, design review",
   "catch": "Burns 12-15k INPUT tokens per call from the harness prompt. Weekly pool. FORBIDDEN to point hermes/opencode/workers at this login - Stu drives it by hand."},
  {"name": "OpenCode (local)", "what": "Local harness, your choice of model",
   "best_for": "Implementation chunks Stu copy/pastes in", "catch": "Manual - you drive it."},
]


# ---------------------------------------------------------------- helpers
def cfg_path(prof):
    return DEFAULT_CFG if prof == "default" else os.path.join(PROFILES, prof, "config.yaml")


def split_choice(s):
    """'lane/model' -> (lane, model). Bare strings with no slash -> (None, s)."""
    if not s or "/" not in s:
        return None, s
    lane, model = s.split("/", 1)
    return lane, model


def join_choice(lane, model):
    return f"{lane}/{model}" if lane else model


def load_catalog():
    if not os.path.exists(DB):
        return {"lanes": {}, "items": []}
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        items = [dict(r) for r in c.execute(
            "SELECT lane, model, is_free, note, live FROM catalog ORDER BY is_free DESC, lane, model")]
        lanes = {r["lane"]: dict(r) for r in c.execute("SELECT * FROM catalog_lane")}
    except sqlite3.OperationalError:
        items, lanes = [], {}
    c.close()
    return {"lanes": lanes, "items": items}


def load_state():
    import inventory
    profs = inventory.collect()
    health = []
    if os.path.exists(DB):
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        for r in c.execute("SELECT * FROM health ORDER BY lane, model"):
            health.append(dict(r))
        c.close()
    by_prov = {}
    for p in profs:
        pr = p.get("provider") or "?"
        by_prov[pr] = by_prov.get(pr, 0) + 1
    # nvidia-guard status - just check if port is listening
    guard_up = port_open(8790)
    # dead models from catalogue
    dead_models = set()
    if os.path.exists(DB):
        c = sqlite3.connect(DB)
        try:
            for r in c.execute("SELECT lane, model FROM catalog WHERE dead=1"):
                dead_models.add(f"{r[0]}/{r[1]}")
        except Exception:
            pass
        c.close()
    return {"profiles": profs, "health": health, "roles": ROLES,
            "lane_budget": LANE_BUDGET, "external": EXTERNAL,
            "catalog": load_catalog(),
            "by_provider": by_prov, "generated_at": int(time.time()),
            "guard_up": guard_up, "dead_models": list(dead_models)}


def _current(prof):
    """Current {model, provider, fallbacks:[lane/model,...]} for a profile, in the
    same encoding the UI uses."""
    import yaml
    d = yaml.safe_load(open(cfg_path(prof))) or {}
    m = d.get("model") or {}
    if not isinstance(m, dict):
        m = {}
    fb = []
    for f in (d.get("fallback_providers") or []):
        if isinstance(f, dict):
            fb.append(join_choice(f.get("provider"), f.get("model")))
        else:
            fb.append(str(f))
    return {"model": m.get("default"), "provider": m.get("provider"),
            "choice": join_choice(m.get("provider"), m.get("default")), "fallbacks": fb,
            "_raw": d}


def _proposed(d, prov, model, fbs):
    """Apply a UI choice to a loaded config dict (in place) and return it."""
    m = d.setdefault("model", {})
    if model:
        m["default"] = model
        m["provider"] = prov
    if fbs is not None:
        d["fallback_providers"] = [
            {"provider": split_choice(f)[0], "model": split_choice(f)[1]} for f in fbs]
    return d


def _want_pair(want):
    """Normalise a UI payload entry -> (provider, model, fallbacks[lane/model])."""
    prov, model = split_choice(want.get("choice") or "")
    fbs = want.get("fallbacks")
    return prov, model, fbs


def plan_changes(req):
    """req = {profile: {choice:'lane/model', fallbacks:['lane/model',...]}} -> diff. Writes nothing."""
    diffs, missing = [], []
    for prof, want in (req or {}).items():
        if not os.path.exists(cfg_path(prof)):
            diffs.append({"profile": prof, "error": "no such profile"})
            continue
        cur = _current(prof)
        prov, model, fbs = _want_pair(want)
        entry = {"profile": prof, "changes": []}
        if model and (model != cur["model"] or prov != cur["provider"]):
            entry["changes"].append({"field": "model", "from": cur["choice"],
                                     "to": join_choice(prov, model)})
        if fbs is not None and fbs != cur["fallbacks"]:
            entry["changes"].append({"field": "fallback_providers",
                                     "from": cur["fallbacks"], "to": fbs})
        for g in wiring.audit(prof, _proposed(cur["_raw"], prov, model, fbs)):
            if g["fixable"]:
                entry["changes"].append({"field": "wiring (auto)", "from": g["lane"], "to": g["detail"]})
            else:
                missing.append(f"{prof}: {g['detail']}")
        if entry["changes"]:
            diffs.append(entry)
    if missing:
        diffs.append({"profile": "!! CANNOT WIRE - these profiles will be skipped",
                      "changes": [{"field": "providers", "from": "-", "to": m} for m in missing]})
    note = "NOTHING WRITTEN. Review, then press Apply. Missing stanzas/keys are added automatically."
    if missing:
        note = "NOTHING WRITTEN. Some lanes cannot be wired (see !! rows); those profiles will be skipped."
    return {"diffs": diffs, "note": note}


def apply_changes(req):
    """Explicit, backed-up write. Only reached via POST /api/apply with apply=true.
    Uses yaml.safe_dump on a full reload; backups are taken first."""
    import yaml
    os.makedirs(BACKUP, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    done, failed, wired_log = [], [], []
    for prof, want in (req or {}).items():
        cfg = cfg_path(prof)
        if not os.path.exists(cfg):
            failed.append({"profile": prof, "error": "no such profile"})
            continue
        try:
            d = _proposed(yaml.safe_load(open(cfg)) or {}, *_want_pair(want))
            blocked = [g["detail"] for g in wiring.audit(prof, d) if not g["fixable"]]
            if blocked:
                failed.append({"profile": prof, "error": "not written: " + "; ".join(blocked)})
                continue
            shutil.copy2(cfg, os.path.join(BACKUP, f"{prof}.{stamp}.yaml"))
            wired, _ = wiring.complete(prof, d)
            with open(cfg, "w") as fh:
                yaml.safe_dump(d, fh, sort_keys=False, default_flow_style=False, allow_unicode=True)
            done.append(prof)
            if wired:
                wired_log.append({"profile": prof, "wired": [g["detail"] for g in wired]})
        except Exception as e:
            failed.append({"profile": prof, "error": str(e)})
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO history(kind,payload,at) VALUES(?,?,?)",
              ("apply", json.dumps({"done": done, "failed": failed, "wired": wired_log, "req": req}), int(time.time())))
    c.commit(); c.close()
    return {"applied": done, "failed": failed, "wired": wired_log,
            "backup_dir": BACKUP, "stamp": stamp, "gateway": gateway_status()}


# ---------------------------------------------------------------- profile detail API
def _profile_dir(prof):
    """Return the directory for a profile's config and soul files."""
    if prof == "default":
        return HOME_HERMES
    return os.path.join(PROFILES, prof)


def _count_skills(profile_dir):
    """Count installed skills in a profile's skills directory (recursive)."""
    skills_dir = os.path.join(profile_dir, "skills")
    if not os.path.exists(skills_dir):
        return 0
    count = 0
    for root, dirs, files in os.walk(skills_dir):
        if "SKILL.md" in files:
            count += 1
    return count


def _extract_lane_from_choice(choice):
    """Extract lane (provider) from a 'lane/model' choice string."""
    if not choice or "/" not in choice:
        return None
    return choice.split("/", 1)[0]


def load_profile_detail(prof):
    """Load config.yaml, .env, and SOUL.md for a profile, plus metadata."""
    import yaml
    pdir = _profile_dir(prof)
    cfg_file = os.path.join(pdir, "config.yaml")
    env_file = os.path.join(pdir, ".env")
    soul_file = os.path.join(pdir, "SOUL.md")

    result = {
        "profile": prof,
        "config": "",
        "env": "",
        "soul": "",
        "toolset": "safe",
        "skills_count": 0,
        "context_length": None,
        "provider": None,
        "lane": None,
    }

    # Load config.yaml for metadata
    cfg = {}
    try:
        if os.path.exists(cfg_file):
            result["config"] = open(cfg_file, encoding="utf-8").read()
            cfg = yaml.safe_load(result["config"]) or {}
    except Exception as e:
        result["config"] = f"# Error reading config: {e}"

    # Extract metadata from config
    if isinstance(cfg, dict):
        # Toolset
        toolsets = cfg.get("toolsets")
        if isinstance(toolsets, list) and toolsets:
            result["toolset"] = toolsets[0]  # primary toolset
        elif isinstance(toolsets, str):
            result["toolset"] = toolsets

        # Context length
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, dict):
            ctx = model_cfg.get("context_length")
            if ctx:
                result["context_length"] = ctx

        # Provider and lane from main model choice
        if isinstance(model_cfg, dict):
            main_choice = join_choice(model_cfg.get("provider"), model_cfg.get("default"))
            if main_choice:
                result["provider"] = model_cfg.get("provider")
                result["lane"] = _extract_lane_from_choice(main_choice)

    # Skills count
    result["skills_count"] = _count_skills(pdir)

    try:
        if os.path.exists(env_file):
            # Don't expose actual secret values, just show which vars are set
            lines = []
            for line in open(env_file, errors="replace"):
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    lines.append(f"{k.strip()}=***SET***")
            result["env"] = "\n".join(lines) if lines else "# (empty or no .env)"
    except Exception:
        result["env"] = "# (unreadable)"

    try:
        if os.path.exists(soul_file):
            result["soul"] = open(soul_file, encoding="utf-8").read()
        else:
            result["soul"] = "# No SOUL.md found for this profile"
    except Exception as e:
        result["soul"] = f"# Error reading SOUL.md: {e}"

    return result


def save_profile_config(prof, config_text):
    """Save config.yaml for a profile with backup."""
    import yaml
    pdir = _profile_dir(prof)
    cfg_file = os.path.join(pdir, "config.yaml")
    os.makedirs(BACKUP, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        # Validate YAML before writing
        yaml.safe_load(config_text)
        if os.path.exists(cfg_file):
            shutil.copy2(cfg_file, os.path.join(BACKUP, f"{prof}.{stamp}.yaml"))
        with open(cfg_file, "w") as fh:
            fh.write(config_text)
        # A hand edit can route to a lane this profile was never wired for.
        # Keys go straight in; a stanza re-dump only happens when one is missing,
        # so an edit that is already wired keeps its comments and layout.
        d = yaml.safe_load(config_text) or {}
        before = json.dumps(d.get("providers"), sort_keys=True)
        wired, left = wiring.complete(prof, d)
        if json.dumps(d.get("providers"), sort_keys=True) != before:
            wiring.write_cfg(prof, d)
        return {"ok": True, "backup": f"{prof}.{stamp}.yaml",
                "wired": [g["detail"] for g in wired], "unwired": [g["detail"] for g in left]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_profile_soul(prof, soul_text):
    """Save SOUL.md for a profile."""
    pdir = _profile_dir(prof)
    soul_file = os.path.join(pdir, "SOUL.md")
    try:
        with open(soul_file, "w") as fh:
            fh.write(soul_text)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- gateway restart
# One launchd gateway (ai.hermes.gateway) serves every profile, so a restart
# interrupts all of them. "busy" = a live registered session in any profile, or
# gateway.log written in the last BUSY_LOG_SECS (a turn is mid-flight).
HERMES_BIN = os.path.join(HOME_HERMES, "hermes-agent", ".hermes", "bin", "hermes")
GATEWAY_LOG = os.path.join(HOME_HERMES, "logs", "gateway.log")
BUSY_LOG_SECS = 90
IDLE_WAIT_MAX = 30 * 60
_restart = {"state": "idle", "since": None, "note": ""}


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def gateway_status():
    sessions = []
    for f in [os.path.join(HOME_HERMES, "runtime", "active_sessions.json")] + \
             glob.glob(os.path.join(PROFILES, "*", "runtime", "active_sessions.json")):
        try:
            entries = json.load(open(f)).get("entries") or []
        except Exception:
            continue
        prof = "default" if f.startswith(os.path.join(HOME_HERMES, "runtime")) else f.split(os.sep)[-3]
        for e in entries:
            if "pid" not in e or _pid_alive(e["pid"]):
                sessions.append(prof)
    try:
        log_age = int(time.time() - os.path.getmtime(GATEWAY_LOG))
    except OSError:
        log_age = None
    busy = bool(sessions) or (log_age is not None and log_age < BUSY_LOG_SECS)
    return {"busy": busy, "sessions": sessions, "log_age_s": log_age, "restart": dict(_restart)}


def _do_restart():
    r = subprocess.run([HERMES_BIN, "gateway", "restart"], capture_output=True, text=True, timeout=120)
    _restart.update(state="done" if r.returncode == 0 else "failed", since=int(time.time()),
                    note=(r.stdout + r.stderr).strip()[-400:])


def _restart_when_idle():
    t0 = time.time()
    while time.time() - t0 < IDLE_WAIT_MAX:
        if _restart["state"] != "waiting":
            return  # cancelled
        if not gateway_status()["busy"]:
            return _do_restart()
        time.sleep(15)
    _restart.update(state="gave-up", since=int(time.time()),
                    note="still busy after 30 min - restart by hand when ready")


def restart_gateway(mode):
    """mode: 'now' | 'when-idle' | 'cancel'."""
    if mode == "cancel":
        if _restart["state"] == "waiting":
            _restart.update(state="idle", since=int(time.time()), note="cancelled")
    elif mode == "now":
        _restart.update(state="restarting", since=int(time.time()), note="")
        threading.Thread(target=_do_restart, daemon=True).start()
    elif mode == "when-idle" and _restart["state"] != "waiting":
        _restart.update(state="waiting", since=int(time.time()), note="")
        threading.Thread(target=_restart_when_idle, daemon=True).start()
    return gateway_status()


# ---------------------------------------------------------------- HTTP handler
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=2, default=str)
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, open(UI, encoding="utf-8").read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, load_state())
        if self.path == "/api/catalog":
            return self._send(200, load_catalog())
        if self.path == "/api/wiring":
            return self._send(200, wiring.doctor(fix=False))
        if self.path == "/api/gateway":
            return self._send(200, gateway_status())
        # Profile detail API
        if self.path.startswith("/api/profile/"):
            # /api/profile/<name> or /api/profile/<name>/soul or /api/profile/<name>/config
            parts = self.path.split("/")
            if len(parts) >= 4:
                prof = urllib.parse.unquote(parts[3])
                if len(parts) == 4:
                    return self._send(200, load_profile_detail(prof))
                elif len(parts) == 5 and parts[4] == "soul":
                    return self._send(200, {"soul": load_profile_detail(prof)["soul"]})
                elif len(parts) == 5 and parts[4] == "config":
                    return self._send(200, {"config": load_profile_detail(prof)["config"]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})
        if self.path == "/api/plan":
            return self._send(200, plan_changes(req.get("changes") or {}))
        if self.path == "/api/apply":
            if req.get("apply") is not True:
                return self._send(400, {"error": "refusing: send apply=true. Review the plan diff first."})
            return self._send(200, apply_changes(req.get("changes") or {}))
        if self.path == "/api/wiring/fix":
            return self._send(200, wiring.doctor(fix=True))
        if self.path == "/api/gateway/restart":
            return self._send(200, restart_gateway(req.get("mode") or "when-idle"))
        if self.path == "/api/refresh":
            # Catalogue pull is FAST (~5s) and is what the user actually sees
            # in the dropdowns, so do it SYNCHRONOUSLY here and report the
            # result. The health sweep takes 6+ minutes, so that stays in the
            # background - but its output now goes to refresh.log instead of
            # DEVNULL, so a crash is visible instead of silent.
            import catalog
            try:
                summary = catalog.refresh()
            except Exception as e:
                summary = {"error": f"{type(e).__name__}: {e}"}
            log = open(os.path.join(HERE, "refresh.log"), "ab")
            subprocess.Popen([PY, os.path.join(HERE, "refresh.py"), "--health-only"],
                             stdout=log, stderr=subprocess.STDOUT, cwd=HERE)
            return self._send(200, {"started": True, "catalogue": summary})
        # Profile detail API - PUT for saving
        if self.path.startswith("/api/profile/"):
            parts = self.path.split("/")
            if len(parts) == 5:
                prof = urllib.parse.unquote(parts[3])
                if parts[4] == "config":
                    return self._send(200, save_profile_config(prof, req.get("config", "")))
                elif parts[4] == "soul":
                    return self._send(200, save_profile_soul(prof, req.get("soul", "")))
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass

    def do_PUT(self):
        # Delegate to do_POST for profile config/soul endpoints
        self.do_POST()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"Fleet dashboard on http://127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
