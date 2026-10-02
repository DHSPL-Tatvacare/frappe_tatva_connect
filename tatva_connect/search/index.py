# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Global spotlight search — one SQLiteSearch subclass on Frappe's native FTS5 framework."""
import hashlib
import json
import re
import sqlite3
from typing import ClassVar

import frappe
from frappe import _
from frappe.search.sqlite_search import MIN_WORD_LENGTH, SQLiteSearch, SQLiteSearchIndexMissingError
from frappe.utils import now
from frappe.utils.caching import request_cache

from tatva_connect.access.visibility import parent_of
from tatva_connect.api.partner_file import _file_lead
from tatva_connect.automation.settings import is_enabled
from tatva_connect.phone import match_digits
from tatva_connect.propagate import fail_safe
from tatva_connect.taxonomy import labels

# The dormant operator toggle that gates the feature — a CRM Tatva Automation row, like every switch.
TOGGLE = "Search::Index::indexing"

# Which lead-detail tab a hit opens; a lead or a deal opens its own record root.
TAB = {"CRM Lead": None, "CRM Deal": None, "File": "attachments"}

# Real columns the batch build selects; title and content are overwritten in prepare_document.
_PLACEHOLDER = [{"title": "name"}, {"content": "creation"}]

# The search_meta row that records which declaration the live index was built from.
_FINGERPRINT_KEY = "schema_fingerprint"

# One doctype tier outweighs every other scoring factor, so the declared order is strict.
_TIER_SPREAD = 100

# How many leads one `IN (...)` lookup binds — far under SQLite's variable limit.
_IN_CHUNK = 500

# How long a write waits on a locked index; SQLite's default of 0 fails on the first collision.
_BUSY_TIMEOUT_MS = 500

# Error Log titles — faults only; routine sweep results go to the `search` file log.
_WRITE_ERROR = "search: index write failed"
_REPAIR_ERROR = "search: unreadable index dropped"

# Rows one reconcile pass repairs per doctype, each way; a site that is far behind catches up over several passes.
_RECONCILE_BATCH = 2000

# The search_meta key prefix for each doctype's reconcile watermark.
_WATERMARK_KEY = "reconciled_upto"

# Catches a half-written file at a quarter of integrity_check's cost; only the hourly sweep runs it.
_HEALTH_PRAGMA = "PRAGMA quick_check"

# Unique IDs: searched through `keys`, stored as metadata, shown only when typed. `exact` = docname, `digits` = phone.
IDENTIFIERS = (
	("lead", "name", "exact"),
	("phone", "mobile_no", "digits"),
)

# The lead's grain axes; static because the sqlite schema is read at import, locked to the meta by test_indexed_columns.
_AXES = (("vertical", "custom_vertical"), ("lead_group", "custom_group"), ("program", "custom_current_program"))

# Every lead field the index reads — frappe also reindexes the lead when any of these changes.
_LEAD_FIELDS = (
	"lead_name",
	"custom_stage",
	"custom_substage",
	*(fieldname for _c, fieldname, _k in IDENTIFIERS if fieldname != "name"),
	*(fieldname for _c, fieldname in _AXES),
)

# The framework's own word floor; a shorter probe never gets a prefix match.
_IDENT_MIN = MIN_WORD_LENGTH

# Anything that is not a word character or space is a token boundary.
_PUNCT = re.compile(r"[^\w\s]")


def tokens(text):
	"""The one query tokeniser: a `(normalised, as_typed)` pair per typed word."""
	pairs = [(" ".join(_PUNCT.sub(" ", word).lower().split()), word) for word in (text or "").split()]
	return [pair for pair in pairs if pair[0]]


def normalise(text):
	# The words `tokens` yields, as one string, so stored values and typed queries meet on one rule.
	return " ".join(word for word, _typed in tokens(text))


def leaf(value):
	# A stage PK is composite (`{program}::…::{stage}`); the leaf is the part a human types.
	return (value or "").split("::")[-1]


