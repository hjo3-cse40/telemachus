"""CLI entrypoint for Telemachus Ingress Proxy."""
from __future__ import annotations

import argparse
import sys

from telemachus.ingress.proxy import run_proxy


def main():
    parser = argparse.ArgumentParser(description="Telemachus Ingress Tool Pruning Proxy")
    parser.add_argument("--port", type=int, default=1235, help="Port to listen on (default: 1235)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--upstream-port", type=int, default=1234, help="Upstream LM Studio port (default: 1234)")
    parser.add_argument("--upstream-host", type=str, default="127.0.0.1", help="Upstream LM Studio host (default: 127.0.0.1)")

    args = parser.parse_args()

    server = run_proxy(
        listen_host=args.host,
        listen_port=args.port,
        upstream_host=args.upstream_host,
        upstream_port=args.upstream_port,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Telemachus Ingress Proxy.")
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
