"""HTTP client for public FPL JSON endpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen


class FplApiError(RuntimeError):
    """Raised when an FPL response cannot be fetched or decoded."""


@dataclass(frozen=True)
class ApiPayload:
    url: str
    body: bytes
    data: Any


class FplApiClient:
    def __init__(
        self,
        *,
        base_url: str = "https://fantasy.premierleague.com/api",
        timeout_seconds: float = 20.0,
        user_agent: str = "fpl-decision-engine/0.3",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def bootstrap_static(self) -> ApiPayload:
        return self.get("bootstrap-static/")

    def fixtures(self) -> ApiPayload:
        return self.get("fixtures/")

    def get(self, path: str) -> ApiPayload:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
            data = json.loads(body)
        except Exception as error:
            raise FplApiError(f"Could not fetch valid JSON from {url}: {error}") from error
        return ApiPayload(url=url, body=body, data=data)
