"""Minimal HTTP helper built on urllib, so the project stays dependency-free."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(RuntimeError):
    """Non-2xx response, carrying the body so API errors stay readable."""

    def __init__(self, status: int, url: str, body: str) -> None:
        self.status = status
        self.url = url
        self.body = body
        snippet = body if len(body) <= 600 else body[:600] + "..."
        super().__init__(f"HTTP {status} for {url}\n{snippet}")


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    timeout: float = 30.0,
    retries: int = 3,
) -> tuple[int, str]:
    """Perform a request, retrying on transient network/5xx failures."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    last_exc: Exception | None = None

    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # 4xx is a definitive answer; only retry server-side hiccups.
            if exc.code < 500 or attempt == retries - 1:
                raise HttpError(exc.code, url, body) from exc
            last_exc = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise
            last_exc = exc
        time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"unreachable retry state for {url}") from last_exc


def request_json(url: str, **kwargs) -> dict:
    """Request and decode JSON, reporting the raw body when decoding fails."""
    _, body = request(url, **kwargs)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        snippet = body[:400]
        raise RuntimeError(
            f"expected JSON from {url} but got:\n{snippet}"
        ) from exc
