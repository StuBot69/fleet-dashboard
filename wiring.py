#!/usr/bin/env python3
"""Provider wiring: make sure every lane a profile routes to actually works.

A profile can only use a lane (main model or fallback rung) when BOTH are true:
  1. config.yaml has a providers:<lane> stanza (unless Hermes has it built in)
  2. the profile's own .env holds the key that stanza references

Anything that edits model/fallback_providers (the dashboard, `hermes model`,
a hand edit, a bot editing itself) can leave one of those missing, and the rung
goes silently inert. This module finds those gaps and closes them:

  - stanzas are copied from the fleet itself: the most common stanza for that
    lane across all profiles. Adding a new lane to ONE profile by hand makes it
    available to all of them; nothing here needs updating when the fleet changes.
  - keys are copied from ~/.hermes/.env (the master), backed up first.
    Key values are never returned or logged, only key names.

CLI (run under the Hermes venv, needs PyYAML):
  python3 wiring.py            report gaps, change nothing
  python3 wiring.py --fix      close every fixable gap (backups first)
"""
import glob, json, os, re, shutil, sys, time
from collections import Counter

import yaml

HOME_HERMES = "/Users/user/.hermes"
PROFILES = os.path.join(HOME_HERMES, "profiles")
MASTER_ENV = os.path.join(HOME_HERMES, ".env")
BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")

# Providers Hermes knows without a providers: stanza in config.yaml.
BUILTIN_PROVIDERS = {"anthropic"}
REF = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def profile_dir(prof):
    return HOME_HERMES if prof == "default" else os.path.join(PROFILES, prof)


def cfg_path(prof):
    return os.path.join(profile_dir(prof), "config.yaml")


def env_path(prof):
    return os.path.join(profile_dir(prof), ".env")


def all_profiles():
    names = ["default"] if os.path.exists(cfg_path("default")) else []
    names += sorted(os.path.basename(os.path.dirname(p))
                    for p in glob.glob(os.path.join(PROFILES, "*", "config.yaml")))
    return names


def load_cfg(prof):
    try:
        return yaml.safe_load(open(cfg_path(prof))) or {}
    except Exception:
        return {}


def env_values(path):
    """{VAR: value} from a .env file; last assignment wins, like a shell."""
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        out[k] = v.strip().strip('"').strip("'")
    return out


def lanes_used(d):
    """Every provider a config routes to: main model first, then fallback rungs."""
    out = []
    m = (d.get("model") or {}).get("provider")
    if m:
        out.append(m)
    for f in d.get("fallback_providers") or []:
        if isinstance(f, dict) and f.get("provider") and f["provider"] not in out:
            out.append(f["provider"])
    return out


def key_var(stanza):
    """The env var a stanza reads its key from, or None (keyless/local lane)."""
    if not isinstance(stanza, dict):
        return None
    if stanza.get("key_env"):
        return stanza["key_env"]
    m = REF.match(str(stanza.get("api_key") or ""))
    return m.group(1) if m else None


def _shareable(stanza):
    """Only copy stanzas that reference keys by ${VAR}; never spread a literal key."""
    if not isinstance(stanza, dict):
        return False
    ak = stanza.get("api_key")
    return ak is None or bool(REF.match(str(ak)))


def fleet_stanzas():
    """lane -> most common shareable stanza across every profile."""
    seen = {}
    for prof in all_profiles():
        for lane, st in ((load_cfg(prof).get("providers") or {}).items()):
            if _shareable(st):
                seen.setdefault(lane, Counter())[json.dumps(st, sort_keys=True)] += 1
    return {lane: json.loads(c.most_common(1)[0][0]) for lane, c in seen.items()}


def audit(prof, d=None, stanzas=None, master=None):
    """Gaps for one profile -> [{lane, kind, var?, fixable, detail}]. Writes nothing.
    Pass `d` to audit a config you are about to write rather than what is on disk."""
    d = load_cfg(prof) if d is None else d
    stanzas = fleet_stanzas() if stanzas is None else stanzas
    master = env_values(MASTER_ENV) if master is None else master
    have_env = env_values(env_path(prof))
    defined = d.get("providers") or {}
    gaps = []
    for lane in lanes_used(d):
        if lane in BUILTIN_PROVIDERS and lane not in defined:
            continue
        st = defined.get(lane)
        if st is None:
            ok = lane in stanzas
            gaps.append({"lane": lane, "kind": "stanza", "fixable": ok,
                         "detail": "copy stanza from fleet" if ok else
                         f"no profile defines a '{lane}' stanza - add one by hand once"})
            st = stanzas.get(lane)
        var = key_var(st)
        if var and not have_env.get(var):
            ok = bool(master.get(var))
            gaps.append({"lane": lane, "kind": "key", "var": var, "fixable": ok,
                         "detail": f"copy {var} from ~/.hermes/.env" if ok else
                         f"{var} is not in ~/.hermes/.env either"})
    return gaps


