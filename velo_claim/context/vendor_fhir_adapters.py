"""FHIR R4 adapters for Velo Claim EHR and HIE integrations.

The adapters expose one small interface used by the agents:
read(resource_type, id), search(resource_type, params), and search_first(...).

Vendor profiles keep configuration separate from claim logic. TrakCare/IRIS,
Oracle Health, Epic, and NABIDH all use the same FHIR/SMART mechanics with
different endpoints, client registrations, and occasional search-parameter
quirks.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from base64 import b64encode, urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velo_claim.core.env import load_env_file, project_root


load_env_file()


DEFAULT_FHIR_AUTH_TYPE = "STATIC_BEARER"
DEFAULT_FHIR_CLIENT_AUTH_METHOD = "client_secret_basic"
DEFAULT_FHIR_SCOPE = (
    "system/Patient.read system/Patient.search "
    "system/Coverage.read system/Coverage.search "
    "system/Encounter.read system/Encounter.search "
    "system/Practitioner.read system/Practitioner.search "
    "system/Organization.read system/Organization.search "
    "system/Condition.read system/Condition.search "
    "system/Procedure.read system/Procedure.search "
    "system/DocumentReference.read system/DocumentReference.search "
    "system/ChargeItem.read system/ChargeItem.search"
)


ADAPTER_ALIASES = {
    "": "generic",
    "DEFAULT": "generic",
    "AUTO": "auto",
    "GENERIC": "generic",
    "FHIR": "generic",
    "EPIC": "epic",
    "EPICFHIR": "epic",
    "TRAKCARE": "trakcare_iris",
    "TRAKCAREIRIS": "trakcare_iris",
    "INTERSYSTEMS": "trakcare_iris",
    "INTERSYSTEMSIRIS": "trakcare_iris",
    "IRIS": "trakcare_iris",
    "IRISFORHEALTH": "trakcare_iris",
    "CERNER": "oracle_health",
    "ORACLE": "oracle_health",
    "ORACLEHEALTH": "oracle_health",
    "ORACLEHEALTHMILLENNIUM": "oracle_health",
    "MILLENNIUM": "oracle_health",
    "NABIDH": "nabidh",
    "DHAHIE": "nabidh",
}


ADAPTER_PROFILES: dict[str, dict[str, str]] = {
    "generic": {
        "env_prefix": "FHIR",
        "label": "Generic FHIR R4",
        "default_auth_type": DEFAULT_FHIR_AUTH_TYPE,
    },
    "epic": {
        "env_prefix": "EPIC_FHIR",
        "label": "Epic on FHIR",
        "default_auth_type": DEFAULT_FHIR_AUTH_TYPE,
    },
    "trakcare_iris": {
        "env_prefix": "TRAKCARE_FHIR",
        "label": "InterSystems TrakCare / IRIS for Health",
        "default_auth_type": DEFAULT_FHIR_AUTH_TYPE,
    },
    "oracle_health": {
        "env_prefix": "ORACLE_HEALTH_FHIR",
        "label": "Oracle Health Millennium",
        "default_auth_type": DEFAULT_FHIR_AUTH_TYPE,
    },
    "nabidh": {
        "env_prefix": "NABIDH_FHIR",
        "label": "NABIDH Dubai HIE",
        "default_auth_type": DEFAULT_FHIR_AUTH_TYPE,
    },
}


class FHIRAuthError(RuntimeError):
    """FHIR auth failed before a resource request could be made."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.status_code = 401


