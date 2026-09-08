from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


class HttpPayerRuleFetcher:
    """Fetch payer rule sets from an authenticated internal rule service."""

    def __init__(self, base_url: str, token: str | None = None, timeout_seconds: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "HttpPayerRuleFetcher | None":
        url = os.getenv("PAYER_RULES_URL", "").strip()
        if not url:
            return None
        return cls(
            url,
            token=os.getenv("PAYER_RULES_ACCESS_TOKEN") or None,
            timeout_seconds=int(os.getenv("PAYER_RULES_TIMEOUT_SECONDS", "20")),
        )

    def __call__(self, payer_id: str, plan_id: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"payer_id": payer_id, "plan_id": plan_id})
        request = urllib.request.Request(
            f"{self.base_url}?{query}",
            headers={
                "Accept": "application/json",
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Payer rule service returned a non-object response.")
        return payload.get("rule_set", payload)
