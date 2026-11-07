#!/usr/bin/env python3
"""Add GROQ_API_KEY to a profile's .env (idempotent, backs up first).
gpt-oss is free on Groq but PAID on OpenRouter, so a bot moved to the groq
provider needs the key in its own .env or the provider is inert."""
import os, shutil, sys, time, json, urllib.request

PROF = "/Users/user/.hermes/profiles"
BK = "/Users/user/Projects/fleet-dashboard/backups"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def last_env_value(var):
    last = None
    for line in open(os.path.expanduser("~/.hermes/.env"), errors="replace"):
        s = line.strip()
        if s.startswith(var + "="):
            v = s.split("=", 1)[1].strip().strip("'\"")
            if v:
                last = v
    return last


def set_env(profile, var, value, comment):
    p = os.path.join(PROF, profile, ".env")
    if not os.path.exists(p):
        return f"{profile}: no .env"
    shutil.copy2(p, os.path.join(BK, f"{profile}.env.{time.strftime('%Y%m%d-%H%M%S')}"))
    lines = [l for l in open(p, errors="replace").read().splitlines()
             if not l.startswith(var + "=")]
    lines += ["", f"# {comment}", f"{var}={value}"]
    open(p, "w").write("\n".join(lines) + "\n")
    os.chmod(p, 0o600)
    return f"{profile}: {var} set ({sum(1 for l in open(p) if l.startswith(var+'='))} line)"


def groq_check(key, model):
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": "Say OK"}],
                       "max_tokens": 8}).encode()
    req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status
    except Exception as e:
        return getattr(e, "code", type(e).__name__)


if __name__ == "__main__":
    os.makedirs(BK, exist_ok=True)
    gq = last_env_value("GROQ_API_KEY")
    if not gq:
        sys.exit("FATAL: no GROQ_API_KEY in ~/.hermes/.env")
    for prof in sys.argv[1:]:
        print(set_env(prof, "GROQ_API_KEY", gq,
                      "added 28 Sep 26: groq direct lane "
                      "(gpt-oss is free here, paid on OpenRouter)"))
    print()
    for m in ("openai/gpt-oss-20b", "openai/gpt-oss-120b"):
        print(f"  groq {m:<22} HTTP {groq_check(gq, m)}")
