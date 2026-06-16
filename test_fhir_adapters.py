import os
from contextlib import contextmanager
from typing import Any

from fhir_adapters import (
    FHIRAdapter,
    FHIRAdapterConfig,
    adapter_config_from_env,
    fhir_adapter_summary,
    normalize_adapter_name,
)


TRACKED_ENV_KEYS = [
    "FHIR_ADAPTER",
    "EHR_ADAPTER",
    "FHIR_BASE_URL",
    "FHIR_AUTH_TYPE",
    "FHIR_TOKEN_URL",
    "FHIR_CLIENT_ID",
    "FHIR_CLIENT_SECRET",
    "FHIR_CLIENT_AUTH_METHOD",
    "FHIR_SCOPE",
    "FHIR_AUDIENCE",
    "FHIR_ACCESS_TOKEN",
    "TRAKCARE_FHIR_BASE_URL",
    "TRAKCARE_FHIR_AUTH_TYPE",
    "TRAKCARE_FHIR_TOKEN_URL",
    "TRAKCARE_FHIR_CLIENT_ID",
    "ORACLE_HEALTH_FHIR_BASE_URL",
    "ORACLE_HEALTH_FHIR_AUTH_TYPE",
    "NABIDH_FHIR_BASE_URL",
    "NABIDH_FHIR_AUTH_TYPE",
]


@contextmanager
def patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in TRACKED_ENV_KEYS}
    for key in TRACKED_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for key in TRACKED_ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


class FakeFHIRAdapter(FHIRAdapter):
    def __init__(self, responses: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
        super().__init__(
            FHIRAdapterConfig(
                adapter="generic",
                label="Fake",
                base_url="https://fake-fhir.example/r4",
                auth_type="no_auth",
            )
        )
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
        self.calls.append((resource_type, params))
        key = (resource_type, "&".join(f"{name}={value}" for name, value in sorted(params.items())))
        return self.responses.get(key, [])


def test_adapter_aliases() -> None:
    assert normalize_adapter_name("TrakCare / IRIS") == "trakcare_iris"
    assert normalize_adapter_name("Oracle Health Millennium") == "oracle_health"
    assert normalize_adapter_name("NABIDH") == "nabidh"
    assert normalize_adapter_name("Epic") == "epic"


def test_trakcare_profile_env_overrides_generic_defaults() -> None:
    with patched_env(
        {
            "FHIR_ADAPTER": "trakcare",
            "FHIR_AUTH_TYPE": "STATIC_BEARER",
            "TRAKCARE_FHIR_BASE_URL": "https://trakcare.example/fhir/r4",
            "TRAKCARE_FHIR_AUTH_TYPE": "backend_services_jwt",
            "TRAKCARE_FHIR_TOKEN_URL": "https://trakcare.example/oauth2/token",
            "TRAKCARE_FHIR_CLIENT_ID": "velo-claim-trakcare",
        }
    ):
        config = adapter_config_from_env({"fhir_auth_type": "STATIC_BEARER"})

    assert config.adapter == "trakcare_iris"
    assert config.label == "InterSystems TrakCare / IRIS for Health"
    assert config.base_url == "https://trakcare.example/fhir/r4"
    assert config.auth_type == "backend_services_jwt"
    assert config.client_auth_method == "private_key_jwt"
    assert config.client_id == "velo-claim-trakcare"


def test_oracle_health_profile_can_be_state_configured() -> None:
    config = adapter_config_from_env(
        {
            "fhir_adapter": "oracle_health",
            "fhir_base_url": "https://oracle-health.example/r4",
            "fhir_auth_type": "no_auth",
        }
    )

    assert config.adapter == "oracle_health"
    assert config.label == "Oracle Health Millennium"
    assert config.base_url == "https://oracle-health.example/r4"
    assert config.auth_type == "no_auth"


def test_nabidh_profile_falls_back_to_generic_fhir_env() -> None:
    with patched_env(
        {
            "FHIR_ADAPTER": "nabidh",
            "FHIR_BASE_URL": "https://nabidh-proxy.example/fhir",
            "FHIR_AUTH_TYPE": "client_credentials",
        }
    ):
        summary = fhir_adapter_summary()

    assert summary["adapter"] == "nabidh"
    assert summary["label"] == "NABIDH Dubai HIE"
    assert summary["base_url_configured"] is True
    assert summary["auth_type"] == "client_credentials"


def test_patient_context_search_falls_back_from_subject_to_patient() -> None:
    adapter = FakeFHIRAdapter(
        {
            (
                "Condition",
                "encounter=Encounter/E1&patient=P1",
            ): [{"resourceType": "Condition", "id": "COND-1"}],
        }
    )

    results = adapter.search_patient_context(
        "Condition",
        patient_id="Patient/P1",
        encounter_id="Encounter/E1",
    )

    assert results == [{"resourceType": "Condition", "id": "COND-1"}]
    assert adapter.calls[0] == (
        "Condition",
        {"subject": "Patient/P1", "encounter": "Encounter/E1"},
    )
    assert adapter.calls[1] == (
        "Condition",
        {"patient": "P1", "encounter": "Encounter/E1"},
    )


def test_coverage_search_falls_back_to_patient_parameter() -> None:
    adapter = FakeFHIRAdapter(
        {
            (
                "Coverage",
                "patient=P1",
            ): [{"resourceType": "Coverage", "id": "COV-1"}],
        }
    )

    coverage = adapter.coverage_for_patient("Patient/P1")

    assert coverage == {"resourceType": "Coverage", "id": "COV-1"}
    assert ("Coverage", {"beneficiary": "Patient/P1"}) in adapter.calls
    assert ("Coverage", {"patient": "P1"}) in adapter.calls


if __name__ == "__main__":
    test_adapter_aliases()
    test_trakcare_profile_env_overrides_generic_defaults()
    test_oracle_health_profile_can_be_state_configured()
    test_nabidh_profile_falls_back_to_generic_fhir_env()
    test_patient_context_search_falls_back_from_subject_to_patient()
    test_coverage_search_falls_back_to_patient_parameter()
    print("FHIR adapter tests passed.")
