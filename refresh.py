#!/usr/bin/env python3
"""
Fleet Dashboard — daily health refresh.

Probes every candidate model on every lane we hold and writes the result into
fleet.db. It NEVER edits a profile config: it only records what is alive, free,
and tool-capable, plus current per-lane usage. apply.py is a separate,
explicitly-invoked step.

Run:  python3 refresh.py          (full sweep)
      python3 refresh.py --quick  (skip tool probes, status codes only)
"""
import json, os, sqlite3, time, urllib.request, urllib.error

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.db")
ENV = os.path.expanduser("~/.hermes/.env")

# Health-probe ceiling, in seconds.
#
# This was 45s, which was too tight for the slow reasoning models on the
# NVIDIA lane: z-ai/glm-5.3-flash answered in 115.6s, 227.4s and 276.8s
# across three live probes on 30 Oct 26. At 45s the probe gave up first and
# wrote http=None + "TimeoutError: The read operation timed out" into
# fleet.db, so a working model was rendered as broken.
#
# 300s is the production ceiling, not a guess: nvidia-guard hard-times-out at
# 300s, so anything slower than that is genuinely unusable in the fleet too.
# Probing at the same ceiling keeps the sweep honest in both directions.
#
# Override with FLEET_PROBE_TIMEOUT if a lane needs different patience.
try:
    PROBE_TIMEOUT = max(30, int(os.environ.get("FLEET_PROBE_TIMEOUT", "300")))
except (TypeError, ValueError):
    PROBE_TIMEOUT = 300
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

LANES = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
    "nvidia":     "https://integrate.api.nvidia.com/v1/chat/completions",
    "groq":       "https://api.groq.com/openai/v1/chat/completions",
    "inception":  "https://api.inceptionlabs.ai/v1/chat/completions",
    "cloudflare": "https://api.cloudflare.com/client/v4/accounts/6ba1a0f52cfe6a4d3a2b4e29b1f2d0a1/ai/run",
    "jasper_gptoss": "http://192.168.55.52:8080/v1/chat/completions",
    "apinex":     "https://api.apinex.bond/v1/chat/completions",
}


def load_candidates_from_catalog():
    """Build CANDIDATES from live catalog — free chat models only."""
    import catalog
    items = catalog.load_catalog()
    candidates = []
    for item in items:
        lane = item["lane"]
        model = item["model"]
        note = item["note"]
        # Only probe lanes we have chat endpoints for
        if lane in LANES:
            candidates.append((lane, model, 1, note))
    return candidates


