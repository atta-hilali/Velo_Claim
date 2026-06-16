import importlib.util
import json
from pathlib import Path
from typing import Any

import payer_registry


ROOT = Path(__file__).resolve().parent
CLAIM_PREP_PATH = ROOT / "Claim Preparation Agent.py"
CLAIM_VALIDATION_PATH = ROOT / "Claim Validation Agent.py"
PRIOR_AUTH_PATH = ROOT / "Prior Authorization Agent.py"
OUTPUT_PATH = ROOT / "sample_outputs" / "phase1_payer_registry_summary.json"


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def coverage_for(payer_name: str) -> dict[str, Any]:
    return {
        "resourceType": "Coverage",
        "id": f"COV-{payer_registry.compact_token(payer_name).upper()}",
        "status": "active",
        "payor": [{"identifier": {"value": payer_name}, "display": payer_name}],
    }


def main() -> None:
    registry = payer_registry.load_payer_registry()
    summary = payer_registry.phase1_payer_summary(registry)
    claim_prep = load_module("claim_preparation_agent", CLAIM_PREP_PATH)
    claim_validation = load_module("claim_validation_agent", CLAIM_VALIDATION_PATH)
    prior_auth = load_module("prior_authorization_agent", PRIOR_AUTH_PATH)

    phase1_expected = {
        "tawuniya_sa",
        "bupa_arabia_sa",
        "medgulf_sa",
        "axa_gulf_sa",
        "daman_ae",
        "oman_insurance_ae",
        "axa_insurance_uae",
        "sukoon_ae",
        "cigna_uae",
        "almadallah_tpa_ae",
        "nextcare_tpa_ae",
    }
    records = payer_registry.payer_records(registry)
    phase1_ids = {item["canonical_id"] for item in records if item.get("phase") == 1}
    require(phase1_expected <= phase1_ids, f"Missing phase 1 payers: {sorted(phase1_expected - phase1_ids)}")
    require(summary["phase1_by_country"] == {"AE": 7, "SA": 4}, f"Unexpected country split: {summary}")

    alias_pairs = [
        ("tawuniya_sa", "Tawuniya"),
        ("bupa_arabia_sa", "BUPA Arabia"),
        ("medgulf_sa", "Med Gulf"),
        ("axa_gulf_sa", "AXA Gulf"),
        ("daman_ae", "DAMAN"),
        ("oman_insurance_ae", "Oman Insurance"),
        ("axa_insurance_uae", "AXA Insurance UAE"),
        ("sukoon_ae", "Sukoon"),
        ("cigna_uae", "Cigna UAE"),
        ("almadallah_tpa_ae", "Almadallah"),
        ("nextcare_tpa_ae", "NextCare GCC"),
    ]
    for canonical_id, alias in alias_pairs:
        require(
            payer_registry.lookup_payer(alias)["canonical_id"] == canonical_id,
            f"Registry did not resolve alias {alias}",
        )
        require(
            claim_validation.payer_matches(canonical_id, alias),
            f"Validation agent did not match {canonical_id} to {alias}",
        )
        require(
            prior_auth.payer_matches(canonical_id, alias),
            f"Prior auth agent did not match {canonical_id} to {alias}",
        )

    routing_cases = [
        ("BUPA Arabia", "AUTO", "USD", "NPHIES"),
        ("MedGulf", "AUTO", "USD", "NPHIES"),
        ("DAMAN", "AUTO", "AED", "SHAFAFIYA"),
        ("Oman Insurance", "DUBAI", "AED", "ECLAIMLINK"),
        ("Almadallah", "AUTO", "AED", "SHAFAFIYA"),
        ("Nextcare", "ABU_DHABI", "AED", "SHAFAFIYA"),
        ("Nextcare", "DUBAI", "AED", "ECLAIMLINK"),
        ("AXA Insurance UAE", "AUTO", "AED", "ECLAIMLINK"),
    ]
    for payer_name, jurisdiction, currency, expected_format in routing_cases:
        actual_format = claim_prep.infer_claim_format(
            requested_format="AUTO",
            jurisdiction=jurisdiction,
            patient={},
            coverage=coverage_for(payer_name),
            provider={},
            facility={},
            currency=currency,
        )
        require(
            actual_format == expected_format,
            f"Expected {payer_name} to route to {expected_format}, got {actual_format}",
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "summary": summary,
                "phase1_ids": sorted(phase1_ids),
                "phase2_ids": sorted(item["canonical_id"] for item in records if item.get("phase") == 2),
                "routing_cases": [
                    {
                        "payer": payer_name,
                        "jurisdiction": jurisdiction,
                        "currency": currency,
                        "expected_format": expected_format,
                    }
                    for payer_name, jurisdiction, currency, expected_format in routing_cases
                ],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print("Phase 1 payer registry tests passed.")
    print("Output:", OUTPUT_PATH)
    print("Summary:", summary)


if __name__ == "__main__":
    main()
