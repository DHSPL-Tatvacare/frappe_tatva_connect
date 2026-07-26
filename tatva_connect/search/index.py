# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Global spotlight search — one SQLiteSearch subclass on Frappe's native FTS5 framework."""
import hashlib
import json
import re
from typing import ClassVar

import frappe
from crm.permissions.org_hierarchy import (
	_team_mem_query,
	get_lead_permission_query_conditions,
	hierarchy_enabled,
)
from frappe import _
from frappe.search.sqlite_search import SQLiteSearch
from frappe.utils import strip_html_tags
from frappe.utils.caching import redis_cache

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


def _leaf(value):
	# A stage PK is composite (`{program}::…::{stage}`); the LEAF is the only part a human types or reads.
	return (value or "").split("::")[-1]

from tatva_connect.access import request_cache
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

# The row of `search_meta` that records which declaration the live index file was actually built from.
_FINGERPRINT_KEY = "schema_fingerprint"

# One permission read per user per minute — a search costs this on EVERY keystroke past the 3-char floor.
_PERMISSION_TTL = 60

# One tier of the doctype preference, wide enough to dominate every other factor in the scoring pipeline.
_TIER_SPREAD = 100

# THE ID RULE, declared ONCE: a unique ID is INPUT — indexed so a punched ID finds its record, never shown in a
# row except in the one dedicated slot, when that ID is what the user typed. This tuple is the whole declaration:
# it fills `keys` (searched), stores the value as returned metadata, and names which ID a typed query matched.
# `exact` is a docname (nobody half-types a hash), `text` also accepts a prefix, `digits` compares phone forms.
_IDENTIFIERS = (
	("lead", "name", "exact"),
	("phone", "mobile_no", "digits"),
	("phone_alt", "custom_alternate_number", "digits"),
	("patient_id", "custom_patient_id", "text"),
	("prospect_id", "custom_lsq_prospect_id", "text"),
)

# The lead's grain axes, in the order the row shows them; fieldnames are the picklist brain's own lead axes.
_AXES = (("vertical", "custom_vertical"), ("lead_group", "custom_group"), ("program", "custom_current_program"))

# Below this a probe prefix-matches half the site, and the endpoint refuses a query shorter than it anyway.
_IDENT_MIN = 3

# The `principals` column is a delimited SET, so a token is bracketed: `|a@x.com|` can never match `|ba@x.com|`.
_D = "|"

# `DocShare.everyone` grants every logged-in user (frappe/share.py get_shared), so it is a principal in its own
# right. Not an email, so it cannot collide with one — and a collision would only WIDEN the pre-filter anyway.
_EVERYONE = "everyone"


def _token(user):
	# One spelling of a principal token, used by the writer (prepare_document) and the reader (the LIKE filter).
	return f"{_D}{user}{_D}"


def _principals_of(lead, lead_owner, creator):
	# Every user id attached to this lead by a ROW-LEVEL mechanism the list engine honours: the lead owner and
	# the creator (`if_owner`), every live assignment (org_hierarchy's ToDo leg), and every share. Deliberately
	# unfiltered by right — this set only PRE-selects rows, and `_visible_rows` is what decides visibility, so
	# being wide here costs a little work and being narrow here would hide a record. (`_` in an email is a LIKE
	# single-char wildcard, which widens the same harmless way.)
	users = {u for u in (lead_owner, creator) if u}
	users.update(
		frappe.get_all(
			"ToDo",
			filters={"reference_type": "CRM Lead", "reference_name": lead, "status": ["!=", "Cancelled"]},
			pluck="allocated_to",
		)
	)
	for share in frappe.get_all(
		"DocShare", filters={"share_doctype": "CRM Lead", "share_name": lead}, fields=["user", "everyone"]
	):
		users.add(_EVERYONE if share.everyone else share.user)
	return _D + _D.join(sorted(u for u in users if u)) + _D if users else ""