def _url_key(url):
	"""The last segment of a file url — the blob key, without the shared route."""
	return str(url or "").strip().rsplit("/", 1)[-1]


def _words(text):
	"""The whole text plus its alphanumeric parts, so `ai-evals-faq.pdf` is also found by `faq`."""
	value = str(text or "").strip()
	if not value:
		return ""
	parts = [p for p in re.split(r"[^0-9A-Za-z]+", value) if p]
	return " ".join(dict.fromkeys([value, *parts]))


def _rowid(doc_id):
	# A stable integer rowid derived from the doc_id, so a write or delete never scans the file.
	return int.from_bytes(hashlib.blake2b(doc_id.encode(), digest_size=8).digest(), "big") >> 1


def _phone_keys(num):
	# Full digits and the ten-digit key, so a number is found with or without its country code.
	return [key for key in {match_digits(num), match_digits(num, last=10)} if key]


@request_cache
def identifier_labels():
	# Each identifier's label from the field's own meta; the docname borrows frappe's word "ID".
	meta = frappe.get_meta("CRM Lead")
	field_of = {column: meta.get_field(fieldname) for column, fieldname, _kind in IDENTIFIERS}
	return {column: (field.label if field else None) or _("ID") for column, field in field_of.items()}


def matched_identifier(hit, query):
	"""The one unique ID the caller typed, if any, returned whole; declaration order breaks a tie."""
	probes = [word for word, _typed in tokens(query) if len(word) >= _IDENT_MIN]
	if not probes:
		return None
	labels = identifier_labels()
	for column, _fieldname, kind in IDENTIFIERS:
		value = hit.get(column)
		if value and _was_typed(str(value), probes, kind):
			return {"column": column, "label": labels[column], "value": str(value)}
	return None


def _was_typed(value, probes, kind):
	# A phone compares on its ten-digit key; every other ID compares on the query tokeniser's form.
	if kind == "digits":
		key = match_digits(value, last=10)
		return bool(key) and any(match_digits(probe, last=10) == key for probe in probes)
	form = normalise(value)
	return any(form == probe or (kind != "exact" and form.startswith(probe)) for probe in probes)


