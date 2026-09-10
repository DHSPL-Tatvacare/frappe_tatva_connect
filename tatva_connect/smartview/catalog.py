"""Smart Views — WHAT FIELDS EXIST, for whom, and what each one is.

The first of the composer's three concerns (`catalog` -> `query` -> `api`), and the one nothing else
depends on: this module imports neither of its siblings.

THE RESOLVED FIELD SET IS THE ALLOWLIST. No fieldname outside it can reach the SQL, appear in a picker,
or be saved into a view — every other module in this package takes the catalog as given and never widens
it. That is why the two brains that build it are here and only here:

  Lead view      -> `CRM Lead API Field` + `CRM Lead Section`. The section states ONCE which table holds
                    a field, whose columns those are and how one row is picked; a field row restates
                    none of it, so both facts are DERIVED and never stored twice.
  Activity view  -> the activity brain (`tatva_connect.activity.api`), asked live. `CRM Task Type` IS
                    the contract — the grain is its key — and `CRM Task Type Field` is its schema.

It also answers the questions that are ABOUT the catalog rather than about a query: which columns a view
projects (the starter set, the always-shown identity columns, the saved list bounded by the catalog),
which column names the row, which fields a free-text search compares, and what type a column is — the
last read through `crm_lead_section.docfield`, so a standard field like `modified` is as real a column
as any authored one.
"""
import frappe
from frappe import _
from frappe.query_builder import DocType

from tatva_connect.access import entitlement
from tatva_connect.activity import api as activity_brain
from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

LEAD_DOCTYPE = "CRM Lead"
TASK_DOCTYPE = "CRM Task"
_NO_JOIN_SOURCES = ("parent", "task")  # sql_source values answered off the driving row, no join



# ---------------------------------------------------------------------------
# Catalog read — the allowlist for a (base_object, activity_type) scope.
#
# TWO brains answer here, because this surface shows two resources and each owns its own fields:
#
#   Lead view      -> CRM Lead API Field + CRM Lead Section. The section states ONCE which table holds
#                     a field, whose columns it is and how one row is picked; a field row restates none
#                     of it. Both facts below are DERIVED, never read off the field row.
#   Activity view  -> the activity brain (tatva_connect.activity.api), asked live. CRM Task Type IS the
#                     contract — the grain is its key — and CRM Task Type Field is its schema.
#
# Smart Views used to read a frozen COPY of that schema out of the lead catalog, addressed by a Data
# field holding a task type's name as text. The types were re-keyed to composite grain PKs: every real
# Link cascaded, the text did not, and every Activity view resolved zero fields. Nothing is copied now.
#
# On THIS (internal) path the catalog is grain-filtered and role-restricted by the one entitlement
# brain (tatva_connect.access.entitlement); the partner path never does either.
# ---------------------------------------------------------------------------

def _sections():
	"""The lead sections, keyed by section_key — the ONE row that owns a section's table and row key."""
	def build():
		return {
			r.name: r
			for r in frappe.get_all(
				"CRM Lead Section",
				fields=["name", "title", "target_doctype", "child_table_field", "is_multi_row", "row_key_field",
				        "is_key_value", "value_field"],
			)
		}

	return entitlement.request_cache("tatva_connect:smartview_sections", "all", build)


def _lead_catalog():
	"""Every lead catalog row, keyed by field_key, each carrying its section's DERIVED facts: where the
	column physically lives (parent/child) and how one row of it is picked. A stored copy of either is
	what left 29 rows with a blank source and called multi-row Lab single-row, so neither is stored.

	Request-cached — one read per request, shared across every scope/grain resolution."""
	def build():
		sections = _sections()
		rows = {}
		for r in frappe.get_all(
			"CRM Lead API Field",
			fields=[
				"field_key", "label", "fieldname", "section", "filterable", "sortable", "surface",
			],
			order_by="field_key asc",
		):
			section = sections.get(r.section)
			if not section:
				continue  # a row whose section does not resolve names no table to be read from
			r.sql_source = crm_lead_section.sql_source(section)
			r.row_key_field = section.row_key_field or ""  # the field a multi-row child is ordered by; blank -> creation
			r.value_field = section.value_field or ""  # the column a key-value row's answer is read from
			r.target_doctype = section.target_doctype
			r.section_title = section.title  # composed into the flat picker/grid label; the Data tab reads the plain label under its own section header
			rows[r.field_key] = r
		return rows

	return entitlement.request_cache("tatva_connect:smartview_catalog", "all", build)


