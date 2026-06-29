from __future__ import annotations

from datetime import datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring


class EClaimLinkClaimBuilder:
    content_type = "application/xml"

    def build(self, canonical_claim: dict) -> str:
        root = Element("Claim.Submission")
        header = SubElement(root, "Header")
        _text(header, "SenderID", canonical_claim["provider"].get("facility_license"))
        _text(header, "ReceiverID", canonical_claim["payer"].get("id"))
        _text(header, "TransactionDate", _format_dt(datetime.now().isoformat()))
        _text(header, "RecordCount", "1")
        _text(header, "DispositionFlag", "TEST")

        claim = SubElement(root, "Claim")
        _text(claim, "ID", canonical_claim["claim_id"])
        _text(claim, "MemberID", canonical_claim["patient"].get("member_id"))
        _text(claim, "PatientID", canonical_claim["encounter"].get("patient_id") or canonical_claim["patient"].get("id"))
        _text(claim, "PayerID", canonical_claim["payer"].get("id"))
        _text(claim, "ProviderID", canonical_claim["provider"].get("facility_license"))
        _text(claim, "EmiratesIDNumber", canonical_claim["patient"].get("emirates_id"))
        _text(claim, "GrossAmount", _money(canonical_claim["amount"].get("gross")))
        _text(claim, "PatientShare", _money(canonical_claim["amount"].get("patient_share")))
        _text(claim, "NetAmount", _money(canonical_claim["amount"].get("net")))

        encounter = SubElement(claim, "Encounter")
        period = canonical_claim["encounter"].get("period", {})
        _text(encounter, "FacilityID", canonical_claim["provider"].get("facility_license"))
        _text(encounter, "Type", "1")
        _text(encounter, "Start", _format_dt(period.get("start") or canonical_claim["encounter"].get("service_date")))
        _text(encounter, "End", _format_dt(period.get("end") or period.get("start") or canonical_claim["encounter"].get("service_date")))
        _text(encounter, "StartType", "1")
        _text(encounter, "EndType", "1")

        for diagnosis in canonical_claim.get("diagnoses", []):
            node = SubElement(claim, "Diagnosis")
            _text(node, "Type", "Principal" if diagnosis.get("type") == "principal" else "Secondary")
            _text(node, "Code", diagnosis.get("code"))

        for line in canonical_claim.get("line_items", []):
            activity = SubElement(claim, "Activity")
            _text(activity, "ID", line.get("id"))
            _text(activity, "Start", _format_dt(period.get("start") or canonical_claim["encounter"].get("service_date")))
            _text(activity, "Type", _activity_type(line))
            _text(activity, "Code", line.get("code"))
            _text(activity, "Quantity", str(line.get("quantity", 1)))
            _text(activity, "Net", _money(line.get("net")))
            _text(activity, "OrderingClinician", canonical_claim["provider"].get("license"))
            _text(activity, "Clinician", canonical_claim["provider"].get("license"))
            if canonical_claim.get("pre_auth_ref"):
                _text(activity, "PriorAuthorizationID", canonical_claim["pre_auth_ref"])

        return minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  ")


def _text(parent: Element, name: str, value: object) -> None:
    if value is not None and value != "":
        SubElement(parent, name).text = str(value)


def _money(value: object) -> str:
    return f"{float(value or 0.0):.2f}"


def _format_dt(value: str | None) -> str:
    if not value:
        return datetime.now().strftime("%d/%m/%Y %H:%M")
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text}T00:00:00")
            return parsed.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return text


def _activity_type(line: dict) -> str:
    system = str(line.get("system", "")).upper()
    return "3" if system in {"CPT", "HCPCS"} else "8"
