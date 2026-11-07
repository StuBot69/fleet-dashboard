#!/usr/bin/env python3
"""Final health check across the fleet: does every config load, does every
provider/fallback resolve to a defined stanza, and what is the lane spread."""
import glob, os, yaml

PROF = "/Users/user/.hermes/profiles"
print(f"{'profile':<14}{'provider':<11}{'lane':<8}{'model':<44}fallbacks resolve?")
print("-" * 112)
bad = 0
for cfg in sorted(glob.glob(os.path.join(PROF, "*", "config.yaml"))):
    p = os.path.basename(os.path.dirname(cfg))
    d = yaml.safe_load(open(cfg)) or {}
    pv = d.get("providers") or {}
    m = d.get("model") or {}
    prov = m.get("default") if isinstance(m, dict) else None
    pname = m.get("provider") if isinstance(m, dict) else None
    lane = "?" 
    if pname == "nvidia":
        env = os.path.join(os.path.dirname(cfg), ".env")
        t = open(env, errors="replace").read() if os.path.exists(env) else ""
        lane = "guard" if "127.0.0.1:8790" in t else "DIRECT!"
    elif pname:
        lane = pname

    issues = []
    if pname and pname not in pv:
        issues.append(f"main provider '{pname}' NOT DEFINED")
    for f in (d.get("fallback_providers") or []):
        if isinstance(f, dict) and f.get("provider") not in pv:
            issues.append(f"fb {f.get('provider')}/{f.get('model')} NOT DEFINED")
    if issues:
        bad += 1
    print(f"{p:<14}{str(pname):<11}{lane:<8}{str(prov):<44}"
          f"{'; '.join(issues) if issues else 'yes'}")
print()
by = {}
for cfg in glob.glob(os.path.join(PROF, "*", "config.yaml")):
    d = yaml.safe_load(open(cfg)) or {}
    m = d.get("model") or {}
    if isinstance(m, dict):
        by[m.get("provider")] = by.get(m.get("provider"), 0) + 1
print("lane spread:", by)
print("configs with unresolved providers:", bad)
