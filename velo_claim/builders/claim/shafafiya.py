from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from velo_claim.standards.shafafiya import (
    activity_type_code,
    bounded_identifier,
    disposition_flag,
    encounter_end_type_code,
    encounter_start_type_code,
    encounter_type_code,
    format_date,
    format_datetime,
    money,
)


class ShafafiyaClaimBuilder:
    content_type = "application/xml"

    def build(self, canonical_claim: dict) -> str:
        root = Element("Claim.Submission")
        header = SubElement(root, "Header")
        _text(header, "SenderID", canonical_claim["provider"].get("facility_license"))
        _text(header, "ReceiverID", canonical_claim["payer"].get("id"))
        _text(header, "TransactionDate", format_datetime(None))
        _text(header, "RecordCount", "1")
        _text(header, "DispositionFlag", disposition_flag())

        claim = SubElement(root, "Claim")
        _text(claim, "ID", canonical_claim["claim_id"])
        _text(claim, "MemberID", canonical_claim["patient"].get("member_id"))
        _text(claim, "PayerID", canonical_claim["payer"].get("id"))
        _text(claim, "ProviderID", canonical_claim["provider"].get("facility_license"))
        _text(claim, "EmiratesIDNumber", canonical_claim["patient"].get("emirates_id"))
        _text(claim, "Gross", _money(canonical_claim["amount"].get("gross")))
        _text(claim, "PatientShare", _money(canonical_claim["amount"].get("patient_share")))
        _text(claim, "Net", _money(canonical_claim["amount"].get("net")))
        if canonical_claim["amount"].get("vat") is not None:
            _text(claim, "VAT", money(canonical_claim["amount"].get("vat")))

        encounter = SubElement(claim, "Encounter")
        period = canonical_claim["encounter"].get("period", {})
        _text(encounter, "FacilityID", canonical_claim["provider"].get("facility_license"))
        _text(encounter, "Type", encounter_type_code(canonical_claim["encounter"]))
        _text(encounter, "PatientID", canonical_claim["encounter"].get("patient_id") or canonical_claim["patient"].get("id"))
        _text(encounter, "EligibilityIDPayer", canonical_claim["payer"].get("eligibility_ref"))
        _text(encounter, "Start", format_datetime(period.get("start") or canonical_claim["encounter"].get("service_date")))
        _text(encounter, "End", format_datetime(period.get("end") or period.get("start") or canonical_claim["encounter"].get("service_date")))
        _text(encounter, "StartType", encounter_start_type_code(canonical_claim["encounter"]))
        _text(encounter, "EndType", encounter_end_type_code(canonical_claim["encounter"]))

        for diagnosis in canonical_claim.get("diagnoses", []):
            node = SubElement(claim, "Diagnosis")
            _text(node, "Type", "Principal" if diagnosis.get("type") == "principal" else "Secondary")
            _text(node, "Code", diagnosis.get("code"))

        for line in canonical_claim.get("line_items", []):
            activity = SubElement(claim, "Activity")
            _text(activity, "ID", bounded_identifier(line.get("id")))
            _text(activity, "Start", format_datetime(line.get("service_date") or period.get("start") or canonical_claim["encounter"].get("service_date")))
            _text(activity, "Type", activity_type_code(line.get("system")))
            _text(activity, "Code", line.get("code"))
            _text(activity, "Quantity", str(line.get("quantity", 1)))
            _text(activity, "Net", _money(line.get("net")))
            _text(activity, "OrderingClinician", canonical_claim["provider"].get("license"))
            _text(activity, "Clinician", canonical_claim["provider"].get("license"))
            if canonical_claim.get("pre_auth_ref"):
                _text(activity, "PriorAuthorizationID", canonical_claim["pre_auth_ref"])
            if line.get("vat") is not None:
                _text(activity, "VAT", money(line.get("vat")))
            if line.get("vat_percent") is not None:
                _text(activity, "VATPercent", line.get("vat_percent"))
            if line.get("date_ordered"):
                _text(activity, "DateOrdered", format_date(line.get("date_ordered")))

        return minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  ")


def _text(parent: Element, name: str, value: object) -> None:
    if value is not None and value != "":
        SubElement(parent, name).text = str(value)


def _money(value: object) -> str:
    return money(value)
