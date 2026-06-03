"""Public patient-enrolment endpoints (replace the LSQ hosted forms).

A public www page (e.g. /nivolumab-enrolment) POSTs here. The form is public, but
the WRITE runs server-side with elevated permissions and HARD-CODES the routing —
so the submitter never needs (and can never set) the manager-level routing fields,
and the public can only ever submit the fixed field set below. Find-or-create by
phone (the platform dedup key); the `CRM Lead.before_insert` dedup guard still
applies on the create path.
"""
import re

import frappe
from frappe import _

# Fixed routing for the Nivolumab (Tishtha) enrolment form — stamped server-side.
NIVOLUMAB_ROUTING = {
	"source": "Nivolumab",
	"custom_vertical": "GoodFlip Care",
	"custom_psp_group": "Anaya",
	"custom_current_program": "Nivolumab",
	"custom_origin_vertical": "GoodFlip Care",
}
REQUIRED = ("patient_name", "phone", "doctor_name", "hospital_name", "city", "nivolumab_dosage", "nivolumab_indication")


def _normalize_phone(raw: str) -> str:
	"""-> E.164 +91XXXXXXXXXX. Accepts 10-digit, 91-prefixed, or +91 forms."""
	digits = re.sub(r"\D", "", raw or "")
	if len(digits) == 10:
		digits = "91" + digits
	return "+" + digits


def _one_row(doc, table):
	"""The single profile row for a child table — reuse or append one."""
	rows = doc.get(table)
	return rows[0] if rows else doc.append(table, {})


@frappe.whitelist(allow_guest=True)
def nivolumab_enrolment(**kwargs):
	missing = [f for f in REQUIRED if not (kwargs.get(f) or "").strip()]
	if missing:
		frappe.throw(_("Missing required field(s): {0}").format(", ".join(missing)))

	mobile = _normalize_phone(kwargs["phone"])
	name = frappe.db.get_value("CRM Lead", {"mobile_no": mobile}, "name")
	lead = frappe.get_doc("CRM Lead", name) if name else frappe.new_doc("CRM Lead")
	action = "updated" if name else "created"

	# identity + routing (manager-level fields, set server-side under ignore_permissions)
	lead.mobile_no = mobile
	lead.first_name = kwargs["patient_name"].strip()
	lead.status = lead.status or "New"
	lead.custom_city = kwargs["city"].strip()
	lead.update(NIVOLUMAB_ROUTING)

	plan = _one_row(lead, "custom_plan_profile")
	plan.nivo_indication = kwargs["nivolumab_indication"].strip()
	plan.nivo_dosage = kwargs["nivolumab_dosage"].strip()

	care = _one_row(lead, "custom_care_providers_profile")
	care.doctor_name = kwargs["doctor_name"].strip()
	care.hospital_name = kwargs["hospital_name"].strip()

	lead.save(ignore_permissions=True) if name else lead.insert(ignore_permissions=True)

	remarks = (kwargs.get("remarks") or "").strip()
	if remarks:
		frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": "Enrolment remarks",
				"content": remarks,
				"reference_doctype": "CRM Lead",
				"reference_docname": lead.name,
			}
		).insert(ignore_permissions=True)

	_attach_prescription(lead.name)
	frappe.db.commit()
	return {"ok": True, "action": action, "lead": lead.name}


def _attach_prescription(lead_name):
	"""Save an optional uploaded prescription file, attached to the lead."""
	files = getattr(frappe.request, "files", None)
	uploaded = files.get("prescription") if files else None
	if not uploaded or not uploaded.filename:
		return
	content = uploaded.stream.read()
	if not content:
		return
	from frappe.utils.file_manager import save_file

	save_file(uploaded.filename, content, "CRM Lead", lead_name, is_private=1)
