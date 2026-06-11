"""
Small FHIR REST smoke test without authentication.

Default sandbox:
    https://hapi.fhir.org/baseR4

What this script shows:
    - GET /Patient?_count=N returns a Bundle
    - GET /Encounter?_count=N returns a Bundle
    - GET /Coverage?_count=N returns a Bundle
    - GET /ResourceType/{id} returns one resource, not a Bundle

No Authorization header is sent.
"""

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_FHIR_BASE_URL = "https://hapi.fhir.org/baseR4"
DEFAULT_RESOURCES = ("Patient", "Encounter", "Coverage")


def get_json(url: str, timeout_seconds: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/fhir+json, application/json",
            "User-Agent": "velo-claim-fhir-no-auth-test/1.0",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body}") from exc


def fhir_url(base_url: str, path: str, params: dict[str, str] | None = None) -> str:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    return url


def summarize_bundle(resource_type: str, bundle: dict[str, Any]) -> str | None:
    print(f"\n--- Search {resource_type} ---")
    print(f"resourceType: {bundle.get('resourceType')}")
    print(f"bundle.type:  {bundle.get('type')}")
    print(f"bundle.total: {bundle.get('total')}")

    entries = bundle.get("entry", [])
    print(f"entry count returned: {len(entries)}")

    if bundle.get("resourceType") != "Bundle":
        print("This response is not a Bundle. The server may not support search here.")
        return None

    if not entries:
        print("No entries returned.")
        return None

    print("Entries inside a Bundle look like:")
    for index, entry in enumerate(entries, start=1):
        resource = entry.get("resource", {})
        print(
            f"  {index}. fullUrl={entry.get('fullUrl')} "
            f"resourceType={resource.get('resourceType')} id={resource.get('id')}"
        )

    first_resource = entries[0].get("resource", {})
    return first_resource.get("id")


def read_one_resource(base_url: str, resource_type: str, resource_id: str) -> None:
    url = fhir_url(base_url, f"{resource_type}/{resource_id}")
    resource = get_json(url)

    print(f"\n--- Direct read {resource_type}/{resource_id} ---")
    print(f"resourceType: {resource.get('resourceType')}")
    print(f"id:           {resource.get('id')}")
    print("This is a single resource response, not a Bundle.")


def search_resource(base_url: str, resource_type: str, count: int) -> str | None:
    url = fhir_url(base_url, resource_type, {"_count": str(count)})
    print(f"\nGET {url}")
    bundle = get_json(url)
    return summarize_bundle(resource_type, bundle)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test public FHIR REST reads without authentication."
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_FHIR_BASE_URL,
        help=f"FHIR R4 base URL. Default: {DEFAULT_FHIR_BASE_URL}",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of search results to request per resource.",
    )
    args = parser.parse_args()

    print("FHIR REST no-auth test")
    print(f"Base URL: {args.base_url}")
    print("No Authorization header is sent.")

    for resource_type in DEFAULT_RESOURCES:
        first_id = search_resource(args.base_url, resource_type, args.count)
        if first_id:
            read_one_resource(args.base_url, resource_type, first_id)

    print("\nDone.")


if __name__ == "__main__":
    main()
