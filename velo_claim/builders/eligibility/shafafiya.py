from __future__ import annotations

from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from velo_claim.standards.shafafiya import (
    authorization_request_id,
    disposition_flag,
    encounter_type_code,
    format_date,
    format_datetime,
)


class ShafafiyaEligibilityBuilder:
    """Build the DOH/Shafafiya eligibility transaction defined by PriorRequest.xsd."""

    content_type = "application/xml"

    def build(self, canonical_claim: dict, source_context: dict | None = None) -> str:
        del source_context
        patient = canonical_claim.get("patient", {})
        payer = canonical_claim.get("payer", {})
        provider = canonical_claim.get("provider", {})
        encounter_data = canonical_claim.get("encounter", {})
        facility_id = provider.get("facility_license")

        root = Element("Prior.Request")
        header = SubElement(root, "Header")
        _text(header, "SenderID", facility_id)
        _text(header, "ReceiverID", payer.get("id"))
        _text(header, "TransactionDate", format_datetime(None))
        _text(header, "RecordCount", "1")
        _text(header, "DispositionFlag", disposition_flag())

        authorization = SubElement(root, "Authorization")
        _text(authorization, "Type", "Eligibility")
        _text(
            authorization,
            "ID",
            authorization_request_id(facility_id, f"ELIG-{canonical_claim.get('claim_id')}"),
        )
        _text(authorization, "MemberID", patient.get("member_id"))
        _text(authorization, "PayerID", payer.get("id"))
        _text(authorization, "EmiratesIDNumber", patient.get("emirates_id"))
        _text(authorization, "DateOrdered", format_date(encounter_data.get("service_date")))

        encounter = SubElement(authorization, "Encounter")
        period = encounter_data.get("period", {})
        _text(encounter, "FacilityID", facility_id)
        _text(encounter, "Type", encounter_type_code(encounter_data))
        _text(encounter, "Start", format_datetime(period.get("start") or encounter_data.get("service_date")))
        _text(
            encounter,
            "End",
            format_datetime(period.get("end") or period.get("start") or encounter_data.get("service_date")),
        )

        return minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  ")


def _text(parent: Element, name: str, value: object) -> None:
    if value is not None and value != "":
        SubElement(parent, name).text = str(value)
