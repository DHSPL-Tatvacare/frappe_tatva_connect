# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Global spotlight search — one SQLiteSearch subclass on Frappe's native FTS5 framework."""
import re
from typing import ClassVar

import frappe
from frappe.search.sqlite_search import SQLiteSearch

_NON_DIGIT = re.compile(r"\D")


def _phone_tokens(num):
	# A phone is indexed digits-only and as its last 10 digits, so it matches with or without the country code.
	if not num:
		return []
	digits = _NON_DIGIT.sub("", str(num))
	forms = {digits}
	if len(digits) > 10:
		forms.add(digits[-10:])
	return [f for f in forms if f]

from tatva_connect.access import entitlement
from tatva_connect.access.visibility import _ref_parent
from tatva_connect.api.partner_file import _file_lead
from tatva_connect.automation.settings import is_enabled

# The dormant operator toggle that gates the feature — a CRM Tatva Automation row, like every switch.
TOGGLE = "Search::Index::indexing"

# Which lead-detail tab a hit opens; a lead opens the detail root (the frontend navigates via the hash).
TAB = {"CRM Lead": None, "FCRM Note": "notes", "CRM Task": "tasks", "CRM Call Log": "calls", "File": "attachments"}

# The batch build SELECTs these real columns before prepare_document runs; title/content map to always-present
# system columns we overwrite there (row title = patient name, content = composed), so no row is ever skipped.
_PLACEHOLDER = [{"title": "name"}, {"content": "creation"}]


class CRMLeadSearch(SQLiteSearch):
	INDEX_NAME = "crm_lead_search.db"

	# Metadata is stored + returned but not tokenized; `lead_group` avoids the SQL reserved word `group`.
	INDEX_SCHEMA: ClassVar[dict] = {
		"metadata_fields": ["lead", "phone", "status", "vertical", "lead_group", "assignee"],
		"tokenizer": "unicode61 remove_diacritics 2 tokenchars '-_@.+'",
	}

	INDEXABLE_DOCTYPES: ClassVar[dict] = {
		"CRM Lead": {"fields": [*_PLACEHOLDER, "mobile_no", "custom_alternate_number", "custom_patient_id", "custom_lsq_prospect_id"]},
		"FCRM Note": {"fields": [*_PLACEHOLDER, "title", "content", "reference_doctype", "reference_docname"]},
		# Task and Call Log are out — low search value, high volume. Re-enable by uncommenting; _content_of/TAB cover both.
		# "CRM Task": {"fields": [*_PLACEHOLDER, "title", "description", "reference_doctype", "reference_docname"]},
		# "CRM Call Log": {"fields": [*_PLACEHOLDER, "from", "to", "reference_doctype", "reference_docname"]},
		"File": {"fields": [*_PLACEHOLDER, "file_name", "attached_to_doctype", "attached_to_name"]},
	}

	def is_search_enabled(self):
		# OFF -> no index file, so every doc-event hook no-ops on index_exists(); that is the migration bulk guard.
		return is_enabled(TOGGLE)

	def get_search_filters(self):
		# Scope to leads the caller may SEE via the real org-hierarchy engine (get_list), not grain; every row
		# carries `lead`, so one IN-clause scopes children too. System Manager is exempt; nobody else unbounded.
		if entitlement.entitled_grains() == entitlement.ALL_GRAINS:
			return {}
		return {"lead": frappe.get_list("CRM Lead", pluck="name", limit_page_length=0)}

	def prepare_document(self, doc):
		# Every row's title is the parent patient's name; content is composed; metadata carries the lead + grain.
		lead = self._lead_of(doc)
		if not lead:
			return None
		ctx = self._lead_context(lead)
		if not ctx:
			return None
		document = super().prepare_document(doc)
		if not document:
			return None
		# Overwrite the placeholder title/content the base filled from name/creation.
		document["title"] = ctx.get("title") or lead
		document["content"] = self._content_of(doc, ctx)
		document["lead"] = lead
		document["phone"] = ctx.get("phone")
		document["status"] = ctx.get("status")
		document["vertical"] = ctx.get("vertical")
		document["lead_group"] = ctx.get("group")
		document["assignee"] = ctx.get("owner_name")
		return document

	def _lead_of(self, doc):
		# The CRM Lead a row hangs off, via the resolvers that already own this decision.
		if doc.doctype == "CRM Lead":
			return doc.name
		if doc.doctype == "File":
			return _file_lead(doc)
		ref = _ref_parent(doc)
		if ref and ref[0] == "CRM Lead":
			return ref[1]
		return None

	def _lead_context(self, lead):
		# Display + scope fields read once per lead and cached for the whole build (17k tasks share ~few leads).
		cache = self.__dict__.setdefault("_ctx_cache", {})
		if lead in cache:
			return cache[lead]
		cache[lead] = ctx = self._read_lead_context(lead)
		return ctx

	def _read_lead_context(self, lead):
		# Fieldnames per the grain brain (_LEAD_AXES); the status pill is the patient-journey stage, else the status.
		row = frappe.db.get_value(
			"CRM Lead", lead,
			["lead_name", "lead_owner", "mobile_no", "custom_vertical", "custom_group", "custom_stage", "status"],
			as_dict=True,
		)
		if not row:
			return None
		owner_name = frappe.db.get_value("User", row.lead_owner, "full_name") if row.lead_owner else None
		return {
			"title": row.lead_name,
			"owner": row.lead_owner,
			"owner_name": owner_name,
			"phone": row.mobile_no,
			"status": (row.custom_stage or "").split("::")[-1] or row.status,
			"vertical": row.custom_vertical,
			"group": row.custom_group,
		}

	def _content_of(self, doc, ctx):
		# The tokenized text: the record's own primary key + business ids + free text, then the owner for name+owner queries.
		dt = doc.doctype
		if dt == "CRM Lead":
			parts = [doc.get("name"), doc.get("mobile_no"), doc.get("custom_alternate_number"), doc.get("custom_patient_id"), doc.get("custom_lsq_prospect_id")]
			parts += [*_phone_tokens(doc.get("mobile_no")), *_phone_tokens(doc.get("custom_alternate_number"))]
		elif dt == "FCRM Note":
			parts = [doc.get("name"), doc.get("title"), doc.get("content")]
		elif dt == "CRM Task":
			parts = [doc.get("name"), doc.get("title"), doc.get("description")]
		elif dt == "CRM Call Log":
			parts = [doc.get("name"), doc.get("from"), doc.get("to")]
		elif dt == "File":
			parts = [doc.get("name"), doc.get("file_name")]
		else:
			parts = []
		parts += [ctx.get("owner_name"), ctx.get("owner")]
		return " ".join(str(p) for p in parts if p)


def build_index():
	# Console/enqueue entrypoint for a full (re)build — mirrors helpdesk's module function.
	CRMLeadSearch().build_index()
