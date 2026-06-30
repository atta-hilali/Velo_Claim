from __future__ import annotations

from datetime import datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from velo_claim.builders.prior_auth.canonical import PACanonicalForm


class ShafafiyaPABuilder:
    content_type = "application/xml"

    def build(self, pa_form: PACanonicalForm) -> str:
        root = Element("Prior.Request")
        header = SubElement(root, "Header")
        _text(header, "SenderID", pa_form.facility.get("license"))
        _text(header, "ReceiverID", pa_form.payer_id)
        _text(header, "TransactionDate", datetime.now().strftime("%d/%m/%Y %H:%M"))
        _text(header, "RecordCount", "1")
        _text(header, "DispositionFlag", "PTE_VALIDATE_ONLY")

        auth = SubElement(root, "Authorization")
        _text(auth, "Type", pa_form.coverage.get("authorization_type") or "Authorization")
        _text(auth, "ID", f"PA-{pa_form.claim_id}")
        _text(auth, "IDPayer", pa_form.pre_auth_ref)
        _text(auth, "MemberID", pa_form.patient.get("member_id"))
        _text(auth, "PayerID", pa_form.payer_id)
        _text(auth, "EmiratesIDNumber", pa_form.patient.get("emirates_id"))
        _text(auth, "DateOrdered", _format_date(pa_form.service_date))

        encounter = SubElement(auth, "Encounter")
        _text(encounter, "FacilityID", pa_form.facility.get("license"))
        _text(encounter, "Type", pa_form.coverage.get("encounter_type") or "1")
        _text(encounter, "Start", _format_dt(pa_form.service_date))
        _text(encounter, "End", _format_dt(pa_form.service_date))

        for index, code in enumerate(pa_form.diagnoses):
            diagnosis = SubElement(auth, "Diagnosis")
            _text(diagnosis, "Type", "Principal" if index == 0 else "Secondary")
            _text(diagnosis, "Code", code)

        for index, proc in enumerate(pa_form.procedures):
            activity = SubElement(auth, "Activity")
            _text(activity, "ID", proc.get("id") or f"ACT-{index + 1:03d}")
            _text(activity, "Start", _format_dt(pa_form.service_date))
            _text(activity, "Type", _activity_type(proc))
            _text(activity, "Code", proc.get("code"))
            _text(activity, "Quantity", proc.get("quantity", 1))
            _text(activity, "Net", _money(proc.get("net") or proc.get("amount") or proc.get("gross")))
            _text(activity, "OrderingClinician", pa_form.provider.get("license"))
            _text(activity, "Clinician", pa_form.provider.get("license"))
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


def _format_date(value: str | None) -> str:
    return _format_dt(value).split(" ")[0]


def _activity_type(line: dict) -> str:
    system = str(line.get("system", "")).upper()
    return "3" if system in {"CPT", "HCPCS"} else "8"
