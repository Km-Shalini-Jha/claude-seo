#!/usr/bin/env python3
"""Standalone worker for Claude SEO SaaS audit jobs."""

from __future__ import annotations

import argparse
import time

from web.backend import WORKER_EVENT, worker_loop


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Claude SEO web job worker")
    parser.add_argument("--poll", type=float, default=1.0, help="Idle polling interval in seconds")
    args = parser.parse_args()
    if args.poll > 0:
        # Wake the imported worker loop at least once. It still blocks on its
        # own event internally, but this keeps the CLI shape explicit.
        WORKER_EVENT.set()
    worker_loop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        time.sleep(0.1)
        raise SystemExit(0)
