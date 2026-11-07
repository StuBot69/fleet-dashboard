#!/usr/bin/env python3
"""Fleet control CLI — one entry point for everything fleet-related."""

import sys
import subprocess
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(cmd_args, **kw):
    return subprocess.run(cmd_args, cwd=HERE, **kw)


def cmd_refresh(args):
    """Run health probes only."""
    quick = "--quick" in args
    cmd = [PY, "refresh.py"]
    if quick:
        cmd.append("--quick")
    run(cmd)


def cmd_catalog(args):
    """Refresh model catalog from all lanes."""
    run([PY, "catalog.py"])


def cmd_full(args):
    """Run catalog refresh then health probes (what daily cron does)."""
    # Run sequentially to avoid quota contention
    run([PY, "catalog.py"])
    run([PY, "refresh.py"])


def cmd_dashboard(args):
    """Start Fleet Dashboard UI on http://127.0.0.1:8787."""
    port = "8787"
    if args and args[0].startswith("--port="):
        port = args[0].split("=", 1)[1]
    # Validate port
    try:
        port_int = int(port)
        if not (1 <= port_int <= 65535):
            raise ValueError
    except ValueError:
        print(f"Invalid port: {port}")
        return
    print(f"Starting Fleet Dashboard on http://127.0.0.1:{port}")
    run([PY, "app.py", port])


def cmd_health(args):
    """Pretty-print health table from fleet.db."""
    import sqlite3
    DB = os.path.join(HERE, "fleet.db")
    if not os.path.exists(DB):
        print("fleet.db not found — run 'fleetctl refresh' first")
        return
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("""
            SELECT lane, model, http, lat, tools, runs, tools_yes, tools_no, err, checked_at
            FROM health ORDER BY lane, model
        """).fetchall()
        if not rows:
            print("No health data — run 'fleetctl refresh' first")
            return
        print(f"{'LANE':12} {'MODEL':50} {'HTTP':>4} {'LAT':>5} {'TOOLS':>5} {'RUNS':>4} {'YES':>3} {'NO':>3}  ERROR")
        print("-" * 120)
        import time
        for r in rows:
            err = (r["err"] or "")[:60]
            print(f"{r['lane']:12} {r['model']:50} {r['http'] or '-':>4} {r['lat'] or '-':>5} "
                  f"{r['tools'] if r['tools'] is not None else '-':>5} {r['runs']:>4} {r['tools_yes']:>3} {r['tools_no']:>3}  {err}")
    finally:
        c.close()


def cmd_catalog_list(args):
    """List all models in catalog (free + live only)."""
    import sqlite3
    DB = os.path.join(HERE, "fleet.db")
    if not os.path.exists(DB):
        print("fleet.db not found — run 'fleetctl catalog' first")
        return
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("""
            SELECT lane, model, is_free, note, live FROM catalog
            WHERE is_free=1 AND live=1 ORDER BY lane, model
        """).fetchall()
        if not rows:
            print("No catalog data — run 'fleetctl catalog' first")
            return
        print(f"{'LANE':14} {'MODEL':60} {'NOTE'}")
        print("-" * 120)
        for r in rows:
            print(f"{r['lane']:14} {r['model']:60} {r['note']}")
    finally:
        c.close()


def cmd_lanes(args):
    """Show lane budget info."""
    from app import LANE_BUDGET
    print(f"{'LANE':16} {'HEADROOM':55} {'BOTTLENECK':12} {'QUALITY'}")
    print("-" * 100)
    for lane, info in LANE_BUDGET.items():
        print(f"{lane:16} {info['free_headroom']:55} {info['bottleneck']:12} {info['quality']}")


def cmd_probe(args):
    """Quick probe a single model: fleetctl probe <lane> <model_id>"""
    if len(args) < 2:
        print("Usage: fleetctl probe <lane> <model_id> [--quick]")
        return
    lane, model = args[0], args[1]
    quick = "--quick" in args
    import json, time, urllib.request
    from refresh import load_keys, probe, LANES
    keys = load_keys()
    if lane not in keys:
        print(f"No key for lane '{lane}' in ~/.hermes/.env")
        return
    if lane not in LANES:
        print(f"Unknown lane '{lane}' — no chat endpoint configured")
        return
    r = probe(lane, model, keys[lane], with_tools=not quick)
    print(f"HTTP: {r['http']}  Latency: {r['lat']}s  Tools: {r['tools']}  Error: {r['err'] or 'none'}")


COMMANDS = {
    "refresh":    (cmd_refresh,    "Run health probes on all candidate models"),
    "catalog":    (cmd_catalog,    "Fetch model lists from all lanes into catalog"),
    "full":       (cmd_full,       "catalog + refresh (what daily cron runs)"),
    "dashboard":  (cmd_dashboard,  "Start Fleet Dashboard UI on :8787"),
    "health":     (cmd_health,     "Pretty-print health table from fleet.db"),
    "list":       (cmd_catalog_list, "List all free+live models in catalog"),
    "lanes":      (cmd_lanes,      "Show lane budget / headroom info"),
    "probe":      (cmd_probe,      "Quick probe one model: fleetctl probe <lane> <model>"),
}


def main():
    if len(sys.argv) < 2:
        print("Fleet Control CLI")
        print("Usage: fleetctl <command> [args...]")
        print()
        print("Commands:")
        for name, (fn, desc) in COMMANDS.items():
            print(f"  {name:12} {desc}")
        return

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print("Run 'fleetctl' for help")
        sys.exit(1)

    fn, _ = COMMANDS[cmd]
    fn(sys.argv[2:])


if __name__ == "__main__":
    main()