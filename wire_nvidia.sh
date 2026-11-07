#!/bin/bash
# Route profiles onto the DIRECT NVIDIA lane through nvidia-guard (free, no token cap).
# Appends the key + guard base URL to each profile .env, backing up first.
set -u
PY=/Users/user/.hermes/hermes-agent/venv/bin/python3
STAMP=$(date +%Y%m%d-%H%M%S)
BK=/Users/user/Projects/fleet-dashboard/backups

NVIDIA_KEY=$("$PY" - <<'EOF'
import os
last=None
for line in open(os.path.expanduser("~/.hermes/.env"), errors="replace"):
    s=line.strip()
    if s.startswith("NVIDIA_API_KEY="):
        v=s.split("=",1)[1].strip().strip("'\"")
        if v: last=v
print(last or "")
EOF
)

if [ -z "$NVIDIA_KEY" ]; then echo "FATAL: no NVIDIA_API_KEY found in ~/.hermes/.env"; exit 1; fi
mkdir -p "$BK"

for p in "$@"; do
  E="/Users/user/.hermes/profiles/$p/.env"
  [ -f "$E" ] || { echo "$p: no .env, skipped"; continue; }
  cp -p "$E" "$BK/$p.env.$STAMP"
  # drop any prior NVIDIA lines so we don't accumulate dupes
  grep -v '^NVIDIA_API_KEY=' "$E" > "$E.tmp" && mv "$E.tmp" "$E"
  grep -v '^NVIDIA_BASE_URL=' "$E" > "$E.tmp" && mv "$E.tmp" "$E"
  {
    echo ""
    echo "# --- added 28 Sep 26: route via nvidia-guard (direct NVIDIA lane, \$0/token) ---"
    echo "NVIDIA_API_KEY=$NVIDIA_KEY"
    echo "NVIDIA_BASE_URL=http://127.0.0.1:8790/v1"
  } >> "$E"
  chmod 600 "$E"
  printf '%-9s OK  key=%s  guard=%s  backup=%s\n' "$p" \
    "$(grep -c '^NVIDIA_API_KEY=' "$E")" "$(grep -c '^NVIDIA_BASE_URL=' "$E")" \
    "$(basename "$BK/$p.env.$STAMP")"
done
