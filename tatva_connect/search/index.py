# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Global spotlight search — one SQLiteSearch subclass plugged into Frappe's native FTS5 framework.

We own nothing about the index engine: registration (`sqlite_search` hook), real-time doc updates
(`update_doc_index`/`delete_doc_index` on `doc_events["*"]`), the resumable full build, the 3-hourly
missing-index self-heal and the after_migrate kickoff are all inherited from
`frappe/search/sqlite_search.py`. We override exactly two methods — `is_search_enabled` (the dormant
flag) and `get_search_filters` (grain scoping through the ONE brain) — plus `prepare_document` to stamp
each row's parent lead and its grain, the way helpdesk stamps `reference_ticket`.

One row per record (`doc_id = doctype:name`), so a record never appears twice. Nothing clinical, no file
bytes, no Comment, no LSQ ids are indexed — there is no value in the index a snippet could leak.
"""
import frappe
from frappe.search.sqlite_search import SQLiteSearch

from tatva_connect.access import entitlement
from tatva_connect.access.visibility import _ref_parent
from tatva_connect.api.partner_file import _file_lead
from tatva_connect.automation.settings import is_enabled

# The dormant operator toggle that gates the whole feature — a CRM Tatva Automation row, like every switch.
TOGGLE = "Search::Index::indexing"

# Which lead-detail tab a hit opens (frontend navigates via the URL hash). A lead opens the detail root.
TAB = {"CRM Lead": None, "FCRM Note": "notes", "CRM Task": "tasks", "CRM Call Log": "calls", "File": "attachments"}

# Every doctype maps title/content to the synthetic attrs we compute in prepare_document, so base
# validation always passes and ONE code path composes all five. `modified` gives recency boosting.
_FIELDS = ["name", {"title": "search_title"}, {"content": "search_content"}, "modified"]


class CRMLeadSearch(SQLiteSearch):
	INDEX_NAME = "crm_lead_search.db"

	# Metadata columns are UNINDEXED — filterable + returned, not tokenized. `phone`/`title` drive the row
	# UI with zero per-hit lookups; vertical/group/program scope; lead is the click-through target.
	INDEX_SCHEMA = {
		"metadata_fields": ["lead", "phone", "vertical", "group", "program", "owner"],
		"tokenizer": "unicode61 remove_diacritics 2 tokenchars '-_@.+'",
	}

	INDEXABLE_DOCTYPES = {
		"CRM Lead": {"fields": _FIELDS},
		"FCRM Note": {"fields": _FIELDS},
		"CRM Task": {"fields": _FIELDS},
		"CRM Call Log": {"fields": _FIELDS},
		"File": {"fields": _FIELDS},
	}

	def is_search_enabled(self):
		"""The one switch, read from the automation registry. OFF -> no index file, so every doc-event
		hook no-ops on `index_exists()`; that is the bulk guard through the LSQ migration. Flipping the
		toggle ON post-migration fires the activator, which kicks the native build."""
		return is_enabled(TOGGLE)

	def get_search_filters(self):
		"""Scope to the leads the caller may actually SEE — the same visibility the lead list uses.

		Lead visibility is org-hierarchy, not grain (grain governs field visibility, a different axis), so
		we run the real permission engine via get_list and filter the index by those lead names. Every row
		carries `lead`, so one IN-clause scopes children too. System Manager (ALL_GRAINS) is exempt and
		sees everything; everyone else is bounded to their org subtree. limit_page_length=0 = no 20-row cap.
		"""
		if entitlement.entitled_grains() == entitlement.ALL_GRAINS:
			return {}
		accessible = frappe.get_list("CRM Lead", pluck="name", limit_page_length=0)
		return {"lead": accessible}

	def prepare_document(self, doc):
		"""Compose the searchable text and stamp the parent lead + its grain onto every row.

		Child records (note/task/call/file) carry their PARENT LEAD's name as title and its grain as
		metadata — resolved through the existing `_ref_parent`/`_file_lead`, never re-derived. A record
		whose parent is not a CRM Lead is skipped (the framework tolerates None)."""
		lead = self._lead_of(doc)
		if not lead:
			return None
		ctx = self._lead_context(lead)
		if not ctx:
			return None

		doc.search_title = ctx["title"] or lead
		doc.search_content = self._content_of(doc, ctx)

		document = super().prepare_document(doc)
		if not document:
			return None

		document["lead"] = lead
		document["phone"] = ctx["phone"]
		document["vertical"] = ctx["vertical"]
		document["group"] = ctx["group"]
		document["program"] = ctx["program"]
		document["owner"] = ctx["owner"]
		return document

	def _lead_of(self, doc):
		"""The CRM Lead a row hangs off, via the resolvers that already own this decision."""
		if doc.doctype == "CRM Lead":
			return doc.name
		if doc.doctype == "File":
			return _file_lead(doc)
		ref = _ref_parent(doc)
		if ref and ref[0] == "CRM Lead":
			return ref[1]
		return None

	def _lead_context(self, lead):
		"""Display + scope fields read once per row from the parent lead. Fieldnames per the grain brain
		(`taxonomy/picklist.py:_LEAD_AXES`)."""
		row = frappe.db.get_value(
			"CRM Lead",
			lead,
			["lead_name", "lead_owner", "mobile_no", "custom_vertical", "custom_group", "custom_current_program"],
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
			"vertical": row.custom_vertical,
			"group": row.custom_group,
			"program": row.custom_current_program,
		}

	def _content_of(self, doc, ctx):
		"""The tokenized text for a row. `from` is a reserved word — read it with .get(). Owner name +
		id are appended so a "patient-name owner-name" query matches (FTS ANDs terms across the row)."""
		dt = doc.doctype
		if dt == "CRM Lead":
			parts = [doc.mobile_no, doc.get("custom_alternate_number"), doc.name, doc.get("custom_patient_id"), doc.get("custom_external_id")]
		elif dt == "FCRM Note":
			parts = [doc.title, doc.content, doc.get("custom_external_id")]
		elif dt == "CRM Task":
			parts = [doc.title, doc.description, doc.get("custom_external_id")]
		elif dt == "CRM Call Log":
			parts = [doc.get("from"), doc.get("to"), doc.get("custom_external_id")]
		elif dt == "File":
			parts = [doc.file_name, doc.get("custom_external_id")]
		else:
			parts = []
		parts += [ctx.get("owner_name"), ctx.get("owner")]
		return " ".join(str(p) for p in parts if p)


def build_index():
	"""Console/enqueue entrypoint for a full (re)build — mirrors helpdesk's module function."""
	CRMLeadSearch().build_index()