class FHIRRequestError(RuntimeError):
    """FHIR resource request failed with an HTTP status code."""

    def __init__(self, message: str, *, status_code: int, url: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


@dataclass(frozen=True)
class FHIRAdapterConfig:
    adapter: str
    label: str
    base_url: str = ""
    auth_type: str = DEFAULT_FHIR_AUTH_TYPE
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    client_auth_method: str = DEFAULT_FHIR_CLIENT_AUTH_METHOD
    scope: str = DEFAULT_FHIR_SCOPE
    audience: str = ""
    private_key_path: str = ""
    key_id: str = ""
    jwks_url: str = ""
    access_token: str = ""
    timeout_seconds: int = 30


def normalize_adapter_name(value: Any) -> str:
    token = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return ADAPTER_ALIASES.get(token, str(value or "generic").strip().lower())


def _state_value(state: dict[str, Any] | None, *keys: str) -> str:
    if not state:
        return ""
    for key in keys:
        value = state.get(key)
        if value not in {None, ""}:
            return str(value)
    return ""


def _env_value(prefix: str, suffix: str, *, include_generic_fallback: bool = True) -> str:
    value = os.getenv(f"{prefix}_{suffix}")
    if value not in {None, ""}:
        return str(value)
    if include_generic_fallback and prefix != "FHIR":
        fallback = os.getenv(f"FHIR_{suffix}")
        if fallback not in {None, ""}:
            return str(fallback)
    return ""


def adapter_config_from_env(state: dict[str, Any] | None = None) -> FHIRAdapterConfig:
    adapter = normalize_adapter_name(
        _state_value(state, "fhir_adapter", "ehr_adapter", "fhir_source")
        or os.getenv("FHIR_ADAPTER")
        or os.getenv("EHR_ADAPTER")
        or "generic"
    )
    if adapter == "auto":
        adapter = infer_adapter_from_config(state)
    profile = ADAPTER_PROFILES.get(adapter, ADAPTER_PROFILES["generic"])
    prefix = profile["env_prefix"]

    def value(field: str, suffix: str, default: str = "") -> str:
        return (
            _state_value(state, f"{adapter}_{field}")
            or _env_value(prefix, suffix, include_generic_fallback=False)
            or _state_value(state, f"fhir_{field}")
            or _env_value("FHIR", suffix)
            or default
        )

    auth_type = value(
        "auth_type",
        "AUTH_TYPE",
        profile.get("default_auth_type", DEFAULT_FHIR_AUTH_TYPE),
    )
    client_auth_method = value(
        "client_auth_method",
        "CLIENT_AUTH_METHOD",
        DEFAULT_FHIR_CLIENT_AUTH_METHOD,
    )
    if auth_type.strip().lower() in {"backend_services_jwt", "private_key_jwt"}:
        client_auth_method = "private_key_jwt"

    return FHIRAdapterConfig(
        adapter=adapter,
        label=profile["label"],
        base_url=value("base_url", "BASE_URL"),
        auth_type=auth_type,
        token_url=value("token_url", "TOKEN_URL"),
        client_id=value("client_id", "CLIENT_ID"),
        client_secret=value("client_secret", "CLIENT_SECRET"),
        client_auth_method=client_auth_method,
        scope=value("scope", "SCOPE", DEFAULT_FHIR_SCOPE),
        audience=value("audience", "AUDIENCE"),
        private_key_path=value("private_key_path", "PRIVATE_KEY_PATH"),
        key_id=value("key_id", "KEY_ID"),
        jwks_url=value("jwks_url", "JWKS_URL"),
        access_token=value("access_token", "ACCESS_TOKEN"),
        timeout_seconds=int(float(value("timeout_seconds", "TIMEOUT_SECONDS", "30"))),
    )


def base64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def resolve_local_path(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        for base in (Path.cwd(), project_root(), Path(__file__).resolve().parent):
            resolved = (base / candidate).resolve()
            if resolved.exists():
                return resolved
        candidate = (project_root() / candidate).resolve()
    return candidate


def infer_adapter_from_config(state: dict[str, Any] | None = None) -> str:
    text = " ".join(
        value
        for value in (
            _state_value(state, "fhir_base_url", "base_url"),
            _state_value(state, "fhir_token_url", "token_url"),
            os.getenv("FHIR_BASE_URL", ""),
            os.getenv("FHIR_TOKEN_URL", ""),
        )
        if value
    ).lower()
    if "epic" in text:
        return "epic"
    if "nabidh" in text or "dha" in text:
        return "nabidh"
    if "cerner" in text or "oracle" in text:
        return "oracle_health"
    if "trakcare" in text or "intersystems" in text or "iris" in text:
        return "trakcare_iris"
    return "generic"


def post_form(
    *,
    url: str,
    form: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise FHIRAuthError(f"FHIR token request failed with HTTP {exc.code}: {body}") from exc


def build_private_key_jwt_assertion(
    *,
    token_url: str,
    client_id: str,
    private_key_path: str,
    key_id: str,
    jwks_url: str | None = None,
    audience: str | None = None,
) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(time.time())
    header = {
        "alg": "RS384",
        "typ": "JWT",
        "kid": key_id,
    }
    if jwks_url:
        header["jku"] = jwks_url

    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": audience or token_url,
        "jti": str(uuid.uuid4()),
        "nbf": now,
        "iat": now,
        "exp": now + 300,
    }
    signing_input = ".".join(
        [
            base64url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        ]
    ).encode("ascii")

    private_key = serialization.load_pem_private_key(
        resolve_local_path(private_key_path).read_bytes(),
        password=None,
    )
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA384())
    return f"{signing_input.decode('ascii')}.{base64url(signature)}"


class FHIRTokenManager:
    def __init__(self, config: FHIRAdapterConfig) -> None:
        self.config = config
        self._access_token: str | None = None
        self._expires_at_epoch = 0.0

    def invalidate(self) -> None:
        self._access_token = None
        self._expires_at_epoch = 0.0

    def access_token(self) -> str | None:
        if self.config.access_token:
            return self.config.access_token

        auth_type = self.config.auth_type.strip().lower()
        if auth_type in {"", "none", "no_auth"}:
            return None
        if auth_type not in {
            "client_credentials",
            "backend",
            "backend_services",
            "backend_services_jwt",
            "private_key_jwt",
            "static_bearer",
        }:
            return None
        if auth_type == "static_bearer":
            return None

        if self._access_token and time.time() < self._expires_at_epoch - 60:
            return self._access_token

        token_response = self._fetch_backend_token()
        access_token = token_response.get("access_token")
        if not access_token:
            raise FHIRAuthError("FHIR token response did not include access_token.")

        expires_in = int(float(token_response.get("expires_in") or 300))
        self._access_token = str(access_token)
        self._expires_at_epoch = time.time() + max(expires_in, 60)
        return self._access_token

    def _fetch_backend_token(self) -> dict[str, Any]:
        config = self.config
        if not config.token_url:
            raise FHIRAuthError("FHIR backend auth requires a token URL.")
        if not config.client_id:
            raise FHIRAuthError("FHIR backend auth requires a client ID.")

        form: dict[str, Any] = {
            "grant_type": "client_credentials",
            "scope": config.scope,
        }
        headers: dict[str, str] = {}
        client_auth_method = config.client_auth_method.strip().lower()

        if client_auth_method == "client_secret_basic":
            if not config.client_secret:
                raise FHIRAuthError(
                    "client_secret_basic requires a configured client secret."
                )
            credentials = f"{config.client_id}:{config.client_secret}".encode("utf-8")
            headers["Authorization"] = f"Basic {b64encode(credentials).decode('ascii')}"
            if config.audience:
                form["aud"] = config.audience
        elif client_auth_method == "client_secret_post":
            if not config.client_secret:
                raise FHIRAuthError(
                    "client_secret_post requires a configured client secret."
                )
            form["client_id"] = config.client_id
            form["client_secret"] = config.client_secret
            if config.audience:
                form["aud"] = config.audience
        elif client_auth_method == "private_key_jwt":
            if not config.private_key_path:
                raise FHIRAuthError("private_key_jwt requires a private key path.")
            if not config.key_id:
                raise FHIRAuthError("private_key_jwt requires a key ID.")
            form["client_assertion_type"] = (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            )
            form["client_assertion"] = build_private_key_jwt_assertion(
                token_url=config.token_url,
                client_id=config.client_id,
                private_key_path=config.private_key_path,
                key_id=config.key_id,
                jwks_url=config.jwks_url,
                audience=config.audience or config.token_url,
            )
        else:
            raise FHIRAuthError(
                "Unsupported FHIR client auth method. Use client_secret_basic, "
                "client_secret_post, or private_key_jwt."
            )

        return post_form(
            url=config.token_url,
            form=form,
            headers=headers,
            timeout_seconds=config.timeout_seconds,
        )


def reference_id(reference: str | None) -> str | None:
    if not reference:
        return None
    return str(reference).split("/")[-1]


class FHIRAdapter:
    def __init__(
        self,
        config: FHIRAdapterConfig,
        token_manager: FHIRTokenManager | None = None,
    ) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.token_manager = token_manager or FHIRTokenManager(config)

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url)

    @property
    def adapter_name(self) -> str:
        return self.config.adapter

    def access_token(self) -> str | None:
        return self.token_manager.access_token()

    def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if not self.base_url:
            raise FHIRRequestError(
                "FHIR base URL is not configured.",
                status_code=0,
                url=path,
            )

        query = f"?{urllib.parse.urlencode(params or {})}" if params else ""
        url = f"{self.base_url}/{path.lstrip('/')}{query}"

        for attempt in range(2):
            headers = {"Accept": "application/fhir+json, application/json"}
            token = self.access_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"

            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=self.config.timeout_seconds,
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 401 and attempt == 0:
                    self.token_manager.invalidate()
                    continue
                raise FHIRRequestError(
                    f"FHIR request failed {exc.code} for {url}: {body}",
                    status_code=exc.code,
                    url=url,
                ) from exc

        raise FHIRRequestError(
            f"FHIR request failed after token refresh for {url}",
            status_code=401,
            url=url,
        )

    def read(self, resource_type: str, resource_id: str | None) -> dict[str, Any] | None:
        if not resource_id:
            return None
        return self._request_json(f"{resource_type}/{reference_id(resource_id)}")

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict[str, Any]]:
        bundle = self._request_json(resource_type, params=params)
        return [
            entry["resource"]
            for entry in bundle.get("entry", [])
            if isinstance(entry.get("resource"), dict)
            and entry["resource"].get("resourceType") == resource_type
        ]

    def search_first(self, resource_type: str, params: dict[str, str]) -> dict[str, Any] | None:
        results = self.search(resource_type, params)
        return results[0] if results else None

    def search_with_fallback(
        self,
        resource_type: str,
        param_options: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for params in param_options:
            try:
                results = self.search(resource_type, params)
                if results:
                    return results
            except FHIRRequestError as exc:
                if exc.status_code in {400, 404, 422}:
                    last_error = exc
                    continue
                raise
        if last_error:
            return []
        return []

    def search_first_with_fallback(
        self,
        resource_type: str,
        param_options: list[dict[str, str]],
    ) -> dict[str, Any] | None:
        results = self.search_with_fallback(resource_type, param_options)
        return results[0] if results else None

    def coverage_for_patient(self, patient_id: str | None) -> dict[str, Any] | None:
        patient = reference_id(patient_id)
        if not patient:
            return None
        patient_ref = f"Patient/{patient}"
        return self.search_first_with_fallback(
            "Coverage",
            [
                {"beneficiary": patient_ref},
                {"beneficiary": patient},
                {"patient": patient},
                {"subscriber": patient_ref},
                {"subscriber": patient},
            ],
        )

    def search_patient_context(
        self,
        resource_type: str,
        *,
        patient_id: str | None,
        encounter_id: str | None = None,
    ) -> list[dict[str, Any]]:
        patient = reference_id(patient_id)
        encounter = reference_id(encounter_id)
        if not patient:
            return []

        patient_ref = f"Patient/{patient}"
        encounter_ref = f"Encounter/{encounter}" if encounter else ""
        options: list[dict[str, str]] = []

        if resource_type == "ChargeItem":
            if encounter_ref:
                options.extend(
                    [
                        {"subject": patient_ref, "context": encounter_ref},
                        {"patient": patient, "context": encounter_ref},
                    ]
                )
            options.extend([{"subject": patient_ref}, {"patient": patient}])
            return self.search_with_fallback(resource_type, options)

        if encounter_ref:
            options.extend(
                [
                    {"subject": patient_ref, "encounter": encounter_ref},
                    {"patient": patient, "encounter": encounter_ref},
                    {"patient": patient, "encounter": encounter},
                ]
            )
        options.extend([{"subject": patient_ref}, {"patient": patient}])
        return self.search_with_fallback(resource_type, options)


def get_fhir_adapter(state: dict[str, Any] | None = None) -> FHIRAdapter | None:
    config = adapter_config_from_env(state)
    if not config.base_url:
        return None
    return FHIRAdapter(config)


def fhir_backend_access_token(state: dict[str, Any] | None = None) -> str | None:
    config = adapter_config_from_env(state)
    return FHIRTokenManager(config).access_token()


def fhir_adapter_summary(state: dict[str, Any] | None = None) -> dict[str, Any]:
    config = adapter_config_from_env(state)
    return {
        "adapter": config.adapter,
        "label": config.label,
        "base_url_configured": bool(config.base_url),
        "auth_type": config.auth_type,
        "token_url_configured": bool(config.token_url),
        "client_id_configured": bool(config.client_id),
        "scope": config.scope,
    }
