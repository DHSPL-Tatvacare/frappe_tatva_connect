# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The record tools — rows of the doctypes an operator lists, and of nothing else.

The list is Tatva Platform Settings → Access → Data Access for AI Agents → Readable Tables. Not listed is not readable, and
an empty list reads nothing, so the capability ships dormant. A listed doctype brings its child tables
with it (`get_doc` returns them); a Link shows its value and its URL, but the target's own rows stay
closed unless it is listed too.

Inside the list Frappe decides, exactly as Desk does: `frappe.get_list` applies row and field-level read
permissions, and one record goes through `get_doc` + `has_permission` + `apply_fieldlevel_read_permissions`.
On top of that the agent sees what the screen shows — a Password or hidden field is never sent.

Reads stay cheap: plain SELECTs (no lock is ever asked for), sorted by `modified` (indexed on every
table), one page at a time from the form's page size, and never a count.
"""
import difflib

import frappe
from frappe.model import get_permitted_fields, no_value_fields, table_fields

from tatva_connect.mcp import ToolError, desk, schema, settings, words

READABLE_FIELD = "mcp_readable_tables"


def _workflow(doc):
	"""A workflow's design lives outside its row: the graph the engine freezes, and the builder's own verdict on it."""
	from tatva_connect.workflow_engine.versions import build_payload

	return {"graph": build_payload(doc), "problems": doc.publish_problems()}


# Records whose meaning lives outside their own row, each read by the engine that owns it.
EXTRAS = {"CRM Workflow": _workflow}


def readable():
	"""The doctypes an operator opened, in the order listed. A child table is read through its parent only."""
	rows = frappe.get_cached_doc(settings.SETTINGS).get(READABLE_FIELD) or []
	return [row.document_type for row in rows if not frappe.get_meta(row.document_type).istable]


def _open(doctype):
	"""The meta of a doctype this login may read rows of, or a refusal that says where to look."""
	doctype = (doctype or "").strip()
	listed = readable()
	if doctype not in listed:
		near = difflib.get_close_matches(doctype, listed, n=1, cutoff=0.6)
		hint = f" Did you mean '{near[0]}'?" if near else ""
		raise ToolError(f"'{doctype}' is not a readable table.{hint} list_tables names the tables this login can read.")
	if not schema.can_read(doctype):
		raise ToolError(f"This login cannot read '{doctype}'. list_tables names the tables it can.")
	return frappe.get_meta(doctype)


def _shown(df):
	"""Would the form show this field's value? Never a Password, never a hidden field, never a layout field."""
	return bool(df) and not df.hidden and df.fieldtype != "Password" and df.fieldtype not in no_value_fields


def _filled(row):
	"""An empty value is left out, as frappectl's `_slim_document` does: the schema already says the field exists."""
	return {key: value for key, value in row.items() if value not in (None, "", [])}


def _values(doc, top=True):
	"""The fields the form shows, child tables included the same way. A field the login may not read is absent."""
	out = {"name": doc.name} if top else {}
	for df in doc.meta.fields:
		# A table is never a hidden secret (User's roles are hidden only because Desk draws them itself); field levels still apply.
		if df.fieldtype in table_fields and hasattr(doc, df.fieldname):
			out[df.fieldname] = [_values(row, top=False) for row in doc.get(df.fieldname) or []]
		elif _shown(df) and hasattr(doc, df.fieldname):
			out[df.fieldname] = doc.get(df.fieldname)
	return _filled(out)


# -- tools --------------------------------------------------------------------
def list_tables(_arguments):
	"""Every table this login can read rows of, in the order the operator listed them."""
	tables = [{"doctype": doctype, "single": bool(frappe.get_meta(doctype).issingle), "url": desk.list_url(doctype)}
	          for doctype in readable() if schema.can_read(doctype)]
	return {"tables": tables,
	        "next": "Call get_schema for a table's fields, then search_records — or get_record for a single."}