def _task_sections():
	"""The activity sections, keyed by section_key — the ONE row that owns a section's table, its row key
	and the column an answer is read from. The task twin of `_sections`, and cached the same way."""
	def build():
		return {
			r.name: r
			for r in frappe.get_all(
				"CRM Task Section",
				fields=["name", "title", "target_doctype", "child_table_field", "is_multi_row",
						"row_key_field", "is_key_value", "value_field"],
			)
		}

	return entitlement.request_cache("tatva_connect:smartview_task_sections", "all", build)


def _activity_catalog(activity_type):
	"""The activity type's fields, ASKED of the brain. Keyed `activity:<fieldname>` — the schema field's
	own name, so re-keying the TYPE moves nothing here and the type itself is reached through the view's
	Link, which cascades.

	Where a field physically lives is `field_target`'s answer and never a second reading of it: a retained
	common CRM Task column is the driving row, anything else is the section row that addresses it. Every
	declared field is therefore a real column somewhere, so every one of them is filterable and sortable —
	the JSON payload, which could only ever be projected, is gone.

	D17: a key-value answer is READ from the column its section declares and COMPARED in the typed column
	the brain names for its declared fieldtype, so a date range is a date range and not a string range."""
	if not activity_type:
		return {}
	sections = _task_sections()
	rows = {}
	for f in activity_brain.get_schema(activity_type):
		section_key, address = activity_brain.field_target(f)
		section = sections.get(section_key)
		key = f"activity:{f['fieldname']}"
		value_field = (section.value_field or "") if section else ""
		rows[key] = frappe._dict(
			field_key=key,
			label=f["label"] or f["fieldname"],
			fieldname=address,
			# The shape classifier is a fact about a section's columns, not about which resource declared it.
			sql_source=crm_lead_section.sql_source(section) if section else "task",
			row_key_field=(section.row_key_field or "") if section else "",
			value_field=value_field,
			compare_field=(activity_brain.typed_column(f["fieldtype"]) or value_field),
			target_doctype=section.target_doctype if section else TASK_DOCTYPE,
			filterable=1,
			sortable=1,
			surface="worklist",
			fieldtype=f["fieldtype"],
			options=f["options"],
		)
	return rows


def _answer_catalog():
	"""The questions a key-value section actually holds, offered as columns and filters.

	Read from the DATA rather than from a catalog, because a screening question is declared NOWHERE: no
	master lists them, and the first answer to one is its birth. What has been asked is the only true
	list, and it grows on its own as campaigns run. The digest is the fieldname, so the same question is
	one column across every form that asked it.

	REQUEST-CACHED AND NO FURTHER, like every other catalog here. It was briefly cached across requests
	on a TTL, which is wrong for a list whose whole point is that it appears the moment a question is
	first answered: a new campaign question went missing from the builder for the length of the window,
	and a filter naming it was refused outright. `tests/lead/test_screening_answers` has always asserted
	otherwise and went red.

	If this scan ever costs something at scale, the answer is an INDEX and not a cache: the query filters
	`parenttype` and groups by the row key, and the existing `ix_qhash_value` leads on the row key alone,
	so it walks the index instead of stepping between distinct values."""
	return entitlement.request_cache("tatva_connect:smartview_answers", "all", _build_answer_catalog)


def _build_answer_catalog():
	"""The scan itself — one pass per key-value section, grouped by the question."""
	rows = {}
	for section in frappe.get_all(
		"CRM Lead Section",
		filters={"is_key_value": 1},
		fields=["name", "target_doctype", *crm_lead_section.COLUMN_FIELDS],
	):
		for q in frappe.get_all(
			section.target_doctype,
			# Scoped to the parent this catalog is FOR, the same filter `_joins` and `_hydrate` apply:
			# a key-value table reused under a second parent would otherwise leak its questions here.
			filters={section.row_key_field: ("is", "set"), "parenttype": LEAD_DOCTYPE},
			fields=[f"{section.row_key_field} as identity", f"{section.label_field} as label",
			        f"{section.question_field} as question"],
			group_by=section.row_key_field,
		):
			key = f"{section.name}:{q.identity}"
			rows[key] = frappe._dict(
				field_key=key,
				label=q.label or q.question or q.identity,
				fieldname=q.identity,
				sql_source="answer",
				row_key_field=section.row_key_field,
				value_field=section.value_field,
				target_doctype=section.target_doctype,
				filterable=1,
				sortable=1,
				surface="worklist",
				fieldtype="Data",
				options="",
			)
	return rows