@redis_cache(ttl=_PERMISSION_TTL, user=True)
def visible_principals():
	# The caller's own line, as principal tokens — bounded by HEADCOUNT, never by lead count. An empty list
	# means the list engine narrows this caller by nothing, which is NOT the same as "matches nothing".
	# Module-level, not a method: redis_cache keys on the call arguments and `self` is not stably hashable.
	# Deliberate trade: for up to _PERMISSION_TTL a just-granted lead still misses the index pre-filter.
	if not get_lead_permission_query_conditions():
		return []
	user = frappe.session.user
	# _EVERYONE because a share to everyone reaches every logged-in caller; Guest is excluded there and here.
	users = {user} if user == "Guest" else {user, _EVERYONE}
	if hierarchy_enabled():
		# org_hierarchy's OWN subtree query, asked not re-derived; it yields nothing for a user outside the tree.
		users.update(row[0] for row in _team_mem_query(user).run() if row[0])
	return sorted(_token(u) for u in users)


def identifier_labels():
	# Each identifier's label, read off the field's OWN meta; a docname is not a meta field, so it borrows the
	# word Frappe itself puts over that column. One meta read per request, via the app's own memoisation.
	def build():
		meta = frappe.get_meta("CRM Lead")
		field_of = {column: meta.get_field(fieldname) for column, fieldname, _kind in _IDENTIFIERS}
		return {column: (field.label if field else None) or _("ID") for column, field in field_of.items()}

	return request_cache("tatva_connect:search_identifier_labels", "all", build)


def matched_identifier(hit, query):
	"""Which unique ID the caller really typed, if any — an ID is ATOMIC, so this is a plain equality/prefix
	comparison on a short string and never a text matcher. The WHOLE value is returned, so nothing can render
	as a broken fragment. Declaration order settles a tie, so exactly one ID is reported and which one is fixed."""
	probes = [word for word in (query or "").lower().split() if len(word) >= _IDENT_MIN]
	if not probes:
		return None
	labels = identifier_labels()
	for column, _fieldname, kind in _IDENTIFIERS:
		value = hit.get(column)
		if value and _was_typed(str(value), probes, kind):
			return {"column": column, "label": labels[column], "value": str(value)}
	return None


def _was_typed(value, probes, kind):
	# A phone is compared as digits, so a stored `+91…` meets a typed `0…`; a docname only ever matches whole.
	forms = _phone_tokens(value) if kind == "digits" else [value.lower()]
	if kind == "digits":
		probes = [_NON_DIGIT.sub("", probe) for probe in probes]
	return any(probe and (form == probe or (kind != "exact" and form.startswith(probe))) for form in forms for probe in probes)


