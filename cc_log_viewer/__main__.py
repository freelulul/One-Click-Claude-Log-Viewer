"""
CLI entry point: ``python3 -m cc_log_viewer``.

Boots the server, prints a tunnel-friendly banner, handles --port iteration
when the requested port is busy, refuses to bind 0.0.0.0 unless the user
explicitly opts in.
"""

from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
from pathlib import Path

from . import api as api_mod
from . import dates as dates_mod
from . import server as server_mod
from . import indexer as indexer_mod


DEFAULT_PORT = 8088
PORT_TRY_LIMIT = 20


def _hostname() -> str:
    """Best-effort fully-qualified hostname for the SSH tunnel hint."""
    try:
        fq = socket.getfqdn()
        if fq and "." in fq:
            return fq
    except OSError:
        pass
    return socket.gethostname() or "localhost"


def _try_bind(host: str, start: int, limit: int = PORT_TRY_LIMIT) -> int:
    """Find the first free port at or above `start`. Returns chosen port."""
    for off in range(limit):
        port = start + off
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                s.close()
                continue
            raise
        else:
            s.close()
            return port
    raise SystemExit(
        f"Could not find a free port in [{start}, {start + limit}); "
        "pass --port N for a different range."
    )


def _print_banner(host: str, port: int, tz_name: str, public: bool) -> None:
    # When stdout is piped to a file we still want the banner visible.
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None
    box = "─" * 53
    print()
    print("Claude Code Log Viewer")
    print(box)
    bind_label = host if host != "0.0.0.0" else "0.0.0.0  (DANGER: bound to all interfaces)"
    print(f"Server   : http://{bind_label}:{port}")
    fq = _hostname()
    print(f"Hostname : {fq}")
    print(f"Timezone : {tz_name}")
    print(f"Logs root: {Path.home() / '.claude' / 'projects'}")
    print()

    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "you"

    if not public:
        print("[SSH tunnel] On your laptop, run:")
        print(f"  ssh -L {port}:127.0.0.1:{port} {user}@{fq}")
        print(f"Then open http://127.0.0.1:{port}/ in your local browser.")
    else:
        print(f"[Direct access] http://{fq}:{port}/")
        print("⚠  --public is dangerous on shared HPC nodes; anyone reaching this")
        print("   port over the network can read every log on disk.")

    if any(k.startswith("SLURM_") for k in os.environ):
        sj = os.environ.get("SLURM_JOB_ID", "<unknown>")
        sn = os.environ.get("SLURMD_NODENAME") or os.environ.get("SLURM_NODELIST", "")
        print(f"[SLURM] job={sj} node={sn} — for tunneling from a compute node, you")
        print(f"        usually need a 2-hop forward via the login node.")
    print()
    print("Ctrl+C to stop.")
    print()


def selftest() -> int:
    """Quick smoke test: import everything, build a synthetic 1MB index."""
    import json
    import tempfile
    import time

    print("[selftest] importing modules…")
    from . import api, cache, dates, indexer, server  # noqa: F401
    print("[selftest] OK")

    print("[selftest] writing synthetic 1MB jsonl…")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "synth.jsonl"
        with path.open("w") as f:
            ts0 = "2026-04-27T09:00:00Z"
            for i in range(2000):
                f.write(json.dumps({
                    "type": "user" if i % 3 == 0 else "assistant",
                    "uuid": f"u-{i}",
                    "parentUuid": f"u-{i-1}" if i > 0 else None,
                    "timestamp": ts0,
                    "message": {"role": "user" if i % 3 == 0 else "assistant",
                                "content": [{"type": "text", "text": f"line {i}: " + "x"*200}]},
                }) + "\n")
        size = path.stat().st_size
        print(f"[selftest] synthetic file size: {size:,} bytes")
        tz = dates.resolve_tz("UTC")
        prog = indexer.Progress(size)
        t0 = time.time()
        idx = indexer.index_session("synth", "synth", path, tz, prog)
        t1 = time.time()
        print(f"[selftest] indexed {idx['num_lines']:,} lines in {(t1-t0)*1000:.1f}ms")
        assert idx["num_lines"] == 2000, idx["num_lines"]
        assert "2026-04-27" in idx["by_date"], idx["by_date"]
    print("[selftest] PASS")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="cc_log_viewer",
        description="Claude Code log viewer — read ~/.claude/projects/*.jsonl in a browser.",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"TCP port (default {DEFAULT_PORT}; auto-iterates if busy)")
    p.add_argument("--tz", default=None,
                   help="Display timezone (IANA like America/Chicago, UTC, fixed +08:00). "
                        "Default: system local.")
    p.add_argument("--public", action="store_true",
                   help="Bind 0.0.0.0 instead of 127.0.0.1. UNSAFE on shared hosts.")
    p.add_argument("--i-mean-it", dest="i_mean_it", action="store_true",
                   help="Required alongside --public to actually bind 0.0.0.0.")
    p.add_argument("--projects-root", default=None,
                   help="Override ~/.claude/projects directory.")
    p.add_argument("--no-banner", action="store_true",
                   help="Suppress the startup banner.")
    p.add_argument("--selftest", action="store_true",
                   help="Run a synthetic smoke test and exit.")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    # Public binding requires explicit confirmation.
    if args.public and not args.i_mean_it:
        sys.stderr.write(
            "\n⚠  --public binds the viewer to 0.0.0.0, which exposes every log\n"
            "   on disk to anyone reachable on this host. On HPC frontend nodes\n"
            "   that means *every other tenant*. Pass --i-mean-it together with\n"
            "   --public if you really want this. Exiting.\n\n"
        )
        return 2

    host = "0.0.0.0" if (args.public and args.i_mean_it) else "127.0.0.1"

    # Resolve TZ. Bail with a useful message if the user typo'd.
    try:
        tz = dates_mod.resolve_tz(args.tz)
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 2
    tz_name = dates_mod.tz_name(tz)

    # Choose port (auto-iterate if busy).
    chosen_port = _try_bind(host, args.port)

    projects_root = Path(args.projects_root).expanduser() if args.projects_root else None
    state = api_mod.AppState(tz=tz, projects_root=projects_root, port=chosen_port)

    if not args.no_banner:
        _print_banner(host, chosen_port, tz_name, public=args.public)

    server_mod.serve(state, host, chosen_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