def _catalog_fields(base_object, activity_type, grains, roles):
	"""Catalog rows usable by a view of this base object/type, keyed by field_key, after entitlement:
	visible in `grains`, minus the fields restricted for `roles`, plus the universal floor. The composer
	never touches a fieldname outside this dict — so a saved view can never project a field outside its
	grain.

	An Activity view resolves the TYPE's schema and nothing else: its rows are CRM Tasks, so a CRM Lead
	column has no column on this query to come from. The type's key already carries its grain, so its
	fields need no second grain filter — resolve_fields still applies the role restrictions."""
	if base_object == "Activity":
		return entitlement.restrict_fields(_activity_catalog(activity_type), roles)
	# Screening answers are added AFTER entitlement, deliberately: a question is declared nowhere, so
	# there is no catalog row to tick and nothing for resolve_fields to judge. Row visibility still
	# holds — an answer hangs off a lead, and a lead outside the caller's line is unreadable. ADR 0005.
	return {**entitlement.resolve_fields(_lead_catalog(), grains, roles), **_answer_catalog()}


def _grains_for_view(v):
	"""The grain a saved view resolves its FIELDS against: the CALLER's entitlement, narrowed to the
	view's declared scope — `entitlement.entitled_grains_within`, which owns that rule.

	It is a READ of a view the caller was already admitted to, so it never throws: asking the authoring
	clamp (`_grains_from_axes`) here is what refused every vertical-wide and cross-grain-shared view the
	tab row had just offered (SV-01). But the fix for that must not become a second field gate. Returning
	the VIEW's grain shipped a rep `lead:status` on all 17 rows of a vertical-wide view while their own
	field catalog withheld it — and, because filter and sort read the catalog, the column could not even
	be filtered. Grain entitlement decides which fields a user sees; a view only narrows it."""
	return entitlement.entitled_grains_within((v.vertical, v.group, v.program))


def _settle_grain(vertical, group, program):
	"""The grain a SAVED view is scoped to, decided once at save.

	A view's columns are a fixed set for the whole table, so they can only be one grain's columns:
	resolved against several, the table carries grain A's columns beside grain B's leads, blank on most
	rows and contradicting the rule the Data Tab keeps. `_grains_from_axes` unions the caller's grains
	when none are named, which is right for the PICKER (offer what could be chosen) and wrong once the
	choice has been made.

	Hold one grain and there is nothing to choose, so it is stamped rather than demanded. Hold several
	and the view must say which. A System Manager holds ALL_GRAINS, whose whole meaning is the entire
	catalog, so an open view is theirs to make."""
	if vertical or group or program:
		return vertical, group, program
	entitled = entitlement.entitled_grains()
	if entitled == entitlement.ALL_GRAINS:
		return vertical, group, program
	if len(entitled) == 1:
		only = next(iter(entitled))
		return (only[0] or None, only[1] or None, only[2] or None)
	frappe.throw(
		_("Choose the grain this view is for. Its columns are one grain's columns, so a view spanning several cannot say what a row means."),
		title=_("Grain required"),
	)


def _grains_from_axes(vertical, group, program):
	"""A one-grain set from explicit axes, or the caller's entitled grains when none given
	(editor with no grain chosen yet → show what the caller could pick). Explicit axes are
	CLAMPED to entitlement: a grain the caller isn't entitled to is rejected fail-closed, so a
	client cannot widen scope past entitled_grains() on the picker, write, or data path."""
	if vertical or group or program:
		grain = (vertical or "", group or "", program or "")
		if not entitlement.grain_entitled(grain):
			frappe.throw(_("You are not entitled to this grain."), frappe.PermissionError)
		return {grain}
	return entitlement.entitled_grains()


def _flat_label(r):
	"""The label for a flat surface (picker, results grid) that has no section header to lean on: the section title prefixes the plain column label so a column reads unambiguously. The stored label stays the plain column name; activity fields (no section) render as-is."""
	base = r.label or r.fieldname
	return f"{r.section_title} — {r.label}" if r.get("section_title") and r.label else base


# ---------------------------------------------------------------------------
# Query assembly.
# ---------------------------------------------------------------------------

