#!/usr/bin/env python3
"""Repoint stale fallback entries that reference a provider with no stanza.

auditor and wrench both listed `provider: openai, model: gpt-oss-120b` as a
fallback. No `openai` provider exists here, so if the primary ever failed they
would fall through to an unresolvable provider. gpt-oss-120b is free on Groq,
so map it there.
"""
import os, shutil, sys, time, yaml

PROF = "/Users/user/.hermes/profiles"
BK = "/Users/user/Projects/fleet-dashboard/backups"
# provider that has no stanza -> provider that does, per model
REMAP = {("openai", "gpt-oss-120b"): ("groq", "openai/gpt-oss-120b"),
         ("openai", "gpt-oss-20b"): ("groq", "openai/gpt-oss-20b")}


def fix(profile, dry=False):
    p = os.path.join(PROF, profile, "config.yaml")
    d = yaml.safe_load(open(p)) or {}
    pv = d.get("providers") or {}
    fbs = d.get("fallback_providers") or []
    out, changes = [], []
    for f in fbs:
        if not isinstance(f, dict):
            out.append(f); continue
        prov, mod = f.get("provider"), f.get("model")
        if prov in pv:
            out.append(f); continue
        new = REMAP.get((prov, mod))
        if new:
            out.append({"provider": new[0], "model": new[1]})
            changes.append(f"{prov}/{mod} -> {new[0]}/{new[1]}")
        else:
            out.append(f)
            changes.append(f"! {prov}/{mod} has no stanza and no remap - FIX BY HAND")
    if not changes:
        return f"{profile}: OK - every fallback resolves"
    if dry:
        return f"{profile}: WOULD " + "; ".join(changes)
    os.makedirs(BK, exist_ok=True)
    shutil.copy2(p, os.path.join(BK, f"{profile}.config.{time.strftime('%Y%m%d-%H%M%S')}.yaml"))
    d["fallback_providers"] = out
    with open(p, "w") as fh:
        yaml.safe_dump(d, fh, sort_keys=False, default_flow_style=False)
    return f"{profile}: " + "; ".join(changes) + "  (backed up)"


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    for t in [a for a in sys.argv[1:] if not a.startswith("--")]:
        print(fix(t, dry))