class CRMLeadSearch(SQLiteSearch):
	INDEX_NAME = "crm_lead_search.db"

	# `keys` is searched but never shown; metadata is stored and returned but never searched.
	INDEX_SCHEMA: ClassVar[dict] = {
		"text_fields": ["title", "content", "keys"],
		"metadata_fields": [
			*(column for column, _f, _k in IDENTIFIERS),
			"stage",
			"stage_color",
			*(column for column, _f in _AXES),
			"file_url",
			"lead_name",
		],
		"tokenizer": "unicode61 remove_diacritics 2 tokenchars '-_@.+'",
	}

	# Declaration order is display order — `_doctype_tier` ranks by it.
	INDEXABLE_DOCTYPES: ClassVar[dict] = {
		"CRM Lead": {"fields": [*_PLACEHOLDER, *_LEAD_FIELDS]},
		# A deal is the same patient: title, grain and stage come off `deal.lead`.
		"CRM Deal": {"fields": [*_PLACEHOLDER, "lead", "organization", "status"]},
		"File": {"fields": [*_PLACEHOLDER, "file_name", "file_url", "attached_to_doctype", "attached_to_name"]},
	}

	def is_search_enabled(self):
		# Read once per engine; off means no index, so every doc-event hook no-ops.
		if self.__dict__.get("_enabled") is None:
			self.__dict__["_enabled"] = is_enabled(TOGGLE)
		return self.__dict__["_enabled"]

	def _set_pragmas(self, cursor, is_read=False):
		# WAL allows one writer; a busy timeout makes a second writer wait instead of failing.
		super()._set_pragmas(cursor, is_read)
		cursor.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS};")

	def _index_documents(self, documents):
		# One REPLACE by rowid; frappe's DELETE by `doc_id` scans the whole file while holding the writer.
		if not documents:
			return
		text_fields = self.schema["text_fields"]
		columns = ["doc_id", *text_fields, *self.schema["metadata_fields"]]
		column_sql = ",".join(columns)
		placeholders = ",".join("?" * (len(columns) + 1))
		replace_sql = f"INSERT OR REPLACE INTO search_fts (rowid, {column_sql}) VALUES ({placeholders})"
		rows = []
		for doc in documents:
			if not doc.get("doctype") or not doc.get("name"):
				self._warn_invalid_document(doc, "missing doctype/name")
				continue
			missing = [field for field in text_fields if doc.get(field) is None]
			if missing:
				self._warn_missing_text_fields(doc["doctype"], doc["name"], missing)
				continue
			doc_id = doc.get("id") or f"{doc['doctype']}:{doc['name']}"
			rows.append((_rowid(doc_id), doc_id, *(doc.get(field, "") for field in columns[1:])))
		if rows:
			self._with_connection(lambda cursor: cursor.executemany(replace_sql, rows))

	def index_doc(self, doctype, docname):
		# Runs inside the caller's save, so a failed write is logged, never raised.
		try:
			super().index_doc(doctype, docname)
		except frappe.DoesNotExistError:
			# The document is gone, so its row is junk: drop it.
			self.remove_doc(doctype, docname)
		except Exception:
			frappe.log_error(_WRITE_ERROR, reference_doctype=doctype, reference_name=docname)

	def remove_doc(self, doctype, docname):
		# A delete the user asked for is never refused because the index failed; by rowid, as above.
		try:
			self.raise_if_not_indexed()
			self.sql("DELETE FROM search_fts WHERE rowid = ?", (_rowid(f"{doctype}:{docname}"),), commit=True)
		except Exception:
			frappe.log_error(_WRITE_ERROR, reference_doctype=doctype, reference_name=docname)

	def rows_of_leads(self, leads):
		# Every indexed row under these leads, one scan per chunk instead of one per lead.
		rows = []
		for start in range(0, len(leads), _IN_CHUNK):
			chunk = leads[start : start + _IN_CHUNK]
			placeholders = ",".join("?" for _ in chunk)
			rows += self.sql(  # sqli-ok: only the `?` list is interpolated; every lead id is a bound parameter
				f"SELECT doc_id FROM search_fts WHERE lead IN ({placeholders})", chunk, read_only=True
			) or []
		return rows

	def index_is_readable(self):
		"""Whether the index file can still be read — a damaged file passes every framework check.
		Asked only by the hourly sweep, never on a save."""
		if not self.index_exists():
			return True  # a missing index is the state frappe already repairs
		try:
			return (self.sql(_HEALTH_PRAGMA, read_only=True) or [["ok"]])[0][0] == "ok"
		except (sqlite3.Error, SQLiteSearchIndexMissingError):  # frappe raises a failed connect as the latter
			return False

	def build_index(self, batch_size=1000, is_continuation=False):
		# Both native build entry points land here; only a finished build is stamped.
		super().build_index(batch_size=batch_size, is_continuation=is_continuation)
		if self.index_exists() and self._is_indexing_complete():
			self._stamp_fingerprint()

	def schema_fingerprint(self):
		# A hash of the declaration itself, so any schema edit is detected without a version number.
		payload = json.dumps({"schema": self.schema, "doctypes": self.INDEXABLE_DOCTYPES}, sort_keys=True)
		return hashlib.sha256(payload.encode()).hexdigest()

	def _meta_read(self, key):
		# None when there is no index or nothing recorded yet.
		if not self.index_exists() or not self._table_exists("search_meta"):
			return None
		rows = self.sql("SELECT value FROM search_meta WHERE key = ?", [key], read_only=True)
		return rows[0]["value"] if rows else None

	def _meta_write(self, key, value):
		# One small key-value table inside the index file, created on first write.
		def write(cursor):
			cursor.execute("CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)")
			cursor.execute("INSERT OR REPLACE INTO search_meta (key, value) VALUES (?, ?)", (key, value))

		self._with_connection(write)

	def stored_fingerprint(self):
		return self._meta_read(_FINGERPRINT_KEY)

	def _stamp_fingerprint(self):
		self._meta_write(_FINGERPRINT_KEY, self.schema_fingerprint())

	def reconcile(self):
		"""Restamp rows changed since the last pass and drop rows whose document is gone.
		Bounded per doctype; returns (rows that were missing, rows removed)."""
		reindexed = removed = 0
		indexed = {row["doc_id"] for row in self.sql("SELECT doc_id FROM search_fts", read_only=True) or []}
		for doctype in self.doc_configs:
			key = f"{_WATERMARK_KEY}::{doctype}"
			watermark = self._meta_read(key)
			if not watermark:
				# A fresh index has nothing to catch up on; record the floor.
				self._meta_write(key, now())
			else:
				changed = frappe.get_all(
					doctype,
					filters={"modified": [">=", watermark]},
					fields=["name", "modified"],
					order_by="modified asc",
					limit=_RECONCILE_BATCH,
				)
				for row in changed:
					self.index_doc(doctype, row.name)
				if changed:
					self._meta_write(key, str(changed[-1].modified))
					# Count only rows that were missing, so the number signals drift, not edit volume.
					reindexed += sum(1 for row in changed if f"{doctype}:{row.name}" not in indexed)
			prefix = f"{doctype}:"
			live = set(frappe.get_all(doctype, pluck="name", limit_page_length=0))
			named = (doc_id[len(prefix) :] for doc_id in indexed if doc_id.startswith(prefix))
			for name in [name for name in named if name not in live][:_RECONCILE_BATCH]:
				self.remove_doc(doctype, name)
				removed += 1
		return reindexed, removed

	def search(self, query, title_only=False, filters=None):
		# Report the count after the permission gate, never the raw count, which would leak hidden leads.
		res = super().search(query, title_only=title_only, filters=filters)
		summary = res.get("summary") or {}
		if "filtered_matches" in summary:
			summary["total_matches"] = summary["returned_matches"] = summary["filtered_matches"]
		return res

	@SQLiteSearch.scoring_function
	def _doctype_tier(self, row, query):
		# Leads above deals above files, strictly, on top of the framework's own scoring.
		order = list(self.doc_configs)
		doctype = row["doctype"] if "doctype" in row.keys() else None
		rank = order.index(doctype) if doctype in order else len(order)
		return float(_TIER_SPREAD ** (len(order) - rank))

	def get_search_filters(self):
		# No pre-filter: who may see a lead is never stored in the index; `_visible_rows` asks get_list.
		return {}

	def _process_search_results(self, raw_results, query):
		# The permission gate runs on the candidates before the framework trims them to its result cap.
		return super()._process_search_results(self._visible_rows(raw_results), query)

	def _visible_rows(self, rows):
		"""The candidates the caller may read: their lead AND the row itself must pass get_list.
		One query per doctype in the page, so a stale or detached row can never be shown."""
		def lead_of(row):
			return row["lead"] if "lead" in row.keys() else None

		candidates = {}
		for row in rows:
			if lead_of(row):
				candidates.setdefault(row["doctype"], set()).add(row["name"])
		if not candidates:
			return []

		leads = {lead_of(row) for row in rows if lead_of(row)}
		allowed_leads = self._readable("CRM Lead", leads)
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
		# The caller's read scope for one doctype; no read permission means no rows.
		try:
			return set(
				frappe.get_list(doctype, filters={"name": ["in", list(names)]}, pluck="name", limit_page_length=0)
			)
		except frappe.PermissionError:
			return set()

	def get_documents_paginated(self, doctype, limit=1000, last_indexed_modified=None, last_indexed_name=None):
		"""Skip pages where no row has a lead — frappe's builder never advances past such a page.
		An empty list still means the doctype is done."""
		while True:
			docs = super().get_documents_paginated(doctype, limit, last_indexed_modified, last_indexed_name)
			if not docs or any(self._lead_of(doc) for doc in docs):
				return docs
			last_indexed_modified = docs[-1].get("creation") or docs[-1].get("modified")
			last_indexed_name = docs[-1]["name"]

	def prepare_document(self, doc):
		# Every row is titled and scoped by its lead; a row with no lead is not indexed.
		lead = self._lead_of(doc)
		if not lead:
			return None
		ctx = self._lead_context(lead)
		if not ctx:
			return None
		document = super().prepare_document(doc)
		if not document:
			return None
		document["title"] = self._title_of(doc, ctx) or lead
		document["lead_name"] = ctx.get("title") or ""
		document["content"] = self._content_of(doc)
		document["keys"] = self._keys_of(doc, ctx)
		document["stage"] = ctx.get("stage")
		document["stage_color"] = ctx.get("stage_color")
		document.update(ctx["ids"])
		document.update(ctx["axes"])
		return document

	def _title_of(self, doc, ctx):
		"""A file is titled by its own name; every other row by its patient."""
		if doc.doctype == "File":
			return doc.get("file_name") or ctx.get("title")
		return ctx.get("title")

	def _lead_of(self, doc):
		# The CRM Lead a row hangs off, via the resolvers that already own this decision.
		if doc.doctype == "CRM Lead":
			return doc.name
		if doc.doctype == "CRM Deal":
			return doc.get("lead")
		if doc.doctype == "File":
			return _file_lead(doc)
		ref = parent_of(doc, doc.doctype)
		if ref and ref[0] == "CRM Lead":
			return ref[1]
		return None

	def _lead_context(self, lead):
		# Read once per lead and cached for the whole build.
		cache = self.__dict__.setdefault("_ctx_cache", {})
		if lead in cache:
			return cache[lead]
		cache[lead] = ctx = self._read_lead_context(lead)
		return ctx

	def _read_lead_context(self, lead):
		# Reads exactly `_LEAD_FIELDS`, so the fields that trigger a reindex are the fields read.
		row = frappe.db.get_value("CRM Lead", lead, list(_LEAD_FIELDS), as_dict=True)
		if not row:
			return None
		stage_label, stage_color = labels.stage_of(row)
		return {
			"title": row.lead_name,
			"stage": stage_label,
			"stage_color": stage_color,
			"ids": {column: (lead if fieldname == "name" else row.get(fieldname)) for column, fieldname, _k in IDENTIFIERS},
			"axes": {column: row.get(fieldname) for column, fieldname in _AXES},
		}

	def _content_of(self, doc):
		# The shown snippet: plain text only; a lead has none because its row is drawn from metadata.
		if doc.doctype == "File":
			return _words(" ".join(p for p in [doc.get("file_name"), _url_key(doc.get("file_url"))] if p))
		if doc.doctype == "CRM Deal":
			return " ".join(p for p in [doc.get("organization"), doc.get("status")] if p)
		return ""

	def _keys_of(self, doc, ctx):
		# Searched, never shown: the row's name, and on a lead every ID, phone form and stage leaf.
		parts = [doc.get("name")]
		if doc.doctype == "CRM Lead":
			for column, _fieldname, kind in IDENTIFIERS:
				parts.append(ctx["ids"].get(column))
				if kind == "digits":
					parts += _phone_keys(ctx["ids"].get(column))
			parts += [leaf(doc.get("custom_stage")), leaf(doc.get("custom_substage"))]
		return " ".join(str(p) for p in parts if p)


