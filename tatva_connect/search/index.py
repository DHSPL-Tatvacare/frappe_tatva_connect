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
from frappe.search.sqlite_search import MIN_WORD_LENGTH, SQLiteSearch
from frappe.utils.caching import redis_cache, request_cache

from tatva_connect.access.visibility import _ref_parent
from tatva_connect.api.partner_file import _file_lead
from tatva_connect.automation.settings import is_enabled
from tatva_connect.phone import match_digits
from tatva_connect.taxonomy.picklist import _LEAD_AXES

# The dormant operator toggle that gates the feature — a CRM Tatva Automation row, like every switch.
TOGGLE = "Search::Index::indexing"

# Which lead-detail tab a hit opens; a lead opens the detail root (the frontend navigates via the hash).
TAB = {"CRM Lead": None, "FCRM Note": "notes", "CRM Task": "tasks", "CRM Call Log": "calls", "File": "attachments"}

# The batch build SELECTs these real columns before prepare_document runs; title/content map to always-present
# system columns we overwrite there (row title = patient name, content = composed), so no row is ever skipped.
_PLACEHOLDER = [{"title": "name"}, {"content": "creation"}]

# The row of `search_meta` that records which declaration the live index file was actually built from.
_FINGERPRINT_KEY = "schema_fingerprint"

# One permission read per user per minute — a search costs this on EVERY keystroke past the endpoint's floor.
_PERMISSION_TTL = 60

# One tier of the doctype preference, wide enough to dominate every other factor in the scoring pipeline.
_TIER_SPREAD = 100

# How long SQLite waits on a locked index before giving up; frappe sets none, so its default of 0 raises on the first collision.
_BUSY_TIMEOUT_MS = 500

# The ONE title every index write failure is logged under, so a single Error Log notification rule catches them all.
_WRITE_ERROR = "Search Index Error"

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

# The lead's grain axes, in the order the row shows them; the FIELDNAMES are the picklist brain's own lead axes,
# imported not restated (taxonomy/picklist.py:37) — `strict` is what makes a drift in either list a crash, not a bug.
_AXES = tuple(zip(("vertical", "lead_group", "program"), _LEAD_AXES, strict=True))

# Every CRM Lead field prepare_document consumes, derived from the two declarations above so it cannot drift.
# This is ONE list doing TWO jobs, which is the whole point: the framework SELECTs it for the batch build AND
# re-reads it on every save to decide whether to reindex (`any(doc.has_value_changed(f) for f in fields)`,
# sqlite_search.py:1846). A field read out-of-band is invisible to that check — which is why correcting a
# patient's phone or name used to leave the old value searchable until someone forced a full rebuild.
_LEAD_FIELDS = (
	"lead_name",
	"lead_owner",
	"owner",
	"custom_stage",
	"status",
	*(fieldname for _c, fieldname, _k in _IDENTIFIERS if fieldname != "name"),
	*(fieldname for _c, fieldname in _AXES),
)

# The framework's own word floor (sqlite_search.py:58), which is also `api._MIN`: below it a term gets no prefix
# wildcard, so a shorter probe is not something the text lane would have searched either.
_IDENT_MIN = MIN_WORD_LENGTH

# The `principals` column is a delimited SET, so a token is bracketed: `|a@x.com|` can never match `|ba@x.com|`.
_D = "|"

# `DocShare.everyone` grants every logged-in user (frappe/share.py get_shared), so it is a principal in its own
# right. Not an email, so it cannot collide with one — and a collision would only WIDEN the pre-filter anyway.
_EVERYONE = "everyone"

# Anything that is not a word character or space is a token boundary — one rule, used by every lane below.
_PUNCT = re.compile(r"[^\w\s]")


def tokens(text):
	"""THE query tokeniser — ONE per typed word, so the two lanes can never cut a query differently.

	Each pair is `(normalised, as_typed)`: the normalised form is lower-cased with punctuation as a space (a
	stored "Goodflip-Care" must meet a typed "goodflip care"), the second is the user's own spelling, kept so a
	word can be shown back as it was written. One pair per WORD is what makes the two forms un-desyncable —
	the vocabulary lane used to align two separate tokenisations and carried a fallback for when they disagreed.
	"""
	pairs = [(" ".join(_PUNCT.sub(" ", word).lower().split()), word) for word in (text or "").split()]
	return [pair for pair in pairs if pair[0]]


