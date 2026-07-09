# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Endpoint primitives for the API-layer (B1..B5) sweep — the tuples the generator crosses.

Nothing here is a hand-written test. Two primitive tables + one param builder; `cases.generate_http_
cases()` crosses them with the hostile principals to PRODUCE the cases, and `test_endpoint_sweep.py`
judges each against the oracle. Add a doctype to SENSITIVE_DOCTYPES or a method to APP_ENDPOINTS and
the whole matrix scales automatically.

  GENERIC_ENDPOINTS  doctype-PARAMETRIC generic Frappe endpoints (frappe.client.*). One primitive
                     works on every doctype, so it is crossed with SENSITIVE_DOCTYPES.
  APP_ENDPOINTS      NAMED app methods (their id-param is fixed, not doctype-derived). Enumerated
                     from the VAPT; the Layer-2 sweep additionally AUTO-DISCOVERS every other
                     @frappe.whitelist method, so this list is the known-seed, not the ceiling.
  SENSITIVE_DOCTYPES the objects a hostile principal must never reach, with the metadata the runner
                     needs to seed a foreign-owned instance and probe a write.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointSpec:
	key: str
	method: str          # dotted whitelisted path (any app / generic Frappe)
	http: str            # GET | POST
	action: str          # read | write | create | delete | list | info
	vector: str          # B1..B5
	doctype: str = ""    # fixed for APP_ENDPOINTS; "" = doctype-parametric (GENERIC)
	id_param: str = ""   # request key carrying the object id ("" = none / list / info)


# -- doctype-parametric generic Frappe endpoints (crossed with SENSITIVE_DOCTYPES) ------------------
GENERIC_ENDPOINTS = [
	EndpointSpec("client-get", "frappe.client.get", "GET", "read", "B1", id_param="name"),
	EndpointSpec("client-get-value", "frappe.client.get_value", "GET", "read", "B1", id_param="name"),
	EndpointSpec("client-set-value", "frappe.client.set_value", "POST", "write", "B2", id_param="name"),
	EndpointSpec("client-delete", "frappe.client.delete", "POST", "delete", "B3", id_param="name"),
	EndpointSpec("client-insert", "frappe.client.insert", "POST", "create", "B3"),
	EndpointSpec("client-get-list", "frappe.client.get_list", "POST", "list", "B3"),
	EndpointSpec("reportview-get", "frappe.desk.reportview.get", "POST", "list", "B3"),
	# doctype-parametric app list handlers (take a doctype in the body, like the generics)
	EndpointSpec("hd-list-data", "helpdesk.api.doc.get_list_data", "POST", "list", "B3"),
	EndpointSpec("crm-get-data", "crm.api.doc.get_data", "POST", "list", "B3"),
]

# -- named app / desk methods from the VAPT (their id-param is intrinsic, not doctype-derived) ------
APP_ENDPOINTS = [
	EndpointSpec("hd-ticket-contact", "helpdesk.helpdesk.doctype.hd_ticket.api.get_ticket_contact",
	             "POST", "read", "B1", "HD Ticket", "ticket"),
	EndpointSpec("hd-ticket-activities", "helpdesk.helpdesk.doctype.hd_ticket.api.get_ticket_activities",
	             "POST", "read", "B1", "HD Ticket", "ticket"),
	EndpointSpec("lms-job-details", "lms.lms.api.get_job_details", "POST", "read", "B1",
	             "Job Opportunity", "job"),
	EndpointSpec("lms-job-opportunities", "lms.lms.api.get_job_opportunities", "POST", "list", "B3",
	             "Job Opportunity"),
	EndpointSpec("lms-courses", "lms.lms.api.get_courses", "POST", "list", "B3", "LMS Course"),
	EndpointSpec("lms-programs", "lms.lms.api.get_programs", "POST", "list", "B3", "LMS Program"),
	EndpointSpec("lms-batches", "lms.lms.api.get_batches", "POST", "list", "B3", "LMS Batch"),
	EndpointSpec("lms-article-stats", "lms.lms.api.get_article_stats", "POST", "info", "B4"),
	EndpointSpec("linked-docs", "frappe.desk.form.linked_with.get_linked_docs", "POST", "read", "B1",
	             "CRM Lead", "docname"),
	EndpointSpec("installed-apps", "frappe.apps.get_apps", "GET", "info", "B4"),
]

# -- the objects a hostile principal must never reach (crossed with GENERIC_ENDPOINTS) --------------
# write_field: a benign field a set_value (B2) probe may target on a real foreign-owned row.
SENSITIVE_DOCTYPES = [
	{"doctype": "Comment", "app": "frappe", "private": False, "write_field": "content"},
	{"doctype": "Contact", "app": "frappe", "private": False, "write_field": "first_name"},
	{"doctype": "HD Ticket", "app": "helpdesk", "private": False, "write_field": "subject"},
	{"doctype": "CRM Call Log", "app": "crm", "private": False, "write_field": "status"},
	{"doctype": "CRM Lead", "app": "crm", "private": False, "write_field": "first_name"},
	{"doctype": "CRM Deal", "app": "crm", "private": False, "write_field": "organization"},
	{"doctype": "CRM Task", "app": "crm", "private": False, "write_field": "title"},
	{"doctype": "FCRM Note", "app": "crm", "private": False, "write_field": "title"},
	{"doctype": "File", "app": "frappe", "private": True, "write_field": "file_name"},
	{"doctype": "Assignment Rule", "app": "frappe", "private": False, "write_field": "description"},
	{"doctype": "ToDo", "app": "frappe", "private": False, "write_field": "description"},
]

_BY_DOCTYPE = {d["doctype"]: d for d in SENSITIVE_DOCTYPES}


def doctype_meta(doctype):
	return _BY_DOCTYPE.get(doctype)


def build_params(spec, doctype, obj_id):
	"""The request body/query for `spec` targeting `obj_id` on `doctype`. One place, driven by the
	primitive's action — never a per-case literal. `obj_id` is a REAL foreign-owned name the runner seeded."""
	if spec.action == "read":
		if spec.id_param and spec.id_param != "name":
			return {spec.id_param: obj_id}                       # app method: {ticket|job|docname: id}
		p = {"name": obj_id}
		if not spec.doctype:                                     # generic endpoint carries the doctype
			p["doctype"] = doctype
		return p
	if spec.action == "write":
		wf = (doctype_meta(doctype) or {}).get("write_field", "name")
		return {"doctype": doctype, "name": obj_id, "fieldname": wf, "value": "vapt-probe"}
	if spec.action == "delete":
		return {"doctype": doctype, "name": obj_id}
	if spec.action == "create":
		return {"doc": {"doctype": doctype}}
	if spec.action == "list":
		if spec.doctype:                                         # named list endpoint (fixed doctype, LMS)
			return {}                                            # a deny-test needs no exact paging body
		return {"doctype": doctype, "filters": {}, "order_by": "modified desc", "page_length": 20}
	return {}                                                    # info: no object
