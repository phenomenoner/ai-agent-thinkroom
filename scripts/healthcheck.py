#!/usr/bin/env python3
"""Fail-closed container readiness probe."""

from __future__ import annotations

from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

DEFAULT_URL = "http://127.0.0.1:8787/health/ready"


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    with opener.open(DEFAULT_URL, timeout=2) as response:
        if response.status != 200:
            raise RuntimeError("Thinkroom readiness check failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
