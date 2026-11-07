import json, os, urllib.request

ENV = os.path.expanduser("~/.hermes/.env")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

key = None
for line in open(ENV, errors="replace"):
    s = line.strip()
    if s.startswith("OPENROUTER_API_KEY="):
        v = s.split("=", 1)[1].strip().strip("'\"")
        if v:
            key = v
req = urllib.request.Request("https://openrouter.ai/api/v1/models")
req.add_header("Authorization", f"Bearer {key}")
req.add_header("User-Agent", UA)
data = json.load(urllib.request.urlopen(req, timeout=30))["data"]

# the slugs Stu just assigned, via provider openrouter
CHECK = ["nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3.5-lightning-30b-a3b",
         "openai/gpt-oss-20b", "openai/gpt-oss-120b", "stealth/space-bunny-alpha",
         "thinkingmachines/inkling-small:free", "dots-studio/dots-3-note-preview:free",
         "poolside/laguna-s-2.1:free"]

by = {m["id"]: m for m in data}
print(f"{'slug (as assigned)':<48}{'listed?':<9}{'prompt$/tok':<14}{'completion$/tok':<16}FREE?")
print("-" * 100)
for s in CHECK:
    m = by.get(s)
    if not m:
        print(f"{s:<48}{'NO':<9}{'-':<14}{'-':<16}slug not in catalogue")
        continue
    p = m.get("pricing") or {}
    pr = float(p.get("prompt") or 0)
    co = float(p.get("completion") or 0)
    free = (pr == 0 and co == 0)
    flag = "FREE" if free else "*** PAID ***"
    if free:
        flag = f"FREE (x{pr:g}/{co:g})"
    print(f"{s:<48}{'yes':<9}{pr:<14g}{co:<16g}{flag}")

# what the :free twins look like, for contrast
print("\n:free twins of the paid slugs above:")
for s in CHECK:
    if s.endswith(":free"):
        continue
    t = s + ":free"
    m = by.get(t)
    if m:
        p = m.get("pricing") or {}
        print(f"  {t:<48} prompt={p.get('prompt')} completion={p.get('completion')}")
    else:
        print(f"  {t:<48} NOT IN CATALOGUE")
