"""python -m kcd.web [--port 8000], from platform/, then open http://127.0.0.1:8000."""

from __future__ import annotations

import argparse

import uvicorn

from kcd.web.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="kcd.web")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    print(f"Kidney Care Desk: http://127.0.0.1:{a.port}  (synthetic patients, no login, this computer only)")
    uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning", timeout_graceful_shutdown=1)


if __name__ == "__main__":
    main()