def _backup(path, tag):
    if os.path.exists(path):
        os.makedirs(BACKUP, exist_ok=True)
        shutil.copy2(path, os.path.join(BACKUP, f"{tag}.{time.strftime('%Y%m%d-%H%M%S')}"))


def complete(prof, d, stanzas=None, master=None):
    """Close every fixable gap: add stanzas to `d` in place and append missing keys
    to the profile's .env. The caller writes `d`. Returns (fixed, unfixable)."""
    stanzas = fleet_stanzas() if stanzas is None else stanzas
    master = env_values(MASTER_ENV) if master is None else master
    gaps = audit(prof, d, stanzas, master)
    fixed, left, keys = [], [], []
    for g in gaps:
        if not g["fixable"]:
            left.append(g)
        elif g["kind"] == "stanza":
            d.setdefault("providers", {})[g["lane"]] = stanzas[g["lane"]]
            fixed.append(g)
        elif g["var"] not in keys:
            keys.append(g["var"])
            fixed.append(g)
    if keys:
        p = env_path(prof)
        _backup(p, f"{prof}.env")
        with open(p, "a") as fh:
            fh.write(f"\n# added by fleet-dashboard wiring {time.strftime('%Y-%m-%d %H:%M')}\n")
            for var in keys:
                fh.write(f"{var}={master[var]}\n")
    return fixed, left


def _insert_stanzas(text, added):
    """Add stanzas to config text without re-dumping it, so comments survive.
    Returns None when the file's layout is not one we can patch safely."""
    block = "".join("  " + ln + "\n" for ln in yaml.safe_dump(
        added, sort_keys=False, default_flow_style=False, allow_unicode=True).splitlines())
    m = re.search(r"^providers:[ \t]*(\{\})?[ \t]*(#.*)?$", text, re.M)
    if not m:
        return text.rstrip("\n") + "\nproviders:\n" + block
    if m.group(1):
        return text[:m.start()] + "providers:\n" + block.rstrip("\n") + text[m.end():]
    first = re.search(r"^([ \t]+)\S", text[m.end() + 1:], re.M)
    if first and first.group(1) != "  ":
        return None  # existing stanzas use another indent
    return text[:m.end() + 1] + block + text[m.end() + 1:]


def write_cfg(prof, d):
    """Write `d`. When it only adds provider stanzas to what is on disk, patch the
    text in place (keeps comments); otherwise fall back to a full dump."""
    path = cfg_path(prof)
    _backup(path, f"{prof}.yaml")
    text = open(path).read() if os.path.exists(path) else ""
    disk = yaml.safe_load(text) or {}
    have = disk.get("providers") or {}
    added = {k: v for k, v in (d.get("providers") or {}).items() if k not in have}
    if added and {**disk, "providers": {**have, **added}} == d:
        patched = _insert_stanzas(text, added)
        try:
            ok = patched is not None and yaml.safe_load(patched) == d
        except yaml.YAMLError:
            ok = False
        if ok:
            with open(path, "w") as fh:
                fh.write(patched)
            return
    with open(path, "w") as fh:
        yaml.safe_dump(d, fh, sort_keys=False, default_flow_style=False, allow_unicode=True)


def doctor(fix=False):
    """Fleet-wide audit (and optional repair) -> {profile: {gaps|fixed, left}}."""
    stanzas, master = fleet_stanzas(), env_values(MASTER_ENV)
    out = {}
    for prof in all_profiles():
        d = load_cfg(prof)
        if not d:
            continue
        if not fix:
            gaps = audit(prof, d, stanzas, master)
            if gaps:
                out[prof] = {"gaps": gaps}
            continue
        before = json.dumps(d.get("providers"), sort_keys=True)
        fixed, left = complete(prof, d, stanzas, master)
        if json.dumps(d.get("providers"), sort_keys=True) != before:
            write_cfg(prof, d)
        if fixed or left:
            out[prof] = {"fixed": fixed, "left": left}
    return out


if __name__ == "__main__":
    res = doctor(fix="--fix" in sys.argv)
    if not res:
        print("wiring: every lane in every profile is wired")
    for prof, r in res.items():
        for k in ("gaps", "fixed", "left"):
            for g in r.get(k, []):
                print(f"{prof:12} {k:5} {g['lane']:14} {g['kind']:6} {g['detail']}")
    sys.exit(1 if any(r.get("gaps") or r.get("left") for r in res.values()) else 0)
