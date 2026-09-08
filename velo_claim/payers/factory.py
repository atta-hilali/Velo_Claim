from __future__ import annotations

import os

from velo_claim.payers.http import HttpPayerTransport
from velo_claim.payers.interface import PayerTransportRegistry
from velo_claim.payers.manual import ManualPayerTransport


def build_payer_transport_registry() -> PayerTransportRegistry:
    manual = ManualPayerTransport()
    timeout = int(os.getenv("PAYER_TRANSPORT_TIMEOUT_SECONDS", "30"))
    nphies = HttpPayerTransport(
        endpoints={
            "eligibility": os.getenv("NPHIES_ELIGIBILITY_URL") or os.getenv("NPHIES_PROCESS_MESSAGE_URL", ""),
            "prior_auth": os.getenv("NPHIES_PRIOR_AUTH_URL") or os.getenv("NPHIES_PROCESS_MESSAGE_URL", ""),
            "claim": os.getenv("NPHIES_CLAIM_SUBMISSION_URL") or os.getenv("NPHIES_PROCESS_MESSAGE_URL", ""),
        },
        poll_endpoints={
            "eligibility": os.getenv("NPHIES_ELIGIBILITY_POLL_URL", ""),
            "prior_auth": os.getenv("NPHIES_PRIOR_AUTH_POLL_URL") or os.getenv("PRIOR_AUTH_POLL_URL", ""),
            "claim": os.getenv("NPHIES_CLAIM_POLL_URL", ""),
        },
        access_token=os.getenv("NPHIES_ACCESS_TOKEN") or os.getenv("PRIOR_AUTH_ACCESS_TOKEN"),
        timeout_seconds=timeout,
    )
    eclaimlink = HttpPayerTransport(
        endpoints={
            "eligibility": os.getenv("ECLAIMLINK_ELIGIBILITY_URL", ""),
            "prior_auth": os.getenv("ECLAIMLINK_PRIOR_AUTH_URL", ""),
            "claim": os.getenv("ECLAIMLINK_CLAIM_SUBMISSION_URL", ""),
        },
        poll_endpoints={
            "eligibility": os.getenv("ECLAIMLINK_ELIGIBILITY_POLL_URL", ""),
            "prior_auth": os.getenv("ECLAIMLINK_PRIOR_AUTH_POLL_URL", ""),
            "claim": os.getenv("ECLAIMLINK_CLAIM_POLL_URL", ""),
        },
        access_token=os.getenv("ECLAIMLINK_ACCESS_TOKEN"),
        timeout_seconds=timeout,
    )
    return PayerTransportRegistry({"NPHIES": nphies, "ECLAIMLINK": eclaimlink}, manual)
