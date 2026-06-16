import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
AGENT_PATH = ROOT / "Prior Authorization Agent.py"
REGISTER_PATH = ROOT / "data" / "prior_auth_extraction" / "velo_claim_prior_auth_extraction_register.json"
OUTPUT_PATH = ROOT / "sample_outputs" / "prior_auth_extraction_register_summary.json"


def load_agent_module() -> Any:
    spec = importlib.util.spec_from_file_location("prior_authorization_agent", AGENT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load agent module from {AGENT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    agent = load_agent_module()
    register = agent.load_prior_auth_extraction_register(REGISTER_PATH)
    summary = agent.prior_auth_extraction_summary(register)

    require(register.get("artifact") == "velo_claim_prior_auth_extraction_register", "Unexpected artifact name")
    require(summary["compact_prior_auth_rule_count"] == 9, "Expected 9 compact prior-auth rules")
    require(summary["shafafiya_service_count"] >= 5, "Expected Shafafiya PA service methods")
    require(summary["missing_data_count"] >= 9, "Expected missing-data register entries")
    require(summary["implementation_step_count"] >= 12, "Expected implementation-step register entries")
    require(
        "actual patient-approved authorization" in (summary["production_gap"] or "").lower(),
        "Production gap should mention actual approved authorization records",
    )

    service_names = {
        item.get("service_method", "")
        for item in register["data"]["shafafiya_pa_services"]["records"]
    }
    for expected in [
        "CheckForNewPriorAuthorizationTransactions",
        "GetNewPriorAuthorizationTransactions",
        "SearchTransactions",
        "DownloadTransactionFile",
        "SetTransactionDownloaded",
    ]:
        require(
            any(expected in name for name in service_names),
            f"Missing Shafafiya service method: {expected}",
        )

    compact_rule_ids = {rule.get("rule_id") for rule in register.get("prior_auth_rules_compact", [])}
    for expected in [
        "DAMAN-PA-CT-ABD-PEL-001",
        "DAMAN-PA-WHEELCHAIR-001",
        "DAMAN-PA-LYMPH-LIPEDEMA-001",
    ]:
        require(expected in compact_rule_ids, f"Missing compact PA rule: {expected}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
                "summary": summary,
                "compact_rule_ids": sorted(compact_rule_ids),
                "shafafiya_service_methods": sorted(service_names),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print("Prior auth extraction register test passed.")
    print("Output:", OUTPUT_PATH)
    print("Summary:", summary)


if __name__ == "__main__":
    main()
