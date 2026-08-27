#!/usr/bin/env python3
"""
tunnel.py - open a Cloudflare tunnel to the local server and tell the Worker
where to find it.

Quick tunnels get a new hostname every restart, so the Worker cannot have the
backend URL baked in. This starts the tunnel, reads the hostname it was handed,
and POSTs it to the Worker's /_register endpoint. After that the public
workers.dev page proxies straight through to your machine.

    python tunnel.py --worker https://soundcloud-dl.kurtiswicker07.workers.dev \
                     --admin-token <ADMIN_TOKEN>

Run server.py first, in another window. Leave this running - closing it takes
the tunnel down and the page stops working.
"""

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Python block-buffers stdout when it is redirected to a file or pipe, which is
# exactly how this gets run in the background - the log stays empty until exit.
print = functools.partial(print, flush=True)                     # noqa: A001

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


CONFIG_FILE = Path(__file__).resolve().parent / "config.local.json"


def load_config():
    """Read config.local.json if it's there. Gitignored, so it's a safe home for
    tokens - keeps them off the command line and out of shell history."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"!! Ignoring {CONFIG_FILE.name}: {e}\n")
        return {}


def find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for pat in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                r"C:\Program Files\cloudflared\cloudflared.exe"):
        if Path(pat).exists():
            return pat
    local = os.environ.get("LOCALAPPDATA")
    if local:
        hits = sorted(Path(local).glob(
            "Microsoft/WinGet/Packages/Cloudflare.cloudflared*/**/cloudflared.exe"))
        if hits:
            return str(hits[-1])
    return None


def register(worker, token, backend, attempts=3):
    body = json.dumps({"backend": backend}).encode()
    req = urllib.request.Request(
        worker.rstrip("/") + "/_register", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Token": token,
            # Cloudflare's browser-integrity check answers urllib's default agent
            # with 403 / error 1010. Send something ordinary instead.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tunnel.py",
            "Accept": "application/json",
        })
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return True, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = (e.read() or b"").decode(errors="replace")[:200]
            # 4xx won't fix itself - a wrong token stays wrong.
            if 400 <= e.code < 500:
                return False, f"HTTP {e.code}: {detail}"
            last = f"HTTP {e.code}: {detail}"
        except Exception as e:                                   # noqa: BLE001
            last = str(e)
        if i < attempts - 1:
            time.sleep(2)
    return False, last


def local_up(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=5):
            return True
    except Exception:                                            # noqa: BLE001
        return False


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(
        description="Tunnel the local server and register it.",
        epilog="Values default to config.local.json when present; flags override it.")
    p.add_argument("--worker", default=cfg.get("worker"), help="your Worker's base URL")
    p.add_argument("--admin-token", default=cfg.get("adminToken"),
                   help="the Worker's ADMIN_TOKEN secret")
    p.add_argument("--port", type=int, default=int(cfg.get("port", 8800)),
                   help="local server port")
    p.add_argument("--wait", type=int, default=int(cfg.get("wait", 0)), metavar="SEC",
                   help="wait up to this long for the local server before giving up. "
                        "Auto-start needs this: nothing guarantees the server task "
                        "wins the race at boot (default: 0, fail immediately)")
    args = p.parse_args()

    missing = [n for n, v in (("--worker", args.worker),
                              ("--admin-token", args.admin_token)) if not v]
    if missing:
        p.error(f"missing {' and '.join(missing)} - pass them, or put "
                f"\"worker\" and \"adminToken\" in {CONFIG_FILE.name}")

    cf = find_cloudflared()
    if not cf:
        sys.exit("cloudflared not found.\n  winget install Cloudflare.cloudflared")

    if not local_up(args.port):
        if args.wait <= 0:
            sys.exit(f"Nothing answering on http://127.0.0.1:{args.port}\n"
                     f"  Start it first:  python server.py")
        print(f"  Waiting up to {args.wait}s for the server on port {args.port} ...")
        deadline = time.time() + args.wait
        while not local_up(args.port):
            if time.time() >= deadline:
                sys.exit(f"Server never came up on port {args.port} after "
                         f"{args.wait}s - giving up.")
            time.sleep(2)

    print(f"  Local  : http://127.0.0.1:{args.port}")
    print(f"  Worker : {args.worker}")
    print("  Opening tunnel ...\n")

    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://localhost:{args.port}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1, encoding="utf-8", errors="replace")

    registered = False
    try:
        for line in proc.stdout:
            if not registered:
                m = URL_RE.search(line)
                if m:
                    backend = m.group(0)
                    print(f"  Tunnel : {backend}")
                    ok, res = register(args.worker, args.admin_token, backend)
                    if ok:
                        registered = True
                        print(f"  Registered with the Worker.\n\n"
                              f"  Open {args.worker}\n"
                              f"  Ctrl+C to stop (the page stops working).\n")
                    else:
                        print(f"\n!! Could not register: {res}\n"
                              f"   The tunnel is up, so {backend} works directly,\n"
                              f"   but the Worker URL will not proxy to it.\n")
                        registered = True          # don't retry every log line
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopping tunnel ...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Stopped. The Worker page will report the machine as unreachable.")


if __name__ == "__main__":
    main()