def _driving(base_object):
	"""(driving DocType name, driving qb table)."""
	return (LEAD_DOCTYPE, DocType(LEAD_DOCTYPE)) if base_object == "Lead" else (TASK_DOCTYPE, DocType(TASK_DOCTYPE))


def _starter_columns(cat):
	"""What a view projects when it has chosen nothing — the driving row's OWN fields, never a join.

	Stock CRM declares a default column set per doctype (`default_list_data`); this is the same idea,
	read off the brain instead of hardcoded. Child and answer fields are excluded by construction, and
	that is what keeps a column-less view under MariaDB's 61-table join ceiling however many screening
	questions exist. `empty` therefore means this set — it has never meant "every field"."""
	return [
		k for k, r in cat.items()
		if (r.surface or "worklist") == "worklist" and r.sql_source in _NO_JOIN_SOURCES
	]


# The columns EVERY view of a base object carries, whatever its author chose — the row has to say whose it
# is. NOT a frozen or sticky column: frappe-ui's ListView cannot freeze one (C.18), and `CRM Smart View`
# already spends the word `pinned` on a pinned TAB. These are the columns a view may not drop, which is
# what the picker calls them on screen: "Always shown". Frappe's own saved views work this way: `crm_view_settings.create` adds `default_list_data()["rows"]`
# to whatever the author picked, so "fields a view must carry" is a native idea, not one invented here.
#
# The human identity and who answers for the row, never the docname: `name` is a hash a rep cannot read,
# and support can add it to a view when they need it. An ACTIVITY view pins nothing — its catalog is the
# task type's declared form fields, so a title or a due date is not a key it could name, and the type is
# already constant for the whole view.
#
# ONLY WHAT CANNOT BE DERIVED IS TYPED. The column that NAMES the row is the driving doctype's own title
# field, asked of `get_meta` — the same question `_identity_key` asks, so the two cannot answer differently
# (A.8). These two are the ones no framework rule declares: the number a rep dials, and who answers for it.
_ALWAYS_SHOWN_EXTRA = {"Lead": ("mobile_no", "lead_owner")}


def _always_shown_fieldnames(base_object):
	"""The fieldnames every view of this base object carries: the title field, then the typed tail."""
	if base_object not in _ALWAYS_SHOWN_EXTRA:
		return ()
	driving_name, _table = _driving(base_object)
	title = frappe.get_meta(driving_name).get_title_field()
	return tuple(f for f in (title, *_ALWAYS_SHOWN_EXTRA[base_object]) if f)


def _always_shown(base_object, cat):
	"""The always-shown keys this CALLER can actually be shown, in declared order.

	Resolved fieldname -> field_key through the catalog and bounded to the driving row, so this can only
	ever name a column that costs no join and that the caller is already entitled to see. Catalog-bounded
	like everything else: a key the caller's grain or role withholds is dropped rather than forced, because
	a column nobody may see is the leak this whole surface is built to refuse. A site whose catalog does not
	carry one of them simply shows one fewer. `test_identity_columns_are_always_shown` pins the result."""
	keys = []
	for fieldname in _always_shown_fieldnames(base_object):
		for key, r in cat.items():
			if r.fieldname == fieldname and r.sql_source in _NO_JOIN_SOURCES and key not in keys:
				keys.append(key)
				break
	return tuple(keys)


def _with_always_shown(keys, base_object, cat):
	"""`keys` with the always-shown columns in front, deduped, order otherwise preserved."""
	always = _always_shown(base_object, cat)
	rest = [k for k in keys if k not in always]
	return [*always, *rest]


def _column_field_keys(view, cat):
	"""The catalog field_keys this view projects, always led by the always-shown identity columns. A saved list is
	used on top of them (catalog-bounded); a view carrying none falls to the starter set."""
	try:
		keys = frappe.parse_json(view.columns) if view.columns else []
	except Exception:
		frappe.write_only()(frappe.log_error)(title="smartview: bad saved columns JSON")
		keys = []
	keys = [k for k in (keys or []) if k in cat]
	return _with_always_shown(keys or _starter_columns(cat), view.base_object, cat)


