#!/usr/bin/env python3
"""Ensure a profile's providers: block defines every provider its model or
fallbacks reference. Fixes the bug where apply set model.provider to a provider
that had no stanza (writer -> groq => "Unknown provider 'groq'")."""
import os, shutil, sys, time, yaml

PROF = "/Users/user/.hermes/profiles"
BK = "/Users/user/Projects/fleet-dashboard/backups"

# canonical stanzas, taken from the profiles that already work
TEMPLATES = {
    "groq": {"api_key": "${GROQ_API_KEY}",
             "base_url": "https://api.groq.com/openai/v1",
             "default_model": "llama-3.3-70b-versatile"},
    "nvidia": {"api_key": "${NVIDIA_API_KEY}",
               "base_url": "https://integrate.api.nvidia.com/v1"},
    "openrouter": {"api_key": "${OPENROUTER_API_KEY}",
                   "base_url": "https://openrouter.ai/api/v1"},
    "inception": {"api_key": "${INCEPTION_API_KEY}",
                  "base_url": "https://api.inceptionlabs.ai/v1"},
}


def referenced(d):
    """Providers the config actually points at."""
    refs = set()
    m = d.get("model") or {}
    if isinstance(m, dict) and m.get("provider"):
        refs.add(m["provider"])
    for f in (d.get("fallback_providers") or []):
        if isinstance(f, dict) and f.get("provider"):
            refs.add(f["provider"])
    return refs


def fix(profile, dry=False):
    p = os.path.join(PROF, profile, "config.yaml")
    d = yaml.safe_load(open(p)) or {}
    pv = d.setdefault("providers", {})
    need = referenced(d) - set(pv.keys())
    if not need:
        return f"{profile}: OK - all referenced providers defined"
    lines = []
    for prov in sorted(need):
        if prov in TEMPLATES:
            pv[prov] = TEMPLATES[prov]
            lines.append(f"    + {prov}: added")
        else:
            lines.append(f"    ! {prov}: referenced but no template - ADD BY HAND")
    if dry:
        return f"{profile}: WOULD ADD " + "; ".join(lines)
    os.makedirs(BK, exist_ok=True)
    shutil.copy2(p, os.path.join(BK, f"{profile}.config.{time.strftime('%Y%m%d-%H%M%S')}.yaml"))
    with open(p, "w") as fh:
        yaml.safe_dump(d, fh, sort_keys=False, default_flow_style=False)
    return f"{profile}: " + "; ".join(lines) + f"  (backed up)"


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    targets = [a for a in sys.argv[1:] if not a.startswith("--")] or \
        sorted(os.path.basename(os.path.dirname(c))
               for c in [os.path.join(PROF, x, "config.yaml")
                         for x in os.listdir(PROF)]
               if os.path.exists(c))
    for t in targets:
        print(fix(t, dry))