def load_last_known_candidates():
    """Fallback: last known good catalog from DB (includes stale entries)."""
    import sqlite3
    DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.db")
    if not os.path.exists(DB):
        return []
    try:
        c = sqlite3.connect(DB)
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT lane, model, is_free, note FROM catalog
            WHERE is_free=1 AND dead=0 AND lane IN ('openrouter','nvidia','groq','inception','jasper_gptoss','apinex')
            ORDER BY lane, model
        """).fetchall()
        c.close()
        candidates = []
        for r in rows:
            lane, model, is_free, note = r["lane"], r["model"], r["is_free"], r["note"]
            if lane in LANES:
                candidates.append((lane, model, is_free, note))
        return candidates
    except Exception:
        return []


# Candidate models worth tracking. Free-first, plus the paid safety nets Stu
# deliberately keeps. Each: (lane, model_id, is_free, note)
# This is now DYNAMIC — loaded from catalog at runtime.
# Kept for reference / fallback if catalog is empty.
CANDIDATES = [
    ("openrouter", "stealth/space-bunny-alpha",              1, "FREE - Stu rates frontier. default candidate"),
    ("openrouter", "thinkingmachines/inkling-small:free",    1, "FREE - 12B/276B MoE, agentic+RAG; harness-gated (403 via curl, fine in Hermes)"),
    ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free", 1, "FREE - shares OR 1k/day pool"),
    ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free", 1, "FREE - 1M ctx"),
    ("openrouter", "cohere/north-mini-code:free",            1, "FREE - tools OK"),
    ("openrouter", "meta/muse-spark-1.3-contributor",        0, "PAID - Stu's deliberate paid fallback"),
    ("openrouter", "openai/gpt-6-astra-pro",                 0, "PAID - occasional only, ask Stu first"),
    ("nvidia",     "nvidia/nemotron-3-super-120b-a12b",      1, "FREE - 1M ctx, tools OK, 10k req/day via guard"),
    ("nvidia",     "nvidia/nemotron-3-ultra-550b-a55b",      1, "FREE - 1M ctx, tools OK"),
    ("nvidia",     "nvidia/nemotron-3.5-lightning-30b-a3b",  1, "FREE - 1M ctx, tools OK, fast"),
    ("nvidia",     "nvidia/nemotron-3.5-super-vl-120b-a12b", 1, "FREE - 1M ctx, multimodal, agentic, tools OK"),
    ("groq",       "openai/gpt-oss-120b",                    1, "FREE - 200k tok/day, publishes rate-limit headers"),
    ("groq",       "openai/gpt-oss-20b",                     1, "FREE"),
    ("inception",  "mercury-2",                              1, "FREE - unverified quota, ration"),
    ("inception",  "mercury-2.5",                            1, "FREE - reasoning model; needs max_tokens >= 200 or content comes back null"),
]


def load_keys():
    keys, last = {}, {}
    try:
        for line in open(ENV, errors="replace"):
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if v:
                last[k] = v          # LAST uncommented wins
    except FileNotFoundError:
        pass
    for lane, var in (("openrouter", "OPENROUTER_API_KEY"), ("nvidia", "NVIDIA_API_KEY"),
                      ("groq", "GROQ_API_KEY"), ("inception", "INCEPTION_API_KEY"),
                      ("cloudflare", "CLOUDFLARE_API_TOKEN"),
                      ("apinex", "APINEX_API_KEY")):
        if last.get(var):
            keys[lane] = last[var]
    # jasper_gptoss is local, no API key needed
    if "jasper_gptoss" in LANES:
        keys.setdefault("jasper_gptoss", "")
    return keys


def probe(lane, model, key, with_tools=True):
    """Return dict with http status, latency, tool_ok, error.

    tools: 1 = a tool call came back, 0 = model answered in prose,
           -1 = INCONCLUSIVE (rejected tool_choice, or no signal).
    A 0 from a single probe is NOT proof a model cannot tool-call: verified
    counter-example nvidia/nemotron-3-super-120b-a12b scores 0 here while quill
    drives real tool calls through it daily. Only 1 is trustworthy.
    """
    if lane == "cloudflare":
        return {"http": None, "tools": -1, "err": "cloudflare probed separately"}

    def send(force_tools):
        body = {
            "model": model,
            "messages": [{"role": "user",
                          "content": "Use the ping tool now. Do not answer in text."}],
            "max_tokens": 256,  # reasoning models (Inception mercury-2.5) spend hidden tokens first; 64 returned content=null
        }
        if force_tools:
            body["tools"] = [{"type": "function", "function": {
                "name": "ping", "description": "A no-op liveness probe. Call it.",
                "parameters": {"type": "object", "properties": {}, "required": []}}}]
            body["tool_choice"] = "required"
        data = json.dumps(body).encode()
        req = urllib.request.Request(LANES[lane], data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("User-Agent", UA)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    msg = (json.loads(raw).get("choices") or [{}])[0].get("message") or {}
                    return r.status, round(time.time() - t0, 1), bool(msg.get("tool_calls")), None
                except Exception:
                    return r.status, round(time.time() - t0, 1), None, None
        except urllib.error.HTTPError as e:
            return e.code, None, None, e.read().decode("utf-8", "replace")[:180]
        except Exception as e:
            return None, None, None, f"{type(e).__name__}: {e}"[:180]

    if not with_tools:
        st, lat, _t, err = send(False)
        return {"http": st, "lat": lat, "tools": -1, "err": err}

    st, lat, tools, err = send(True)
    # Model refuses forced tool_choice -> it may still support tools. Re-probe
    # liveness plainly and report INCONCLUSIVE rather than a false negative.
    # But distinguish: 400 "model not found" is a hard failure, not tool_choice rejection.
    if st == 400 and tools is None:
        err_lower = (err or "").lower()
        is_model_not_found = any(kw in err_lower for kw in (
            "not found", "does not exist", "unknown model", "model_not_found",
            "invalid model", "no endpoints found", "unavailable"
        ))
        if is_model_not_found:
            # Hard failure - model doesn't exist at this endpoint
            return {"http": st, "lat": lat, "tools": -1, "err": err}
        # Tool choice rejected - may still support tools, probe without tools
        st2, lat2, _t2, err2 = send(False)
        return {"http": st2, "lat": lat2, "tools": -1,
                "err": (err or "") + " [rejected tool_choice; tool support UNKNOWN]"}
    if tools is None:
        tools = -1
    return {"http": st, "lat": lat, "tools": tools, "err": err}


def conn():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS health(
      lane TEXT, model TEXT, is_free INT, note TEXT,
      http INT, lat REAL, tools INT, err TEXT, checked_at INT,
      runs INT DEFAULT 0, tools_yes INT DEFAULT 0, tools_no INT DEFAULT 0,
      PRIMARY KEY(lane, model));
    CREATE TABLE IF NOT EXISTS lane_usage(
      lane TEXT, used INT, limit_val INT, checked_at INT, PRIMARY KEY(lane));
    CREATE TABLE IF NOT EXISTS history(
      id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT, at INT);
    """)
    return c


