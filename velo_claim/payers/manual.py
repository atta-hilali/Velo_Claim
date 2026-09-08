from __future__ import annotations

from velo_claim.payers.interface import PayerTransportInterface, TransportRequest, TransportResult


class ManualPayerTransport(PayerTransportInterface):
    def submit(self, request: TransportRequest) -> TransportResult:
        return TransportResult(
            status="manual_required",
            correlation_id=request.correlation_id,
            diagnostic={"standard": request.standard, "transaction_type": request.transaction_type},
        )

    def poll(self, request: TransportRequest, external_id: str | None = None) -> TransportResult:
        return self.submit(request)