def normalise(text):
	# The same words `tokens` yields, as one string — how a stored value and a typed query meet on one rule.
	return " ".join(word for word, _typed in tokens(text))


def leaf(value):
	# A stage PK is composite (`{program}::…::{stage}`); the LEAF is the only part a human types or reads.
	return (value or "").split("::")[-1]


def _phone_keys(num):
	# Indexed as its full digits AND as the digits brain's ten-digit key, so a stored `+91…` is found whether or
	# not the country code was typed. Both spellings come from `phone.match_digits` — never a local regex.
	return [key for key in {match_digits(num), match_digits(num, last=10)} if key]


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


@request_cache
def identifier_labels():
	# Each identifier's label, read off the field's OWN meta; a docname is not a meta field, so it borrows the
	# word Frappe itself puts over that column. One meta read per request — frappe's own decorator, no dummy key.
	meta = frappe.get_meta("CRM Lead")
	field_of = {column: meta.get_field(fieldname) for column, fieldname, _kind in _IDENTIFIERS}
	return {column: (field.label if field else None) or _("ID") for column, field in field_of.items()}


def matched_identifier(hit, query):
	"""Which unique ID the caller really typed, if any — an ID is ATOMIC, so this is a plain equality/prefix
	comparison on a short string and never a text matcher. The WHOLE value is returned, so nothing can render
	as a broken fragment. Declaration order settles a tie, so exactly one ID is reported and which one is fixed."""
	probes = [word for word, _typed in tokens(query) if len(word) >= _IDENT_MIN]
	if not probes:
		return None
	labels = identifier_labels()
	for column, _fieldname, kind in _IDENTIFIERS:
		value = hit.get(column)
		if value and _was_typed(str(value), probes, kind):
			return {"column": column, "label": labels[column], "value": str(value)}
	return None


