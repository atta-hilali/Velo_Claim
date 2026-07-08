from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_INPUT_PATH = "sample_inputs/dubai_clinic_full_pipeline.json"
DEFAULT_OUTPUT_PATH = "sample_outputs/manual_encounter_response.json"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input encounter file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Manual encounter input must be a JSON object.")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def post_json(url: str, body: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed with {exc.code}: {text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not reach {url}. Start the API first with: npm run api:memory"
        ) from exc


def get_text(url: str, *, timeout_seconds: int) -> str:
    request = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed with {exc.code}: {text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}.") from exc


def submit_manual_encounter(
    *,
    api_base_url: str,
    input_path: Path,
    output_path: Path,
    payload_output_path: Path | None,
    timeout_seconds: int,
    payload_timeout_seconds: int,
) -> dict[str, Any]:
    api_base_url = api_base_url.rstrip("/")
    encounter = read_json(input_path)
    result = post_json(f"{api_base_url}/encounters", encounter, timeout_seconds=timeout_seconds)
    write_json(output_path, result)

    claim_id = result.get("claim_id")
    if claim_id and payload_output_path:
        payload = get_text(
            f"{api_base_url}/claims/{claim_id}/payload",
            timeout_seconds=payload_timeout_seconds,
        )
        payload_output_path.parent.mkdir(parents=True, exist_ok=True)
        payload_output_path.write_text(payload, encoding="utf-8")

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit a manual encounter JSON file to the Velo Claim FastAPI backend."
    )
    parser.add_argument("--api", default=DEFAULT_API_BASE_URL, help="FastAPI base URL.")
    parser.add_argument("--input", default=DEFAULT_INPUT_PATH, help="Encounter JSON file.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Response JSON output file.")
    parser.add_argument(
        "--payload-output",
        default=None,
        help="Optional path where the generated claim payload should be saved.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for POST /encounters. Production storage over SSH can be slow.",
    )
    parser.add_argument(
        "--payload-timeout",
        type=int,
        default=120,
        help="Seconds to wait when downloading the generated payload.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_path = Path(args.output)
    payload_output_path = Path(args.payload_output) if args.payload_output else None
    try:
        result = submit_manual_encounter(
            api_base_url=args.api,
            input_path=Path(args.input),
            output_path=output_path,
            payload_output_path=payload_output_path,
            timeout_seconds=args.timeout,
            payload_timeout_seconds=args.payload_timeout,
        )
    except Exception as exc:
        print(f"Manual encounter submission failed: {exc}", file=sys.stderr)
        return 1

    claim = result.get("claim") or {}
    print("Manual encounter submitted.")
    print(f"claim_id: {result.get('claim_id')}")
    print(f"status: {claim.get('status')}")
    print(f"format: {claim.get('format')}")
    print(f"score: {claim.get('score')}")
    print(f"response: {output_path}")
    if payload_output_path:
        print(f"payload: {payload_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
