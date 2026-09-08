from __future__ import annotations

from velo_claim.payers.interface import PayerTransportInterface, TransportRequest, TransportResult


class ShafafiyaPayerTransport(PayerTransportInterface):
    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def submit(self, request: TransportRequest) -> TransportResult:
        filename = request.filename or f"vc-{request.correlation_id}.xml"
        try:
            response = self.adapter.upload(request.payload.encode("utf-8"), filename)
        except Exception as exc:
            return TransportResult(
                "delivery_unknown",
                request.correlation_id,
                diagnostic={"error_type": type(exc).__name__, "message": str(exc)},
            )
        code = int(response.get("code", -999))
        if code in {0, 1} and response.get("transaction_id"):
            status = "accepted"
        elif code in {-1, -2, -3, -7, -12}:
            status = "rejected"
        else:
            status = "delivery_unknown"
        return TransportResult(
            status,
            request.correlation_id,
            external_id=str(response.get("transaction_id")) if response.get("transaction_id") else None,
            response=response,
            diagnostic={"platform_code": code},
        )

    def poll(self, request: TransportRequest, external_id: str | None = None) -> TransportResult:
        return TransportResult("manual_required", request.correlation_id, external_id=external_id)
