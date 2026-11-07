import json, subprocess, sys
PY = "/Users/user/.hermes/hermes-agent/venv/bin/python3"
raw = subprocess.run([PY, "inventory.py"], capture_output=True, text=True,
                     cwd="/Users/user/Projects/fleet-dashboard")
d = json.loads(raw.stdout)

PREV = {
 "archivist":"thinkingmachines/inkling-small:free","astra-slim":"openai/gpt-6-astra-pro",
 "auditor":"dots-studio/dots-3-note-preview:free","coder":"poolside/laguna-s-2.1:free",
 "forge":"mercury-2","gust":"openai/gpt-oss-120b","ledger":"thinkingmachines/inkling-small:free",
 "messenger":"dots-studio/dots-3-note-preview:free","metrics":"dots-studio/dots-3-note-preview:free",
 "planner":"nvidia/nemotron-3-ultra-550b-a55b:free","qa":"dots-studio/dots-3-note-preview:free",
 "quill":"nvidia/nemotron-3-super-120b-a12b","scope":"stealth/space-bunny-alpha",
 "system-bot":"dots-studio/dots-3-note-preview:free",
 "threat-model":"nvidia/nemotron-3-super-120b-a12b:free",
 "validator":"poolside/laguna-s-2.1:free","wrench":"cohere/north-mini-code:free",
 "writer":"dots-studio/dots-3-note-preview:free"}

print("=== CURRENT ===")
for p in d:
    g = "guard" if p.get("via_guard") else "    "
    print(f"{p['profile']:<14}{str(p.get('provider')):<11}{g} {p.get('model')}")

print("\n=== CHANGED SINCE MY LAST INVENTORY ===")
ch = [(p["profile"], PREV.get(p["profile"]), p.get("model")) for p in d
      if PREV.get(p["profile"]) != p.get("model")]
for n, o, v in ch:
    print(f"  {n}: {o}  ->  {v}")
if not ch:
    print("  (no model changes)")

by = {}
for p in d:
    by[p.get("provider")] = by.get(p.get("provider"), 0) + 1
print(f"\nprovider spread: {by}  (was openrouter=15)")

print("\n=== dead ling slug in configs ===")
g = subprocess.run("grep -rho 'inclusionai/ling-3.0-flash-fin:free' "
                   "/Users/user/.hermes/profiles/*/config.yaml 2>/dev/null | wc -l",
                   shell=True, capture_output=True, text=True).stdout.strip()
s = subprocess.run("grep -rho 'ling-3.0-flash-sante' "
                   "/Users/user/.hermes/profiles/*/config.yaml 2>/dev/null | wc -l",
                   shell=True, capture_output=True, text=True).stdout.strip()
print(f"  fin:free={g}  (was 20)   sante:free={s}  (must be 0)")
