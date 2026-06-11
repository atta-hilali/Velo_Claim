import importlib.util
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


AGENT_PATH = Path(__file__).resolve().parent / "Claim Preparation Agent.py"


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("claim_preparation_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {AGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/fhir+json, application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc


def decode_jwt_part(part: str) -> dict[str, Any]:
    padded = part + "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def token_summary(token: str) -> dict[str, Any]:
    header, payload, *_ = token.split(".")
    decoded_header = decode_jwt_part(header)
    decoded_payload = decode_jwt_part(payload)
    return {
        "header_alg": decoded_header.get("alg"),
        "client_id": decoded_payload.get("client_id"),
        "scope": decoded_payload.get("scope") or decoded_payload.get("scp"),
        "token_type": decoded_payload.get("epic.tokentype"),
        "payload_keys": sorted(decoded_payload.keys()),
    }


def try_get_json(url: str, token: str | None = None) -> tuple[int, dict[str, Any] | str]:
    headers = {"Accept": "application/fhir+json, application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def print_bundle_summary(label: str, data: dict[str, Any]) -> None:
    print(f"\n=== {label} ===")
    print("resourceType:", data.get("resourceType"))
    if data.get("resourceType") == "Bundle":
        print("type:", data.get("type"))
        print("total:", data.get("total"))
        entries = data.get("entry", [])
        print("entries:", len(entries))
        if entries:
            resource = entries[0].get("resource", {})
            print("first_resource:", resource.get("resourceType"))
            print("first_id:", resource.get("id"))
    else:
        print("id:", data.get("id"))


def main() -> None:
    agent = load_agent_module()
    token = agent.fhir_backend_access_token({})
    if not token:
        raise RuntimeError("No token returned. Check FHIR_AUTH_TYPE and .env.")

    print("Token received.")
    print("Token length:", len(token))
    print("Token preview:", token[:12] + "..." + token[-8:])
    print("Token summary:", json.dumps(token_summary(token), indent=2))

    base_url = (os.getenv("FHIR_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("FHIR_BASE_URL is empty.")

    metadata = get_json(f"{base_url}/metadata", token)
    print_bundle_summary("CapabilityStatement", metadata)

    failures = []
    for resource_type in ["Patient", "Encounter", "Coverage", "Practitioner", "Organization"]:
        url = f"{base_url}/{resource_type}?{urllib.parse.urlencode({'_count': '1'})}"
        status, data = try_get_json(url, token)
        if status == 200 and isinstance(data, dict):
            print_bundle_summary(f"{resource_type} search", data)
            continue

        failures.append((resource_type, status, str(data)[:500]))
        print(f"\n=== {resource_type} search ===")
        print("status:", status)
        print("error:", str(data)[:500])

    if failures:
        print("\n=== Diagnosis ===")
        print(
            "Token acquisition works, but one or more FHIR resource reads failed. "
            "For Epic this usually means the app registration is missing the matching "
            "Incoming APIs / backend permissions, or the search call is missing an "
            "Epic-required parameter such as patient or beneficiary."
        )


if __name__ == "__main__":
    main()
