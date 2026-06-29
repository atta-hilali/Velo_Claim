from __future__ import annotations

from typing import Any

from velo_claim.core.models import CanonicalState, default_state


class InputResolver:
    """Normalise all entrypoint styles into the canonical state shape."""

    def resolve(self, raw_input: dict[str, Any]) -> CanonicalState:
        state = {**default_state(), **raw_input}
        if "encounter_package" in raw_input and "source_context" not in raw_input:
            package = raw_input.get("encounter_package") or {}
            state["source_context"] = {
                "patient": package.get("patient", {}),
                "coverage": package.get("coverage", {}),
                "encounter": package.get("encounter", {}),
                "provider": package.get("provider", {}),
                "facility": package.get("facility", {}),
                "conditions": package.get("conditions", []),
                "procedures": package.get("procedures", []),
                "attachments": package.get("attachments", []),
                "charge_items": package.get("charge_items", []),
                "payer_rules": package.get("payer_rules", raw_input.get("payer_rules", [])),
            }
        return state
