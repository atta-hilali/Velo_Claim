from __future__ import annotations

from datetime import datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from velo_claim.builders.prior_auth.canonical import PACanonicalForm


class ShafafiyaPABuilder:
    content_type = "application/xml"

    def build(self, pa_form: PACanonicalForm) -> str:
        root = Element("Prior.Authorization")
        header = SubElement(root, "Header")
        _text(header, "SenderID", pa_form.facility.get("license"))
        _text(header, "ReceiverID", pa_form.payer_id)
        _text(header, "TransactionDate", datetime.now().strftime("%d/%m/%Y %H:%M"))
        _text(header, "DispositionFlag", "PTE_VALIDATE_ONLY")

        auth = SubElement(root, "Authorization")
        _text(auth, "ClaimID", pa_form.claim_id)
        _text(auth, "MemberID", pa_form.patient.get("member_id"))
        _text(auth, "PayerID", pa_form.payer_id)
        _text(auth, "ProviderID", pa_form.facility.get("license"))
        _text(auth, "ServiceDate", pa_form.service_date)
        for code in pa_form.diagnoses:
            diagnosis = SubElement(auth, "Diagnosis")
            _text(diagnosis, "Code", code)
        for proc in pa_form.procedures:
            activity = SubElement(auth, "Activity")
            _text(activity, "Code", proc.get("code"))
            _text(activity, "Quantity", proc.get("quantity", 1))
            _text(activity, "Clinician", pa_form.provider.get("license"))
        for doc in pa_form.supporting_docs:
            document = SubElement(auth, "Attachment")
            _text(document, "Reference", doc)
        return minidom.parseString(tostring(root, encoding="utf-8")).toprettyxml(indent="  ")


def _text(parent: Element, name: str, value: object) -> None:
    if value is not None and value != "":
        SubElement(parent, name).text = str(value)