def build_index():
	# Console/enqueue entry point for a full build.
	CRMLeadSearch().build_index()


def sweep_index_health():
	"""Hourly: reconcile a readable index; drop an unreadable one and hand the rebuild to frappe.
	Skips while a build is running, and does nothing while the feature is off."""
	import os

	from frappe.search.sqlite_search import build_index_in_background
	from frappe.utils.synchronization import filelock

	if frappe.flags.in_migrate or frappe.flags.in_install:
		return
	# One sweep at a time: frappe only dedups a job while it is still queued.
	with filelock("crm_search_index_sweep", timeout=60):
		engine = CRMLeadSearch()
		if not (engine.is_search_enabled() and engine.index_exists()):
			return
		if os.path.exists(engine._get_db_path(is_temp=True)):
			return  # a build owns the index right now
		if engine.index_is_readable():
			reindexed, removed = engine.reconcile()
			logger = frappe.logger("search")
			(logger.warning if reindexed or removed else logger.info)(f"reconciled: reindexed {reindexed}, removed {removed}")
			return

		engine.drop_index()
		build_index_in_background()
		frappe.log_error(_REPAIR_ERROR, "The index could not be read, so it was dropped and a rebuild is queued.")


def reindex_lead(lead):
	"""Restamp a lead's row and its child rows, which copy the lead's title, grain and stage."""
	reindex_leads([lead])