def _identity_key(base_object, col_keys, cat):
	"""The ONE column that names the row, so only it draws the person chip every other listing draws there.

	The rule was "the first column", which made a chip out of whatever happened to be leftmost — a Created
	On cell rendered as a lead. The name column is the one `_always_shown_fieldnames` leads with — the
	doctype's own title field — asked THERE rather than derived a second time here. A lead view whose title
	field is not projected falls back to its first Data column, which is the name part these views show.
	An ACTIVITY row is a CRM Task whose name column is a snapshot of the punch (D-C), so it never gets one."""
	if base_object != "Lead":
		return None
	title_field = next(iter(_always_shown_fieldnames(base_object)), None)
	for key in col_keys:
		if cat.get(key) and cat[key].fieldname == title_field:
			return key
	for key in col_keys:
		if cat.get(key) and _col_type(cat[key])[0] == "Data":
			return key
	return None


def _search_keys(cat, col_keys, driving_name):
	"""Which fields a free-text search compares: the row's own IDENTITY fields, plus whatever this view
	displays — and never a field that lives off the driving row.

	TWO DEFECTS, ONE RULE. Search was "every projected filterable field", so it searched whatever the view
	happened to show: a patient's phone found nothing on a view without a phone column, and a view whose
	columns were all unfilterable searched NOTHING and returned the whole list looking searched. A person
	typing into a patient list is looking for a PERSON, so identity is searched whatever the view shows.

	Identity is ADDED to what the view projects, never substituted for it. Bounding the set to fields on
	the driving row would have cost no join — but an ACTIVITY worklist's columns ARE its form's section
	fields, so a rep searching a word they can see in the Remark column would have found nothing. A search
	that cannot find what is on screen is a worse defect than a slow one; the joins a projected column
	already needs are the price, and only while a term is actually typed.

	The identity set is DERIVED, never typed here: frappe's own `get_search_fields`/`get_title_field`, plus
	the app's own ID rule (`search.index.IDENTIFIERS`). A list retyped here would be a second one."""
	from tatva_connect.search.index import IDENTIFIERS

	meta = frappe.get_meta(driving_name)
	identity = {*meta.get_search_fields(), meta.get_title_field()}
	identity |= {fieldname for _column, fieldname, _kind in IDENTIFIERS}
	return {
		key for key, r in cat.items()
		if r.filterable
		and (key in col_keys or (r.sql_source in _NO_JOIN_SOURCES and r.fieldname in identity))
	}


def _col_docfield(r):
	"""The live DocField backing a lead catalog row, standard fields included (`crm_lead_section.docfield`).
	The row's target_doctype is its section's. None when it cannot be resolved."""
	dt = (r.target_doctype or "").strip()
	if not dt:
		return None
	# A key-value row's `fieldname` addresses a ROW, so the column its answer is read from is the
	# section's value column, and that is the type the worklist must format.
	fieldname = r.value_field if r.sql_source == "answer" else r.fieldname
	try:
		return crm_lead_section.docfield(dt, fieldname)
	except Exception:
		frappe.write_only()(frappe.log_error)(title="smartview: unresolvable catalog target",
		                                     message=f"{dt}.{fieldname}")
		return None


def _col_type(r):
	"""(fieldtype, options) for a catalog column — drives the frontend's column width, cell formatting
	and the native Filter/ColumnSettings controls (operator menu, value widget, Link target).

	An activity field answers with the SCHEMA's own type, because that is the type the brain declared and
	the one the form submits: the promoted column it lands in is a generic Data column, and reading meta
	there would flatten a Select back to free text. A lead field is a real column, so meta IS its truth.
	Unknown -> 'Data' (inert)."""
	if r.get("fieldtype"):
		return r.fieldtype, (r.options or "")
	df = _col_docfield(r)
	return (df.fieldtype, df.options or "") if df else ("Data", "")


def _link_master(r):
	"""The Link target a catalog column points at, or None where it is not a Link. Read off `_col_type`, so
	the filter and the control that offered the value are looking at the same answer."""
	fieldtype, options = _col_type(r)
	return options if fieldtype == "Link" else None


def _validate_columns(columns, cat):
	"""The requested columns, every one a catalog field_key for the scope. Throws on any
	unknown key (fail-closed allowlist). Returns the cleaned, order-preserving list."""
	if isinstance(columns, str):
		columns = frappe.parse_json(columns) if columns else []
	columns = columns or []
	bad = [k for k in columns if k not in cat]
	if bad:
		frappe.throw(_("Unknown column field(s): {0}").format(", ".join(map(str, bad))))
	return list(columns)
