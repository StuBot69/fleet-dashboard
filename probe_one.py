#!/usr/bin/env python3
"""Re-probe one model on one lane and update its health row.

A single probe is NOISE on free tiers (the same model flips 200 -> 500 between
identical calls), so this takes N samples and records the N-of-M tally the
dashboard expects. Used to repair a health row that a transient 500 poisoned.

Usage: probe_one.py <lane> <model> [n]
"""
import json, os, sys, time, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # reuse the same key loading + probe semantics

DB = refresh.DB
UA = refresh.UA


def main():
    lane, model = sys.argv[1], sys.argv[2]
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    keys = refresh.load_keys()
    if lane not in keys:
        print(f"no key for lane {lane}")
        return 1

    c = refresh.conn()
    now = int(time.time())
    prev = c.execute("SELECT runs,tools_yes,tools_no FROM health WHERE lane=? AND model=?",
                     (lane, model)).fetchone()
    runs = (prev[0] if prev else 0)
    yes = (prev[1] if prev else 0)
    no = (prev[2] if prev else 0)
    last = None
    for i in range(n):
        r = refresh.probe(lane, model, keys[lane], with_tools=True)
        last = r
        print(f"  sample {i+1}/{n}: http={r['http']} tools={r['tools']} {r['err'] or ''}"[:160])
        if r["http"] == 200:
            runs += 1
            if r["tools"] == 1:
                yes += 1
            elif r["tools"] == 0:
                no += 1
        time.sleep(2.0)

    # Keep the health row even if every sample failed - a dead model must show
    # as dead, not vanish.
    note = "FREE - re-probed"
    is_free = 1
    c.execute("INSERT OR REPLACE INTO health(lane,model,is_free,note,http,lat,tools,err,"
              "checked_at,runs,tools_yes,tools_no) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              (lane, model, is_free, note, last["http"], last["lat"], last["tools"],
               last["err"], now, runs, yes, no))
    c.commit()
    c.close()
    print(f"  updated health row {lane}/{model}: http={last['http']} tools={last['tools']} "
          f"runs={runs} yes={yes} no={no}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