def search_records(arguments):
	"""One page of rows, narrowed by words and exact matches, with the columns the list view shows."""
	meta = _open(arguments.get("doctype"))
	if meta.issingle:
		raise ToolError(f"'{meta.name}' is one settings record: call get_record with just the doctype.")

	# Frappe's query engine refuses a filter on a field this login cannot read, so only those it can are offered.
	permitted = set(get_permitted_fields(meta.name))

	def usable(field):
		return field == "name" or (field in permitted and _shown(meta.get_field(field)))

	filters = arguments.get("filters") or {}
	for field in filters:
		if not usable(field):
			# Naming the fields it can use lets the agent correct the call at once, instead of searching for them.
			offered = [df.fieldname for df in meta.fields if df.fieldtype in ("Link", "Select", "Data", "Check") and usable(df.fieldname)]
			raise ToolError(f"'{field}' is not a field of {meta.name} this login can filter on. "
			                f"It can filter on: {', '.join(['name', *offered][:12])}.")

	searched = [f for f in dict.fromkeys([meta.get_title_field(), *meta.get_search_fields()]) if usable(f)]
	columns = [f for f in meta.get_list_fields() if f == "name" or _shown(meta.get_field(f))]

	size, page = settings.config()["record_page_size"], max(1, arguments.get("page") or 1)
	try:
		rows = frappe.get_list(meta.name, fields=columns, filters=_conditions(filters, searched, arguments.get("query")),
		                       order_by="modified desc", limit_start=(page - 1) * size, limit_page_length=size + 1)
	except frappe.ValidationError as invalid:
		# frappe's own filter check (an unknown operator) — its words go back so the agent can fix the call.
		raise ToolError(f"Those filters were refused: {frappe.utils.strip_html(str(invalid))}") from None
	more = len(rows) > size
	return {
		"doctype": meta.name,
		"list_url": desk.list_url(meta.name, filters),
		"page": page,
		"rows": [{**_filled(row), "url": desk.form_url(meta.name, row.name)} for row in rows[:size]],
		"more": more,
		"next": (f"More rows exist. Narrow with filters on {', '.join(columns)} rather than paging on." if more
		         else f"Nothing matches: the search covered {', '.join(searched)} and is complete." if not rows
		         else "Call get_record for the one record you need in full."),
	}


def get_record(arguments):
	"""One record in full: what its form shows, its child tables, the screens that open it, and the URL of each Link."""
	meta = _open(arguments.get("doctype"))
	name = (arguments.get("name") or "").strip()
	if not (name or meta.issingle):
		raise ToolError(f"Name the {meta.name} record — search_records returns names.")

	# frappe.client.get's own sequence: load, check_permission, strip what field levels hide.
	try:
		doc = frappe.get_doc(meta.name) if meta.issingle else frappe.get_doc(meta.name, name)
		doc.check_permission("read")
	except frappe.DoesNotExistError:
		raise ToolError(f"No {meta.name} named '{name}'. search_records finds the right name.") from None
	except frappe.PermissionError:
		raise ToolError(f"This login cannot read {meta.name} '{name}'.") from None
	doc.apply_fieldlevel_read_permissions()

	record = _values(doc)
	record["urls"] = desk.record_urls(meta.name, doc.name)
	links = {df.fieldname: desk.form_url(df.options, record[df.fieldname]) for df in meta.get_link_fields()
	         if record.get(df.fieldname)}
	if links:
		record["link_urls"] = links
	if add_extras := EXTRAS.get(meta.name):
		record.update(add_extras(doc))
	return record


def _conditions(filters, searched, query):
	"""Frappe's nested filter form: the exact matches, and every search word found in at least one searched field."""
	exact = [[field, *(value if isinstance(value, list) else ["=", value])] for field, value in filters.items()]
	groups = [_either([[field, "like", f"%{word}%"] for field in searched]) for word in words(query)]
	if not groups:
		return exact
	return _joined(exact + groups, "and")


def _either(conditions):
	return conditions[0] if len(conditions) == 1 else _joined(conditions, "or")


def _joined(items, operator):
	joined = [items[0]]
	for item in items[1:]:
		joined += [operator, item]
	return joined
