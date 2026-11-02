"""``rec-serve``: run the HTTP service with uvicorn."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the server.

    Scale with processes (``--workers``) and pods, not threads: the engine holds
    the GIL, and more concurrent engine runs lower throughput.
    """
    parser = argparse.ArgumentParser(prog="rec-serve", description=main.__doc__)
    parser.add_argument("--app", action="append", help="app slug (repeatable)")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container default
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    if args.app:
        os.environ["RECSYS_APPS"] = ",".join(args.app)

    import uvicorn  # noqa: PLC0415 - heavy import, only for the server

    uvicorn.run(
        "recsys.serving.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
    )
    return 0
