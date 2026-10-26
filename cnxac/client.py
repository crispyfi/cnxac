"""Thin client for the new Central configuration API.

One request at a time, because a config change fails while another is in progress.
Rate limits, "undergoing processing" responses and connection errors are retried.
Credentials and tokens are never printed or logged.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Credentials

API = "network-config/v1alpha1"
RETRIES = 5


class CentralError(Exception):
    """A request that Central refused, or that never got a response."""

    def __init__(self, method: str, path: str, status: int | None, message: str):
        self.method, self.path, self.status, self.message = method, path, status, message
        where = f"HTTP {status}" if status is not None else "no response"
        super().__init__(f"{method} {path}: {where}: {message}")


def message_of(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error_description") or data.get("error") or data)[:500]
    return str(data)[:500]


def items_of(data: Any, list_key: str | None = None) -> list[dict]:
    """The object list in a GET body, {<list key>: [...]}. A type with no objects returns {}."""
    if not data:
        return []
    if list_key and list_key in data:
        return data[list_key]
    lists = [v for v in data.values() if isinstance(v, list)]
    if len(lists) == 1:
        return lists[0]
    raise ValueError(f"no single object list in a body with keys {sorted(data)}")


class Client:
    def __init__(self, creds: Credentials, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, retries: int = RETRIES):
        self._creds = creds
        self._sleep = sleep
        self._retries = retries
        self._http = httpx.Client(base_url=f"{creds.base_url.rstrip('/')}/{API}/", timeout=60,
                                  transport=transport)
        self._token: str | None = None
        self._expires = 0.0
        # Called with (method, path, params, status, body) for every response, e.g. by apply's request log
        self.on_response: Callable[[str, str, dict | None, int, Any], None] | None = None

    @property
    def host(self) -> str:
        """The Central host, for output."""
        return urlsplit(self._creds.base_url).hostname or self._creds.base_url

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() > self._expires - 60:
            self._fetch_token()
        return {"Authorization": f"Bearer {self._token}"}

    def _fetch_token(self) -> None:
        try:
            r = self._http.post(self._creds.token_url, data={"grant_type": "client_credentials"},
                                auth=(self._creds.client_id, self._creds.client_secret), timeout=30)
        except httpx.TransportError as e:
            raise CentralError("POST", "token", None, type(e).__name__) from None
        if r.status_code != 200:
            raise CentralError("POST", "token", r.status_code,
                               "token request refused; check base_url, client_id and client_secret")
        body = r.json()
        self._token = body["access_token"]
        self._expires = time.monotonic() + float(body.get("expires_in", 3600))

    @staticmethod
    def _parse(r: httpx.Response) -> Any:
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def _wait(self, attempt: int, r: httpx.Response | None = None) -> None:
        try:
            delay = float(r.headers["Retry-After"]) if r is not None else None
        except (KeyError, ValueError):
            delay = None
        self._sleep(delay if delay is not None else min(3 * attempt, 30))

    def request(self, method: str, path: str, params: dict | None = None, body: Any = None,
                files: dict | None = None) -> tuple[int, Any]:
        """Send one request and return (status, parsed body), retrying while Central is busy.
        files sends a multipart upload instead of a JSON body."""
        for key in params or {}:
            if key != key.lower() or "_" in key:
                raise ValueError(f"query parameter {key!r} must be kebab-case")
        attempt, refreshed = 0, False
        while True:
            headers = {**self._auth_header(), "Accept": "*/*" if method == "DELETE" else "application/json"}
            try:
                r = self._http.request(method, path, params=params, json=body, files=files, headers=headers)
            except httpx.TransportError as e:
                if attempt >= self._retries:
                    raise CentralError(method, path, None, type(e).__name__) from None
                attempt += 1
                self._wait(attempt)
                continue
            data = self._parse(r)
            if self.on_response:
                self.on_response(method, path, params, r.status_code, data)
            if r.status_code == 401 and not refreshed:
                refreshed, self._token = True, None
                continue
            busy = r.status_code == 429 or (r.status_code == 500 and "undergoing processing" in message_of(data))
            if busy and attempt < self._retries:
                attempt += 1
                self._wait(attempt, r)
                continue
            return r.status_code, data

    def call(self, method: str, path: str, params: dict | None = None, body: Any = None,
             files: dict | None = None) -> Any:
        """Like request, but raises CentralError unless the status is 2xx."""
        status, data = self.request(method, path, params, body, files)
        if not 200 <= status < 300:
            raise CentralError(method, path, status, message_of(data))
        return data

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.call("GET", path, params)

    def post(self, path: str, body: Any = None, params: dict | None = None) -> Any:
        return self.call("POST", path, params, body)

    def put(self, path: str, body: Any = None, params: dict | None = None) -> Any:
        return self.call("PUT", path, params, body)

    def patch(self, path: str, body: Any = None, params: dict | None = None) -> Any:
        return self.call("PATCH", path, params, body)

    def delete(self, path: str, params: dict | None = None) -> Any:
        return self.call("DELETE", path, params)

    def upload(self, path: str, name: str, content: bytes, params: dict | None = None) -> Any:
        """POST a file as the multipart field "file", as Central's CSV imports take it."""
        return self.call("POST", path, params, files={"file": (name, content, "text/csv")})

    def get_items(self, path: str, list_key: str | None = None) -> list[dict]:
        """Every object of a profile type. Profile lists aren't paginated."""
        return items_of(self.get(path), list_key)

    def get_paged(self, path: str, limit: int = 100) -> list[dict]:
        """Every item of a scope list (sites), which takes limit and offset."""
        items: list[dict] = []
        while True:
            data = self.get(path, {"limit": limit, "offset": len(items)}) or {}
            page = data.get("items") or []
            items += page
            if len(page) < limit or len(items) >= data.get("total", float("inf")):
                return items
