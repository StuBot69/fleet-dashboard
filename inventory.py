#!/usr/bin/env python3
"""Read every Hermes profile config and return a normalized inventory."""
import json, os, glob

PROFILES = "/Users/user/.hermes/profiles"
GUARD_URL = "http://127.0.0.1:8790/v1"


def _yaml():
    import yaml
    return yaml


HOME_HERMES = "/Users/user/.hermes"


def _profile_dir(prof):
    """Return the directory for a profile's config and soul files."""
    if prof == "default":
        return HOME_HERMES
    return os.path.join(PROFILES, prof)


def _profile_paths():
    """(name, config_path) for every profile. 'default' is ~/.hermes itself, not
    under profiles/ - it is the Telegram/desktop orchestrator and must be listed."""
    out = [("default", os.path.join(HOME_HERMES, "config.yaml"))]
    for cfg in sorted(glob.glob(os.path.join(PROFILES, "*", "config.yaml"))):
        out.append((os.path.basename(os.path.dirname(cfg)), cfg))
    return out


def _load_soul(prof):
    """Load SOUL.md content for a profile, if it exists."""
    pdir = _profile_dir(prof)
    soul_file = os.path.join(pdir, "SOUL.md")
    try:
        if os.path.exists(soul_file):
            return open(soul_file, encoding="utf-8").read()
    except Exception:
        pass
    return ""


def collect():
    y = _yaml()
    out = []
    for prof, cfg in _profile_paths():
        try:
            d = y.safe_load(open(cfg)) or {}
        except Exception as e:
            out.append({"profile": prof, "error": str(e)})
            continue
        m = d.get("model") or {}
        if not isinstance(m, dict):
            m = {}
        fb = []
        for f in (d.get("fallback_providers") or []):
            if isinstance(f, dict):
                prov = f.get('provider')
                model = f.get('model')
                # Avoid double prefix if model already starts with "provider/"
                if model.startswith(f"{prov}/"):
                    fb.append(model)
                else:
                    fb.append(f"{prov}/{model}")
            else:
                fb.append(str(f))
        env_path = os.path.join(os.path.dirname(cfg), ".env")  # default: ~/.hermes/.env
        env_txt = ""
        if os.path.exists(env_path):
            try:
                env_txt = open(env_path, errors="replace").read()
            except Exception:
                pass
        soul = _load_soul(prof)
        out.append({
            "profile": prof,
            "model": m.get("default"),
            "provider": m.get("provider"),
            "fallbacks": fb,
            "via_guard": GUARD_URL in env_txt,
            "soul": soul,
        })
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), indent=2))