class CRMLeadSearch(SQLiteSearch):
	INDEX_NAME = "crm_lead_search.db"

	# `keys` is tokenized + searched but never displayed (ids/phones/owner); the snippet only ever shows
	# `content`. Metadata is stored + returned but not tokenized; `lead_group` avoids the reserved word `group`.
	INDEX_SCHEMA: ClassVar[dict] = {
		"text_fields": ["title", "content", "keys"],
		# `principals` is the delimited owner/creator/assignee/share set — a permission column, matched by LIKE.
		# The identifier columns are stored (so `ident` can name the ID that matched) and never tokenized here.
		"metadata_fields": [*(column for column, _f, _k in _IDENTIFIERS), "status", *(column for column, _f in _AXES), "assignee", "principals", "file_url"],
		"tokenizer": "unicode61 remove_diacritics 2 tokenchars '-_@.+'",
	}

	# DECLARATION ORDER IS DISPLAY ORDER — `_doctype_tier` reads it, so the leads -> notes -> attachments preference is written once.
	INDEXABLE_DOCTYPES: ClassVar[dict] = {
		# The IDs and grain axes come off the lead CONTEXT (every child row carries them too), so a lead selects only what `_keys_of` reads off its own row.
		"CRM Lead": {"fields": [*_PLACEHOLDER, "email", "custom_stage", "custom_substage", "source"]},
		"FCRM Note": {"fields": [*_PLACEHOLDER, "title", "content", "reference_doctype", "reference_docname"]},
		# `file_url` is declared so the framework's own metadata mapping stores it — a hit opens the bytes with no per-result read.
		"File": {"fields": [*_PLACEHOLDER, "file_name", "file_url", "attached_to_doctype", "attached_to_name"]},
		# CRM Task is out — 12,567 rows / 2.93 MB the owner does not want in the spotlight. Re-enable by uncommenting; _content_of/_keys_of/TAB cover it, and SearchResults.vue needs its tile back.
		# "CRM Task": {"fields": [*_PLACEHOLDER, "title", "description", "assigned_to", "reference_doctype", "reference_docname"]},
		# Call Log is out — a from/to pair is a phone already indexed on its lead. Re-enable by uncommenting; _content_of/TAB cover it.
		# "CRM Call Log": {"fields": [*_PLACEHOLDER, "from", "to", "reference_doctype", "reference_docname"]},
	}

	def is_search_enabled(self):
		# OFF -> no index file, so every doc-event hook no-ops on index_exists(); that is the migration bulk guard.
		return is_enabled(TOGGLE)

	def build_index(self, batch_size=1000, is_continuation=False):
		# Both native entrypoints — the enqueued build_index and the 3-hourly build_index_if_not_exists — land here.
		super().build_index(batch_size=batch_size, is_continuation=is_continuation)
		# Stamp a FINISHED index only, so a cut-short build is never read back as "already on the current schema".
		if self.index_exists() and self._is_indexing_complete():
			self._stamp_fingerprint()

	def schema_fingerprint(self):
		# The index's shape, derived from the declaration itself: `self.schema` is exactly what _ensure_fts_table
		# creates the FTS columns from, so nothing is hardcoded and no human has to remember to bump a version.
		payload = json.dumps({"schema": self.schema, "doctypes": self.INDEXABLE_DOCTYPES}, sort_keys=True)
		return hashlib.sha256(payload.encode()).hexdigest()

	def stored_fingerprint(self):
		# What the live index file was really built from; None means no index, or one built before the stamp existed.
		if not self.index_exists() or not self._table_exists("search_meta"):
			return None
		rows = self.sql("SELECT value FROM search_meta WHERE key = ?", [_FINGERPRINT_KEY], read_only=True)
		return rows[0]["value"] if rows else None

	def _stamp_fingerprint(self):
		# One tiny table beside the framework's own, created on first stamp; the index file is its only home.
		def write(cursor):
			cursor.execute("CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)")
			cursor.execute(
				"INSERT OR REPLACE INTO search_meta (key, value) VALUES (?, ?)",
				(_FINGERPRINT_KEY, self.schema_fingerprint()),
			)

		self._with_connection(write)

	def search(self, query, title_only=False, filters=None):
		# `total_matches` is counted off the RAW candidate rows, i.e. before _process_search_results drops the
		# ones the caller may not see — reporting it would leak a count of invisible leads. The framework's own
		# post-filter count is the only honest one (capped at MAX_SEARCH_RESULTS, which P4b(e) owns).
		res = super().search(query, title_only=title_only, filters=filters)
		summary = res.get("summary") or {}
		if "filtered_matches" in summary:
			summary["total_matches"] = summary["returned_matches"] = summary["filtered_matches"]
		return res

	def get_scoring_pipeline(self):
		# ONE ranking, written out: bm25, then the framework's title boost, then the declared doctype order.
		# The recency boost is deliberately NOT here — `modified` is not declared, because every row on a
		# migrated site carries the import's timestamp, so the boost would be a constant that reorders nothing
		# while costing a metadata column and a full rebuild. Stated, not left to a silent `if`.
		return [self._get_base_score, self._get_title_boost, self._doctype_tier]

	def _doctype_tier(self, row, query):
		# The owner's order, expressed where ranking lives instead of re-sorting the framework's output.
		# _TIER_SPREAD per tier dominates the pipeline's own ceiling (title 5.0 x base 1.0), so the declared
		# order is strict rather than a tie-break, and a hit is never promoted past a whole doctype.
		order = list(self.doc_configs)
		doctype = row["doctype"] if "doctype" in row.keys() else None
		rank = order.index(doctype) if doctype in order else len(order)
		return float(_TIER_SPREAD ** (len(order) - rank))

	def get_search_filters(self):
		# A PRE-filter, bounded by headcount: the caller's line as `(principals LIKE ? OR ...)`, one bound
		# variable per PRINCIPAL. Never an enumeration of leads — that broke past SQLITE_MAX_VARIABLE_NUMBER,
		# and the framework swallows the error, so the widest-visibility users got zero results. The exemption
		# is the list engine's own (empty condition string), asked of org_hierarchy, not re-derived here.
		# This filter decides NOTHING: _process_search_results below is the authority on visibility.
		tokens = visible_principals()
		return {"principals": ["LIKE", tokens]} if tokens else {}

	def _process_search_results(self, raw_results, query):
		# The authoritative row gate, and the reason the pre-filter above is allowed to be approximate: the
		# candidates are handed to `get_list` — the SAME brain the enumeration used — as ONE bounded question.
		# Bounded by the framework's candidate cap (500), never by lead count. Runs BEFORE the framework's
		# truncation to 100, so scoping costs no recall inside the candidate set.
		return super()._process_search_results(self._visible_rows(raw_results), query)

	def _visible_rows(self, rows):
		# One get_list over the candidate leads. A row with no lead is dropped: nothing is indexed unowned.
		by_lead = {}
		for row in rows:
			lead = row["lead"] if "lead" in row.keys() else None
			if lead:
				by_lead.setdefault(lead, []).append(row)
		if not by_lead:
			return []
		allowed = set(
			frappe.get_list("CRM Lead", filters={"name": ["in", list(by_lead)]}, pluck="name", limit_page_length=0)
		)
		return [row for row in rows if (row["lead"] if "lead" in row.keys() else None) in allowed]

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
		document["content"] = self._content_of(doc)
		document["keys"] = self._keys_of(doc, ctx)
		document["status"] = ctx.get("status")
		document["assignee"] = ctx.get("owner_name")
		document["principals"] = ctx.get("principals")
		# One declaration writes every ID (`lead`, the docname, is one of them and is also the scope column) and every axis.
		document.update(ctx["ids"])
		document.update(ctx["axes"])
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
		# Fieldnames per the two declarations above (_IDENTIFIERS / _AXES, whose axes are the grain brain's own
		# _LEAD_AXES); the status pill is the patient-journey stage, else the status.
		ids = [fieldname for _c, fieldname, _k in _IDENTIFIERS if fieldname != "name"]
		axes = [fieldname for _c, fieldname in _AXES]
		row = frappe.db.get_value(
			"CRM Lead", lead, ["lead_name", "lead_owner", "owner", "custom_stage", "status", *ids, *axes], as_dict=True
		)
		if not row:
			return None
		return {
			"title": row.lead_name,
			"owner": row.lead_owner,
			"owner_name": self._user_name(row.lead_owner),
			"principals": _principals_of(lead, row.lead_owner, row.owner),
			"status": _leaf(row.custom_stage) or row.status,
			# The docname is the lead itself; every other ID and every axis is the value the declaration names.
			"ids": {column: (lead if fieldname == "name" else row.get(fieldname)) for column, fieldname, _k in _IDENTIFIERS},
			"axes": {column: row.get(fieldname) for column, fieldname in _AXES},
		}

	def _content_of(self, doc):
		# The DISPLAYED snippet — clean human text only; ids and the owner are search-only (see _keys_of).
		dt = doc.doctype
		if dt == "FCRM Note":
			text = " ".join(p for p in [doc.get("title"), doc.get("content")] if p)
		elif dt == "CRM Task":
			text = " ".join(p for p in [doc.get("title"), doc.get("description")] if p)
		elif dt == "CRM Call Log":
			text = " ".join(p for p in [doc.get("from"), doc.get("to")] if p)
		elif dt == "File":
			text = doc.get("file_name") or ""
		else:  # CRM Lead — no snippet at all: its row is rendered from metadata, and an ID is never displayed text.
			text = ""
		return strip_html_tags(text).strip() if text else ""

	def _keys_of(self, doc, ctx):
		# Searchable but never shown: every unique ID + its phone digit forms + the owner, so a punched ID or phone
		# resolves to its record. A child row carries its own name and the owner — its lead's IDs are the lead row's.
		parts = [doc.get("name")]
		if doc.doctype == "CRM Lead":
			for column, _fieldname, kind in _IDENTIFIERS:
				parts.append(ctx["ids"].get(column))
				if kind == "digits":
					parts += _phone_tokens(ctx["ids"].get(column))
			# Email is searched and never displayed either; stage/sub-stage/source are closed sets P5 turns into filters.
			parts += [doc.get("email")]
			parts += [_leaf(doc.get("custom_stage")), _leaf(doc.get("custom_substage")), doc.get("source")]
		if doc.doctype == "CRM Task":
			parts += [doc.get("assigned_to"), self._user_name(doc.get("assigned_to"))]
		parts += [ctx.get("owner_name"), ctx.get("owner")]
		return " ".join(str(p) for p in parts if p)

	def _user_name(self, user):
		# One read per distinct user for the whole build — 12.5k tasks share a handful of assignees.
		cache = self.__dict__.setdefault("_user_cache", {})
		if user not in cache:
			cache[user] = frappe.db.get_value("User", user, "full_name") if user else None
		return cache[user]


