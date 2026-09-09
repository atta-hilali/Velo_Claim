from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from velo_claim.corrections.patches import UnsafeCorrectionError
from velo_claim.corrections.service import (
    CorrectionNotFoundError,
    CorrectionWorkflowService,
    InvalidCorrectionStateError,
    StaleCorrectionError,
)
from velo_claim.storage.interfaces import DuplicateRecordError
from velo_claim.submission.api import reviewer


class GenerateCorrectionsIn(BaseModel):
    validation_report_id: str | None = None


class ReviewCorrectionIn(BaseModel):
    decision: Literal["APPROVED", "MODIFIED", "REJECTED"]
    modified_value: Any = None
    comment: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_modified_value(self):
        if self.decision == "MODIFIED" and self.modified_value is None:
            raise ValueError("MODIFIED review requires modified_value.")
        return self


def build_correction_router(get_services) -> APIRouter:
    router = APIRouter(tags=["corrections"])

    def service() -> CorrectionWorkflowService:
        return CorrectionWorkflowService(get_services())

    @router.get("/claims/{claim_id}/corrections")
    def list_corrections(claim_id: str, actor=Depends(reviewer)) -> dict[str, Any]:
        return _call(lambda: service().list_cycles(claim_id))

    @router.get("/claims/{claim_id}/corrections/{cycle_id}")
    def get_correction_cycle(claim_id: str, cycle_id: str, actor=Depends(reviewer)) -> dict[str, Any]:
        return _call(lambda: service().get_cycle(claim_id, cycle_id))

    @router.post("/claims/{claim_id}/corrections/generate")
    def generate_corrections(
        claim_id: str,
        body: GenerateCorrectionsIn | None = None,
        actor=Depends(reviewer),
    ) -> dict[str, Any]:
        body = body or GenerateCorrectionsIn()
        return _call(
            lambda: service().generate(
                claim_id,
                validation_report_id=body.validation_report_id,
            )
        )

    @router.post("/claims/{claim_id}/corrections/{suggestion_id}/review")
    def review_correction(
        claim_id: str,
        suggestion_id: str,
        body: ReviewCorrectionIn,
        actor=Depends(reviewer),
    ) -> dict[str, Any]:
        return _call(
            lambda: service().review(
                claim_id=claim_id,
                suggestion_id=suggestion_id,
                decision=body.decision,
                reviewer_id=actor,
                modified_value=body.modified_value,
                comment=body.comment,
            )
        )

    @router.post("/claims/{claim_id}/corrections/{cycle_id}/apply")
    def apply_correction_cycle(
        claim_id: str,
        cycle_id: str,
        actor=Depends(reviewer),
    ) -> dict[str, Any]:
        return _call(lambda: service().apply(claim_id=claim_id, cycle_id=cycle_id, reviewer_id=actor))

    return router


def _call(fn):
    try:
        return fn()
    except CorrectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (StaleCorrectionError, DuplicateRecordError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidCorrectionStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsafeCorrectionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
