#!/usr/bin/env python3
"""Live catalogue of every inference endpoint we can route a Hermes profile to.

Fetches each lane's own /models list (free-tier filter applied per lane) and
writes the result into fleet.db table `catalog`. Lanes that fail to answer keep
their last-known rows and are marked stale, so the dropdowns never go empty
because one provider is having a bad minute.

Lane names are the Hermes provider names used in config.yaml, so a catalogue
entry (lane, model) maps straight onto model.provider + model.default.

Run:  python3 catalog.py          (refresh all lanes, print summary)
"""
import json, os, sqlite3, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "fleet.db")
ENV = os.path.expanduser("~/.hermes/.env")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# lane -> (key var, models url, filter kind)
LANES = {
    "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/models", "or"),
    "nvidia":     ("NVIDIA_API_KEY",     "https://integrate.api.nvidia.com/v1/models", "nvidia"),
    "groq":       ("GROQ_API_KEY",       "https://api.groq.com/openai/v1/models", "groq"),
    "inception":  ("INCEPTION_API_KEY",  "https://api.inceptionlabs.ai/v1/models", "all"),
    "jasper_gptoss": (None, "http://192.168.55.52:8080/v1/models", "local"),
    "apinex":     ("APINEX_API_KEY",     "https://apinex.bond/api/public/models", "apinex"),
}

# Paid / direct lanes that have no free list endpoint we want to query.
# Static on purpose: these are deliberate choices, not free-pool discovery.
STATIC = [
    ("anthropic", "claude-haiku-5-5",   0, "PAID direct - Anthropic API, orchestrator default"),
    ("anthropic", "claude-sonnet-5-5",  0, "PAID direct - Anthropic API"),
    ("openrouter", "meta/muse-spark-1.3-contributor", 0, "PAID - Stu's deliberate paid fallback"),
    ("openrouter", "openai/gpt-6-astra-pro", 0, "PAID - occasional only, ask Stu first"),
]

# Non-chat models that show up in the Groq list (speech, guards, TTS).
GROQ_SKIP = ("whisper", "orpheus", "guard")


def load_keys():
    keys = {}
    try:
        for line in open(ENV, errors="replace"):
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                v = v.strip().strip("'\"")
                if v:
                    keys[k.strip()] = v  # last uncommented wins
    except FileNotFoundError:
        pass
    return keys


def get_json(url, key=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _is_free_or(m):
    p = m.get("pricing") or {}
    try:
        return float(p.get("prompt") or 0) == 0 and float(p.get("completion") or 0) == 0
    except (TypeError, ValueError):
        return False


def fetch_lane(lane, keys):
    """Returns (rows, error). rows = [(lane, model, is_free, note)]."""
    var, url, kind = LANES[lane]
    key = keys.get(var) if var else None
    if var and not key:
        return None, f"no {var} in ~/.hermes/.env"
    try:
        d = get_json(url, key, timeout=5 if kind == "local" else 20)
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:120]}"
    data = d.get("data") if isinstance(d, dict) else None
    if data is None and isinstance(d, dict) and "models" in d:
        data = d["models"]
    if not isinstance(data, list):
        return None, "unexpected payload"
    rows = []
    for m in data:
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        if kind == "or":
            if not _is_free_or(m):
                continue  # paid OR models are only listed via STATIC
            rows.append((lane, mid, 1, "FREE (OpenRouter shared 1k/day pool)"))
        elif kind == "nvidia":
            rows.append((lane, mid, 1, "FREE - needs nvidia-guard NVIDIA_BASE_URL"))
        elif kind == "groq":
            if any(s in mid.lower() for s in GROQ_SKIP):
                continue
            rows.append((lane, mid, 1, "FREE - Groq 200k tok/day per model"))
        elif kind == "all":
            # Inception: mercury-2.5 is a REASONING model. Its hidden reasoning tokens
            # count against max_tokens; at a tiny budget (16) it returns content=null,
            # finish_reason=length - which looks like "no reply". Use max_tokens >= 200.
            rows.append((lane, mid, 1, "FREE - quota unverified, ration"))
        elif kind == "local":
            rows.append((lane, mid, 1, "LOCAL - Jasper rig (only when powered on)"))
        elif kind == "apinex":
            # APInex free tier: THREE fields must agree before a model is free.
            #   id starts with 'free/'   -> it is a free-pool model
            #   allowFree is True        -> this ACCOUNT may use it free
            #   health == 'live'         -> the gateway is serving it
            #
            # TRAP: 34 of 44 models report allowFree=true, but 23 of those are
            # BARE ids (claude-opus-5.5, gemini-3.1-pro, ...) which are PAID
            # (dollarsPer1M > 0). And 10 'free/'-prefixed models report
            # allowFree=false and 402 on a free account. Filtering on
            # allowFree ALONE admits 23 paid models; filtering on the 'free/'
            # prefix alone admits 10 that hard-402.
            #
            # weight is the BURN RATE against the shared 20M weighted-token
            # pool, not a quality tier - models at the same weight are one
            # budget bucket, not several.
            if not mid.startswith("free/"):
                continue
            if m.get("allowFree") is not True:
                continue
            if (m.get("health") or "") != "live":
                continue
            if (m.get("dollarsPer1M") or 0) or (m.get("outputDollarsPer1M") or 0):
                continue
            w = m.get("weight")
            w = w if isinstance(w, int) else "?"
            ctx = m.get("contextWindow") or "?"
            tools = "tools" if m.get("supportsTools") else "no-tools"
            rows.append((lane, mid, 1,
                         f"FREE - APInex 20M weighted tok/day, weight {w}, {ctx} ctx, {tools}; daily check-in required"))
    return rows, None