def build_index():
	# Console/enqueue entrypoint for a full (re)build — mirrors helpdesk's module function.
	CRMLeadSearch().build_index()


def reindex_lead(lead):
	# The `principals` column is denormalised, so anything that moves a lead's owner, assignment or share has to
	# restamp it — on the lead AND on every child row that carries its context. The index itself names those
	# children (`lead` column), so no reverse resolver is re-derived here. Dormant/absent index -> no-op, the
	# same predicate every native doc-event indexing hook uses. Never raises into the caller's save.
	engine = CRMLeadSearch()
	if not (engine.is_search_enabled() and engine.index_exists()):
		return
	rows = engine.sql("SELECT doc_id FROM search_fts WHERE lead = ?", [lead], read_only=True) or []
	targets = {("CRM Lead", lead)}
	for row in rows:
		doctype, _, name = row["doc_id"].partition(":")
		if doctype in engine.doc_configs and name:
			targets.add((doctype, name))
	for doctype, name in sorted(targets):
		try:
			engine.index_doc(doctype, name)
		except Exception:
			frappe.log_error(title="Search Reindex Error", message=f"{doctype}:{name} (lead {lead})")


def reindex_on_lead_owner_change(doc, method=None):
	# CRM Lead.on_update — the owner leg of the visibility predicate.
	if doc.has_value_changed("lead_owner"):
		reindex_lead(doc.name)


def reindex_on_assignment(doc, method=None):
	# ToDo after_insert / on_trash — the assignment leg; a cancelled ToDo grants nothing, hence on_update below.
	if doc.reference_type == "CRM Lead" and doc.reference_name:
		reindex_lead(doc.reference_name)


def reindex_on_assignment_change(doc, method=None):
	# ToDo on_update — only a reallocation or a status move can change who the lead is visible to.
	if doc.has_value_changed("allocated_to") or doc.has_value_changed("status"):
		reindex_on_assignment(doc, method)


def reindex_on_share(doc, method=None):
	# DocShare after_insert / on_update / on_trash — crm shares a lead with its assigned agent (crm_lead.py:189).
	if doc.share_doctype == "CRM Lead" and doc.share_name:
		reindex_lead(doc.share_name)