def prune_orphan_health(c, candidates):
    """Delete health rows for models no longer in the catalogue and not
    referenced by any profile config. Returns the number of rows removed.

    See the comment at the call site for why this exists and why the
    "referenced by a profile" condition matters.
    """
    # The "still current" set is the FULL catalogue table, NOT the candidate
    # list. catalog.load_catalog() filters to is_free=1 AND live=1 AND
    # dead=0 chat models, so the candidates are a strict subset - using them
    # would wrongly prune health rows for catalogue models that are merely
    # non-chat, paid, or already marked dead (marked-dead models must KEEP
    # their row so the UI can show "404 dead" rather than "no sweep data").
    current = set()
    try:
        for r in c.execute("SELECT lane, model FROM catalog WHERE live=1"):
            current.add((r[0], r[1]))
    except Exception:
        current = {(lane, model) for lane, model, _f, _n in candidates}

    # Models any profile still points at. These keep their health row even if
    # they have dropped out of the catalogue, so a profile's badge keeps
    # showing its last real result instead of going blank.
    #
    # A model can be referenced from SEVERAL places, and inventory.collect()
    # only reads model.default / model.provider / fallback_providers. It does
    # NOT read:
    #   - providers.<lane>.default_model   (e.g. openrouter default_model:
    #                                       stealth/space-bunny-alpha)
    #   - fallbacks:<role>                  (top-level role -> list of models)
    #   - routing:<role>                    (coding/execution/planning/...)
    #   - named model stanzas (reasoning_model.model, model_zoo entries)
    # A prune built on inventory alone deleted health rows for models the
    # fleet still actively uses, so the reference set is built by scanning
    # every model-bearing field in every profile config.
    #
    # References are stored VERBATIM (e.g. "stealth/space-bunny-alpha",
    # "nvidia/z-ai/glm-5.3-flash"). Matching happens later, against both the
    # bare native id and the lane-prefixed form, with exact equality.
    referenced_ids = set()
    try:
        import yaml as _yaml
        import glob as _glob
        cfgs = [os.path.join(os.path.expanduser("~/.hermes"), "config.yaml")]
        cfgs += sorted(_glob.glob(os.path.expanduser("~/.hermes/profiles/*/config.yaml")))
        for cfg in cfgs:
            try:
                d = _yaml.safe_load(open(cfg, encoding="utf-8")) or {}
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            for _lane, _p in (d.get("providers") or {}).items():
                if isinstance(_p, dict) and _p.get("default_model"):
                    referenced_ids.add(str(_p["default_model"]))
            # routing:<role> -> "lane/model" (scalar) AND fallbacks:<role> ->
            # list of "lane/model". Both are live references.
            for _role, _m in (d.get("routing") or {}).items():
                if isinstance(_m, str) and "/" in _m:
                    referenced_ids.add(_m)
            for _role, _ms in (d.get("fallbacks") or {}).items():
                if isinstance(_ms, list):
                    for _m in _ms:
                        if isinstance(_m, str) and "/" in _m:
                            referenced_ids.add(_m)
                elif isinstance(_ms, str) and "/" in _ms:
                    referenced_ids.add(_ms)
            for _name, _m in (d.get("model_zoo") or {}).items():
                if isinstance(_m, dict) and _m.get("model"):
                    referenced_ids.add(str(_m["model"]))
            for _name, _m in (d.get("models") or {}).items():
                if isinstance(_m, dict) and _m.get("model"):
                    referenced_ids.add(str(_m["model"]))
            for _m in (d.get("fallback_providers") or []):
                if isinstance(_m, dict) and _m.get("model"):
                    referenced_ids.add(str(_m["model"]))
            m = d.get("model")
            if isinstance(m, dict) and m.get("default"):
                referenced_ids.add(str(m["default"]))
    except Exception as e:
        # If we cannot read the profiles, do NOT prune. Deleting health rows
        # on the strength of a failed read would be the exact data loss this
        # function exists to prevent.
        print(f"  prune skipped: could not read profile configs ({type(e).__name__}: {e})")
        return 0

    # Only prune within lanes whose catalogue pull actually succeeded this run.
    # A lane with a failed catalogue fetch is missing from `catalog` for a
    # transient reason, and pruning it would throw away good history.
    try:
        ok_lanes = {r[0] for r in c.execute("SELECT lane FROM catalog_lane WHERE ok=1")}
    except Exception:
        ok_lanes = set()

    orphans = []
    for lane, model in c.execute("SELECT lane, model FROM health"):
        if (lane, model) in current:
            continue
        # A reference matches this row if it is written either as the bare
        # native model id ("stealth/space-bunny-alpha", which is how OpenRouter
        # ids appear) or lane-prefixed ("nvidia/z-ai/glm-5.3-flash"). Exact
        # equality both ways - never a prefix match, or "z-ai/glm-5.3-flash"
        # would wrongly protect the retired "z-ai/glm-5.3".
        native = model
        lane_prefixed = f"{lane}/{model}"
        if any(r == native or r == lane_prefixed for r in referenced_ids):
            continue
        if ok_lanes and lane not in ok_lanes:
            continue          # lane catalogue was stale/failed - leave it alone
        orphans.append((lane, model))

    for lane, model in orphans:
        c.execute("DELETE FROM health WHERE lane=? AND model=?", (lane, model))
        print(f"  PRUNED stale health row: {lane}/{model}")

    return len(orphans)