def _was_typed(value, probes, kind):
	# A phone is compared on the digits brain's OWN ten-digit key, so a stored `+91…` meets a typed `0…` — and a
	# shorter number is not a key at all (phone.py:43), so a bare `919` marks nothing rather than every +91 lead.
	# Every other ID goes through the query's own tokeniser, so both sides are cut once and the same way.
	if kind == "digits":
		key = match_digits(value, last=10)
		return bool(key) and any(match_digits(probe, last=10) == key for probe in probes)
	form = normalise(value)
	return any(form == probe or (kind != "exact" and form.startswith(probe)) for probe in probes)


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
		# `_LEAD_FIELDS` is every field the context read consumes, so a change to any of them reindexes the lead.
		"CRM Lead": {"fields": [*_PLACEHOLDER, "email", "custom_substage", "source", *_LEAD_FIELDS]},
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
		# Read once per ENGINE, because `_status` and the framework's own `search()` (sqlite_search.py:245) both ask
		# and one search builds one engine. Scoped to the instance and no wider: `automation.settings.is_enabled` is
		# deliberately uncached so a flipped switch takes effect at once, and the next search reads it again.
		if self.__dict__.get("_enabled") is None:
			self.__dict__["_enabled"] = is_enabled(TOGGLE)
		return self.__dict__["_enabled"]

	def _set_pragmas(self, cursor, is_read=False):
		# WAL admits ONE writer and the framework sets no busy timeout, so SQLite's default of 0 raises `database is locked` on the first collision rather than waiting the millisecond the other write takes.
		super()._set_pragmas(cursor, is_read)
		cursor.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS};")

	def index_doc(self, doctype, docname):
		# Runs INLINE in the caller's save (`update_doc_index` is a `*` on_update event, wrapped in nothing), so a failed write leaves a stale row and an Error Log entry instead of costing a rep their work; `_visible_rows` gates every hit through get_list, so a stale row can never be shown.
		try:
			super().index_doc(doctype, docname)
		except Exception:
			frappe.log_error(title=_WRITE_ERROR, message=f"index {doctype}:{docname}\n\n{frappe.get_traceback()}")

	def remove_doc(self, doctype, docname):
		# The same rule on the delete leg (`delete_doc_index`, a `*` on_trash event): a deletion the user asked for is never refused because the index would not take it.
		try:
			super().remove_doc(doctype, docname)
		except Exception:
			frappe.log_error(title=_WRITE_ERROR, message=f"remove {doctype}:{docname}\n\n{frappe.get_traceback()}")

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

	# Registered through the framework's own discovery hook (sqlite_search.py:94), so bm25 + the title boost stay
	# the base's to define and a scoring function frappe adds later is not silently dropped by a hand-copied list.
	# Recency stays off because `modified` is not a declared metadata field — the base gates it on exactly that
	# (sqlite_search.py:973), and every row on a migrated site carries the import's timestamp anyway.
	@SQLiteSearch.scoring_function
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
		"""The candidates the caller may actually read — asked of frappe's permission engine, TWICE over.

		`get_search_filters` returns ONE dict applied to every row whatever its doctype, so the framework's
		own seam cannot ask a note about note permissions. Both gates below are `get_list`, which IS that
		engine (DocPerm, permission_query_conditions, has_permission hooks, User Permissions, shares):

		  · by LEAD — a sub-entity is visible only if the lead it hangs off is on the caller's line.
		  · by the row's OWN doctype — a lead grant is not a note grant. Without this a caller who reaches
		    a lead by share or assignment, but holds no FCRM Note read, was served clinical note text.

		One bounded question per doctype present in the page (<= 3), never an enumeration. Runs BEFORE the
		framework truncates to MAX_SEARCH_RESULTS, so scoping costs no recall inside the candidate set. It is
		also what makes the index safe to be stale: a deleted or detached row cannot survive `get_list`.
		"""
		def lead_of(row):
			return row["lead"] if "lead" in row.keys() else None

		candidates = {}
		for row in rows:
			if lead_of(row):
				candidates.setdefault(row["doctype"], set()).add(row["name"])
		if not candidates:
			return []

		leads = {lead_of(row) for row in rows if lead_of(row)}
		allowed_leads = set(
			frappe.get_list("CRM Lead", filters={"name": ["in", list(leads)]}, pluck="name", limit_page_length=0)
		)
		allowed = {
			(doctype, name)
			for doctype, names in candidates.items()
			for name in self._readable(doctype, names)
		}
		return [
			row
			for row in rows
			if lead_of(row) in allowed_leads and (row["doctype"], row["name"]) in allowed
		]

	def _readable(self, doctype, names):
		# The caller's own read scope for ONE doctype. A doctype they hold no read on raises rather than
		# returning empty, and a caller who may read nothing is the same answer either way: no rows.
		try:
			return set(
				frappe.get_list(doctype, filters={"name": ["in", list(names)]}, pluck="name", limit_page_length=0)
			)
		except frappe.PermissionError:
			return set()

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
		# The SAME list the doctype declares, so the fields that trigger a reindex are exactly the fields read.
		row = frappe.db.get_value("CRM Lead", lead, list(_LEAD_FIELDS), as_dict=True)
		if not row:
			return None
		return {
			"title": row.lead_name,
			"owner": row.lead_owner,
			"owner_name": self._user_name(row.lead_owner),
			"principals": _principals_of(lead, row.lead_owner, row.owner),
			"status": leaf(row.custom_stage) or row.status,
			# The docname is the lead itself; every other ID and every axis is the value the declaration names.
			"ids": {column: (lead if fieldname == "name" else row.get(fieldname)) for column, fieldname, _k in _IDENTIFIERS},
			"axes": {column: row.get(fieldname) for column, fieldname in _AXES},
		}

	def _content_of(self, doc):
		# The DISPLAYED snippet — clean human text only; ids and the owner are search-only (see _keys_of).
		# Rich text goes through the framework's own pipeline (sqlite_search.py:1569), which puts a space between
		# blocks; `strip_html_tags` is one regex and indexed `<p>dose</p><p>Patient</p>` as `dosePatient`. A file
		# name and a phone pair are already plain, and running an HTML parser over them only makes bs4 warn.
		dt = doc.doctype
		if dt == "FCRM Note":
			return self._process_content(" ".join(p for p in [doc.get("title"), doc.get("content")] if p))
		if dt == "CRM Task":
			return self._process_content(" ".join(p for p in [doc.get("title"), doc.get("description")] if p))
		if dt == "CRM Call Log":
			return " ".join(p for p in [doc.get("from"), doc.get("to")] if p)
		if dt == "File":
			return doc.get("file_name") or ""
		# CRM Lead — no snippet at all: its row is rendered from metadata, and an ID is never displayed text.
		return ""

	def _keys_of(self, doc, ctx):
		# Searchable but never shown: every unique ID + its phone digit forms + the owner, so a punched ID or phone
		# resolves to its record. A child row carries its own name and the owner — its lead's IDs are the lead row's.
		parts = [doc.get("name")]
		if doc.doctype == "CRM Lead":
			for column, _fieldname, kind in _IDENTIFIERS:
				parts.append(ctx["ids"].get(column))
				if kind == "digits":
					parts += _phone_keys(ctx["ids"].get(column))
			# Email is searched and never displayed either; stage/sub-stage/source are closed sets P5 turns into filters.
			parts += [doc.get("email")]
			parts += [leaf(doc.get("custom_stage")), leaf(doc.get("custom_substage")), doc.get("source")]
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
	"""Restamp the lead's row and every child row that carries a copy of its context.

	Two things frappe's own incremental indexer cannot do, which is exactly why this exists and no more:

	  · `principals` is derived from OTHER documents — a ToDo or a DocShare. Sharing a lead never touches the
	    lead, so `update_doc_index` is never called for it; only the ToDo/DocShare event fires, and it has to
	    restamp the lead itself.
	  · the children denormalise the lead's title, grain and principals, and there is no parent -> child
	    cascade for an index. The `lead` column names them, so no reverse resolver is re-derived here.

	A save of the lead DOCUMENT needs none of this — every field the context reads is declared, so the
	framework reindexes that row on its own. Runs in a background job; see `_enqueue_reindex`.
	"""
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
		# No try/except: `index_doc` owns every write failure and logs it, so a second handler here would be unreachable.
		engine.index_doc(doctype, name)


