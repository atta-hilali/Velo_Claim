from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


TransportStatus = Literal[
    "accepted",
    "queued",
    "pended",
    "partial",
    "complete",
    "rejected",
    "error",
    "delivery_unknown",
    "manual_required",
]


@dataclass(slots=True)
class TransportRequest:
    transaction_type: Literal["eligibility", "prior_auth", "claim"]
    standard: str
    payload: str
    content_type: str
    correlation_id: str
    payer_id: str
    filename: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TransportResult:
    status: TransportStatus
    correlation_id: str
    external_id: str | None = None
    response: Any = None
    retry_after_seconds: int | None = None
    diagnostic: dict[str, Any] = field(default_factory=dict)

    @property
    def is_waiting(self) -> bool:
        return self.status in {"accepted", "queued", "pended", "partial", "delivery_unknown"}

    @property
    def is_final(self) -> bool:
        return self.status in {"complete", "rejected", "error"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PayerTransportInterface(ABC):
    @abstractmethod
    def submit(self, request: TransportRequest) -> TransportResult: ...

    @abstractmethod
    def poll(self, request: TransportRequest, external_id: str | None = None) -> TransportResult: ...


class PayerTransportRegistry:
    def __init__(self, transports: dict[str, PayerTransportInterface], default: PayerTransportInterface) -> None:
        self._transports = {str(key).upper(): value for key, value in transports.items()}
        self._default = default

    def resolve(self, standard: str) -> PayerTransportInterface:
        return self._transports.get(str(standard).upper(), self._default)