def reindex_leads(leads):
	"""The batch form of `reindex_lead`: one engine and one child-row lookup for all leads."""
	engine = CRMLeadSearch()
	if not (engine.is_search_enabled() and engine.index_exists()):
		return
	targets = {("CRM Lead", lead) for lead in leads}
	for row in engine.rows_of_leads(leads):
		doctype, _, name = row["doc_id"].partition(":")
		if doctype in engine.doc_configs and name:
			targets.add((doctype, name))
	for doctype, name in sorted(targets):
		engine.index_doc(doctype, name)  # index_doc logs its own failures


# The transaction's collected leads; its absence means nothing is registered yet.
_PENDING = "_search_reindex_pending"


def _enqueue_reindex(lead):
	"""Collect the lead; one job is enqueued per transaction, after commit, never on import or migrate."""
	if not lead or frappe.flags.in_import or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	# Off means nothing is scheduled at all; a cached read, so no query per save.
	if not is_enabled(TOGGLE):
		return
	pending = getattr(frappe.local, _PENDING, None)
	if pending is None:
		pending = set()
		setattr(frappe.local, _PENDING, pending)
		frappe.db.before_commit.add(_flush_reindex)
		frappe.db.before_rollback.add(_discard_reindex)
	pending.add(lead)


def _flush_reindex():
	"""Enqueue the transaction's collected leads as one job. Registered on `frappe.db.before_commit`."""
	leads = sorted(getattr(frappe.local, _PENDING, None) or ())
	_discard_reindex()
	if not leads:
		return
	if len(leads) == 1:
		# One lead keeps a per-lead job id, so repeated saves collapse into one job.
		frappe.enqueue(
			"tatva_connect.search.index.reindex_lead",
			queue="short",
			lead=leads[0],
			enqueue_after_commit=True,
			deduplicate=True,
			job_id=f"search-reindex-{leads[0]}",
		)
		return
	frappe.enqueue(
		"tatva_connect.search.index.reindex_leads",
		queue="short",
		leads=leads,
		enqueue_after_commit=True,
	)


def _discard_reindex():
	"""Drop the transaction's collected leads. Registered on `frappe.db.before_rollback`."""
	if hasattr(frappe.local, _PENDING):
		delattr(frappe.local, _PENDING)


@fail_safe
def reindex_on_lead_context_change(doc, method=None):
	# CRM Lead.on_update: child rows copy the lead's title, grain and stage, so restamp them when one moves.
	if any(doc.has_value_changed(fieldname) for fieldname in _LEAD_FIELDS):
		_enqueue_reindex(doc.name)