def _enqueue_reindex(lead):
	# ONE job per lead, off the request path, with the three guards that stop a runaway: `deduplicate` + a
	# per-lead `job_id` collapse the several triggers one assignment fires (ToDo insert AND update, DocShare
	# insert AND update, the lead's own save) into a single run; `enqueue_after_commit` means a rolled-back
	# save never reindexes; and a bulk import or a migrate enqueues nothing at all.
	if not lead or frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	frappe.enqueue(
		"tatva_connect.search.index.reindex_lead",
		queue="short",
		lead=lead,
		enqueue_after_commit=True,
		deduplicate=True,
		job_id=f"search-reindex-{lead}",
	)


def reindex_on_lead_context_change(doc, method=None):
	# CRM Lead.on_update — the children carry the lead's title, grain and principals, so any declared field
	# moving restamps them. The lead's own row needs nothing here; the framework already reindexed it.
	if any(doc.has_value_changed(fieldname) for fieldname in _LEAD_FIELDS):
		_enqueue_reindex(doc.name)


def reindex_on_assignment(doc, method=None):
	# ToDo after_insert / on_trash — the assignment leg; a cancelled ToDo grants nothing, hence on_update below.
	if doc.reference_type == "CRM Lead" and doc.reference_name:
		_enqueue_reindex(doc.reference_name)


def reindex_on_assignment_change(doc, method=None):
	# ToDo on_update — only a reallocation or a status move can change who the lead is visible to.
	if doc.has_value_changed("allocated_to") or doc.has_value_changed("status"):
		reindex_on_assignment(doc, method)


def reindex_on_share(doc, method=None):
	# DocShare after_insert / on_update / on_trash — crm shares a lead with its assigned agent (crm_lead.py:189).
	if doc.share_doctype == "CRM Lead" and doc.share_name:
		_enqueue_reindex(doc.share_name)