def conn():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS catalog(
      lane TEXT, model TEXT, is_free INT, note TEXT,
      seen_at INT, live INT DEFAULT 1, dead INT DEFAULT 0,
      PRIMARY KEY(lane, model));
    CREATE TABLE IF NOT EXISTS catalog_lane(
      lane TEXT PRIMARY KEY, ok INT, error TEXT, checked_at INT, n INT);
    """)
    # Migration: add dead column if missing
    try:
        c.execute("ALTER TABLE catalog ADD COLUMN dead INT DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return c


def refresh():
    keys = load_keys()
    c = conn()
    now = int(time.time())
    summary = {}
    for lane in LANES:
        rows, err = fetch_lane(lane, keys)
        if rows is None:
            # keep last-known rows, mark stale
            c.execute("UPDATE catalog SET live=0 WHERE lane=?", (lane,))
            n = c.execute("SELECT COUNT(*) FROM catalog WHERE lane=?", (lane,)).fetchone()[0]
            c.execute("INSERT OR REPLACE INTO catalog_lane VALUES(?,?,?,?,?)",
                      (lane, 0, err, now, n))
            summary[lane] = f"STALE ({n} kept): {err}"
            continue
        c.execute("DELETE FROM catalog WHERE lane=?", (lane,))
        for (ln, mid, free, note) in rows:
            c.execute("INSERT OR REPLACE INTO catalog VALUES(?,?,?,?,?,1,0)",
                      (ln, mid, free, note, now))
        c.execute("INSERT OR REPLACE INTO catalog_lane VALUES(?,?,?,?,?)",
                  (lane, 1, None, now, len(rows)))
        summary[lane] = f"{len(rows)} models"
    # static paid/direct rows (always present, always live=1 by definition)
    # Inception: mercury-2.5 is a REASONING model. Its hidden reasoning tokens count
    # against max_tokens. With a tiny budget (e.g. 16) it returns content=null with
    # finish_reason=length, which looks like "no reply". Use max_tokens >= 200.
    # mercury-2 (non-reasoning-heavy) answers at small budgets.
    for (ln, mid, free, note) in STATIC:
        c.execute("INSERT OR REPLACE INTO catalog VALUES(?,?,?,?,?,1,0)",
                  (ln, mid, free, note, now))
    c.commit()
    c.close()
    return summary


def load_catalog():
    """Return catalog as list of dicts: [{lane, model, is_free, note, live, dead}, ...]"""
    c = conn()
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute(
            "SELECT lane, model, is_free, note, live, dead FROM catalog WHERE is_free=1 AND live=1 AND dead=0 ORDER BY lane, model"
        ).fetchall()
        items = [dict(r) for r in rows]
        # Filter to chat models only
        return [item for item in items if is_chat_model(item["model"], item.get("lane"))]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def is_chat_model(model_id: str, lane: str = None) -> bool:
    """Heuristic: skip embeddings, guards, parse, OCR, TTS, ASR, video, image, translation, etc.

    `lane` is optional but matters: some skip terms are lane-specific. The
    "kimi-k" term excludes moonshotai/kimi-k2.6 on the nvidia lane, but the
    APInex model `free/kimi-k3` is a healthy 1M-ctx chat model and MUST stay
    in the catalogue. Without the lane check the term silently drops it from
    the dropdown AND from the health sweep.
    """
    # Lane-specific exclusions, applied before the shared list.
    lane_specific = {
        "nvidia": ("kimi-k",),
    }
    skip_terms = tuple(lane_specific.get(lane, ())) + (
        "embed", "guard", "parse", "ocr", "tts", "asr", "translate",
        "video", "image", "vision", "audio", "speech", "detector",
        "calibration", "super-resolution", "upscale", "reward",
        "nemotron-nano-3", "nemotron-4-340b-reward", "nemotron-3-embed",
        "nvclip", "nv-embed", "riva-", "parakeet", "chatterbox",
        "diffusiongemma", "ising-", "cosmos-", "neva-", "vila",
        "palmyra-", "arctic-", "recurrentgemma", "codegemma-2b",
        "gemma-2b",  # Gemma 2 2B is small but still chat
        "phi-3-vision", "kosmos-2", "fuyu-", "deplot",
        "mistral-nemo-minitron", "mistral-nemo-12b",
        "llama-guard", "llama2-70b", "codellama-70b",
        # NOTE: "muse-glimmer" was here, but meta/muse-glimmer-30b IS a working
        # chat model on the NVIDIA lane (free, tool-calling verified 19 Oct 26).
        # Excluding it left the free Muse selectable in the dropdown but with
        # no health row, so it could never show a green status. Keep Muse.
        # "kimi-k" moved to lane_specific above (nvidia only) - APInex's
        # free/kimi-k3 is a healthy 1M-ctx chat model.
        "zamba2", "yi-large",
        "jamba-", "sea-lion", "dbrx-", "starcoder2",
        "granite-", "qwen-image", "wan2", "flux-",
    )
    m = model_id.lower()
    return not any(term in m for term in skip_terms)


if __name__ == "__main__":
    for k, v in refresh().items():
        print(f"{k:14} {v}")
