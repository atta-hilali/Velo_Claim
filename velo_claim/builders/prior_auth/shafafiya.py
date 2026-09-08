from __future__ import annotations

from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from velo_claim.builders.prior_auth.canonical import PACanonicalForm
from velo_claim.standards.shafafiya import (
    activity_type_code,
    authorization_request_id,
    bounded_identifier,
    disposition_flag,
    encounter_type_code,
    format_date,
    format_datetime,
    money,
)


class ShafafiyaPABuilder:
    content_type = "application/xml"

    def build(self, pa_form: PACanonicalForm) -> str:
        root = Element("Prior.Request")
        header = SubElement(root, "Header")
        _text(header, "SenderID", pa_form.facility.get("license"))
        _text(header, "ReceiverID", pa_form.payer_id)
        _text(header, "TransactionDate", format_datetime(None))
        _text(header, "RecordCount", "1")
        _text(header, "DispositionFlag", disposition_flag())

        auth = SubElement(root, "Authorization")
        _text(auth, "Type", pa_form.coverage.get("authorization_type") or "Authorization")
        _text(
            auth,
            "ID",
            pa_form.request_identifier
            or authorization_request_id(pa_form.facility.get("license"), pa_form.claim_id),
        )
        _text(auth, "IDPayer", pa_form.pre_auth_ref)
        _text(auth, "MemberID", pa_form.patient.get("member_id"))
        _text(auth, "PayerID", pa_form.payer_id)
        _text(auth, "EmiratesIDNumber", pa_form.patient.get("emirates_id"))
        _text(auth, "DateOrdered", format_date(pa_form.service_date))

        encounter = SubElement(auth, "Encounter")
        _text(encounter, "FacilityID", pa_form.facility.get("license"))
        _text(encounter, "Type", encounter_type_code(pa_form.encounter))
        period = pa_form.encounter.get("period", {})
        _text(encounter, "Start", format_datetime(period.get("start") or pa_form.service_date))
        _text(
            encounter,
            "End",
            format_datetime(period.get("end") or period.get("start") or pa_form.service_date),
        )

        for index, code in enumerate(pa_form.diagnoses):
            diagnosis = SubElement(auth, "Diagnosis")
            _text(diagnosis, "Type", "Principal" if index == 0 else "Secondary")
            _text(diagnosis, "Code", code)

        for index, proc in enumerate(pa_form.procedures):
            activity = SubElement(auth, "Activity")
            _text(activity, "ID", bounded_identifier(proc.get("id") or f"ACT-{index + 1:03d}"))
            _text(
                activity,
                "Start",
                format_datetime(proc.get("service_date") or period.get("start") or pa_form.service_date),
            )
            _text(activity, "Type", activity_type_code(proc.get("system")))
            _text(activity, "Code", proc.get("code"))
            _text(activity, "Quantity", proc.get("quantity", 1))
            _text(activity, "Net", money(proc.get("net") or proc.get("amount") or proc.get("gross")))
            _text(activity, "OrderingClinician", pa_form.provider.get("license"))
            _text(activity, "Clinician", pa_form.provider.get("license"))
        return minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  ")


def _text(parent: Element, name: str, value: object) -> None:
    if value is not None and value != "":
        SubElement(parent, name).text = str(value)