def main():
    argv = __import__("sys").argv
    quick = "--quick" in argv
    catalogue_only = "--catalogue-only" in argv
    health_only = "--health-only" in argv
    keys = load_keys()
    c = conn()
    now = int(time.time())

    # CATALOGUE FIRST. The dropdowns the user actually looks at are fed by the
    # catalogue, and the health sweep's candidate list is read FROM the
    # catalogue - so pulling it first means the sweep probes today's models
    # and the dropdowns are correct even if the sweep later dies.
    # It used to run LAST, which meant a slow (6+ min) or crashed sweep left
    # the dropdowns showing yesterday's models with no visible cause.
    if not health_only:
        try:
            import catalog
            for k, v in catalog.refresh().items():
                print(f"catalogue {k:14} {v}")
        except Exception as e:
            print(f"catalogue refresh failed: {type(e).__name__}: {e}")
    if catalogue_only:
        return

    # Load candidates: live catalog → last-known catalog → hardcoded fallback
    try:
        candidates = load_candidates_from_catalog()
        if not candidates:
            print("Live catalog empty, trying last-known catalog...")
            candidates = load_last_known_candidates()
        if not candidates:
            print("Last-known catalog empty, falling back to hardcoded CANDIDATES")
            candidates = CANDIDATES
        else:
            # Filter to chat models only using catalog's heuristic
            import catalog
            candidates = [c for c in candidates if catalog.is_chat_model(c[1], c[0])]
            print(f"Loaded {len(candidates)} chat models from catalog")
    except Exception as e:
        print(f"Catalog load failed ({e}), trying last-known catalog...")
        candidates = load_last_known_candidates()
        if not candidates:
            print("Last-known catalog empty, falling back to hardcoded CANDIDATES")
            candidates = CANDIDATES

    rows = []
    for lane, model, is_free, note in candidates:
        if lane not in keys:
            rows.append((lane, model, is_free, note, None, None, None, "no key for lane", now))
            continue
        r = probe(lane, model, keys[lane], with_tools=not quick)
        rows.append((lane, model, is_free, note, r["http"], r["lat"],
                     None if r["tools"] is None else int(r["tools"]), r["err"], now))
        print(f"{lane:11} {model:52} http={r['http']} tools={r['tools']} {r['err'] or ''}"[:150])
        time.sleep(1.2)                      # stay well under 40 rpm
    # Accumulate N-of-M rather than overwriting: free-tier providers route each
    # request to a different backend, so one probe is noise. Observed 28 Sep:
    # stealth/space-bunny-alpha scored tools=1 then tools=0 on identical
    # consecutive runs, and nemotron-3-super flipped 200 -> 500.
    #
    # prev is None the FIRST time a model is seen (it has no health row yet).
    # Indexing None raises TypeError and killed the whole sweep mid-run, which
    # also stopped the catalogue pull at the end of main() - so a brand new
    # model could never enter the dropdowns. Guard every read.
    for lane, model, is_free, note, http, lat, tools, err, _ts in rows:
        prev = c.execute("SELECT runs,tools_yes,tools_no FROM health WHERE lane=? AND model=?",
                         (lane, model)).fetchone()
        runs = (prev[0] if prev else 0) + 1
        yes = (prev[1] if prev else 0) + (1 if tools == 1 else 0)
        no = (prev[2] if prev else 0) + (1 if tools == 0 else 0)
        c.execute(
            "INSERT OR REPLACE INTO health(lane,model,is_free,note,http,lat,tools,err,"
            "checked_at,runs,tools_yes,tools_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (lane, model, is_free, note, http, lat, tools, err, _ts, runs, yes, no))
    c.execute("INSERT INTO history(kind,payload,at) VALUES(?,?,?)",
              ("refresh", json.dumps({"n": len(rows), "quick": quick}), now))
    c.commit()

    # Mark models as DEAD in catalogue if they consistently 404 with no tool evidence.
    # Criteria: http == 404 AND runs >= 3 AND tools_yes == 0
    # This prevents dead models from cluttering the dropdowns.
    dead_marked = 0
    for lane, model, is_free, note, http, lat, tools, err, _ts in rows:
        if http == 404:
            prev = c.execute("SELECT runs, tools_yes FROM health WHERE lane=? AND model=?", (lane, model)).fetchone()
            if prev and prev[0] >= 3 and prev[1] == 0:
                c.execute("UPDATE catalog SET dead=1 WHERE lane=? AND model=?", (lane, model))
                dead_marked += 1
                print(f"  MARKED DEAD: {lane}/{model} (404 in {prev[0]} runs, no tool calls)")

    c.commit()

    # PRUNE orphaned health rows.
    #
    # The sweep only INSERT OR REPLACEs the models it probed, so rows for
    # models that have since left the catalogue used to linger FOREVER with
    # their last verdict still attached. z-ai/glm-5.3 is the example: NVIDIA
    # retired it (HTTP 410 Gone, measured 0.1s live on 30 Oct), but its row
    # still read http=None + TimeoutError from a 28 Oct sweep - a stale
    # failure being presented as a current one, which is exactly the class of
    # misleading entry this dashboard must not show.
    #
    # A row is pruned when BOTH are true:
    #   1. its (lane, model) is no longer in the catalogue, AND
    #   2. no profile config references that model as a main model or fallback.
    # Condition 2 keeps rows for models we still deliberately point at but
    # that have dropped out of the free catalogue - pruning those would blank
    # a profile's badge instead of showing its last real result.
    #
    # Only prune lanes whose catalogue pull actually succeeded this run. If a
    # lane's catalogue fetch failed, its models are missing from `catalog` for
    # a transient reason and pruning would destroy good health history.
    pruned = prune_orphan_health(c, candidates)
    c.commit()

    print(f"\nwrote {len(rows)} health rows to {DB}")
    if pruned:
        print(f"pruned {pruned} orphaned health rows (no longer in catalogue, not referenced by any profile)")
    if dead_marked:
        print(f"marked {dead_marked} models as dead in catalogue")
    # The catalogue pull now happens at the TOP of main() (before the probes),
    # so the dropdowns are correct even when the sweep is slow or dies partway.


def wire_fleet():
    """Complete any half-wired lane (stanza or key missing) that something outside
    the dashboard introduced - `hermes model`, a hand edit, a bot editing itself."""
    try:
        import wiring
        for prof, r in wiring.doctor(fix=True).items():
            for g in r.get("fixed", []):
                print(f"wiring   {prof:12} fixed {g['lane']:12} {g['detail']}")
            for g in r.get("left", []):
                print(f"wiring   {prof:12} LEFT  {g['lane']:12} {g['detail']}")
    except Exception as e:
        print(f"wiring check failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    wire_fleet()
    main()
