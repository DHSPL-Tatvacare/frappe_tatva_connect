"""Content-addressed, immutable versions — the ONE freeze a Workflow graph and a Task Form both mint through.

A version row holds `<parent_field>`, `version_no`, `definition_hash`, `payload_json` and `is_current`. Publishing an
unchanged definition reuses its version; reverting re-flags the old one. Each caller builds its own payload.
"""
import frappe
from frappe import _

# Volatile columns a frozen row must never carry: they move on every save without changing the definition.
VOLATILE = frozenset(
	("creation", "modified", "modified_by", "owner", "docstatus", "idx", "parent", "parentfield", "parenttype", "name")
)


def freeze_row(row):
	"""One row, stripped to its definition-relevant columns. `None` normalises to `""` so a field never
	set and one cleared hash identically."""
	return {k: ("" if v is None else v) for k, v in row.get_valid_dict().items() if k not in VOLATILE}


def canonical(payload):
	"""Deterministic serialisation via the native encoder (`frappe.as_json` sorts keys) - the hash input
	and the stored blob are the same bytes."""
	return frappe.as_json(payload, indent=None, separators=(",", ":"))


def definition_hash(payload):
	return frappe.utils.sha256_hash(canonical(payload))


def mint(doctype, parent_field, parent, payload, **counts):
	"""Mint the version of `payload` for `parent`, or reuse the existing one with the same content hash
	(an idempotent save mints nothing; reverting re-flags the old version). Marks it current and
	returns its name."""
	digest = definition_hash(payload)
	name = frappe.db.get_value(doctype, {parent_field: parent, "definition_hash": digest})
	if not name:
		latest = frappe.get_all(
			doctype, filters={parent_field: parent}, fields=["version_no"], order_by="version_no desc", limit=1
		)
		name = frappe.get_doc({
			"doctype": doctype,
			parent_field: parent,
			"version_no": (latest[0].version_no if latest else 0) + 1,
			"definition_hash": digest,
			"payload_json": canonical(payload),
			**counts,
		}).insert(ignore_permissions=True).name  # authz-ok: tier-a — immutable version, engine-written behind the caller's write check
	mark_current(doctype, parent_field, parent, name)
	return name


def mark_current(doctype, parent_field, parent, version_name):
	"""Exactly one current version per parent. `db.set_value` writes the flag directly: the controller's
	immutability guard defends the frozen DEFINITION, and which one is live is a fact about the parent's
	present, not about this frozen program."""
	for other in frappe.get_all(doctype, filters={parent_field: parent, "is_current": 1}, pluck="name"):
		if other != version_name:
			frappe.db.set_value(doctype, other, "is_current", 0, update_modified=False)
	frappe.db.set_value(doctype, version_name, "is_current", 1, update_modified=False)


def refuse_edit(doc, frozen, message):
	"""A version's definition never changes once minted; `message` takes the changed fields as `{0}`."""
	if doc.is_new():
		return
	changed = [f for f in frozen if doc.has_value_changed(f)]
	if changed:
		frappe.throw(message.format(", ".join(changed)), title=_("Immutable"))


def current(doctype, parent_field, parent, count_field):
	"""The frozen version now serving, or None before the first publish. Surfaced because an author otherwise cannot
	tell WHICH definition is live; the count and the short hash are what make the difference visible."""
	row = frappe.db.get_value(
		doctype,
		{parent_field: parent, "is_current": 1},
		["name", "version_no", count_field, "definition_hash", "creation"],
		as_dict=True,
	)
	if not row:
		return None
	return {
		"name": row.name,
		"version_no": row.version_no,
		count_field: row.get(count_field),
		"hash": (row.definition_hash or "")[:8],
		"created": row.creation,
	}
