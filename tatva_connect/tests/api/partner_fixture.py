# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Test-only partner scaffolding: a grain, a user, a contract and catalog rows — minted, then destroyed.

A partner's grain and the field keys its contract ticks are OPERATOR data. A test that reads them off a
dev site asserts the seed, not the code: reseed, and it goes red for a reason that is no defect. So a
test that needs a partner mints its own — a grain no operator would name, ticking catalog rows this
module created — and tears the lot down. Nothing here is read back from a seed.

Shared the way `tests/authz/grains.py` already is: the fixture lives in the package that owns the
concern (the partner API contract), and its consumers import it from wherever they live.
"""
import frappe

# Distinctive enough that no operator taxonomy can collide with them, so teardown is unambiguous.
VERTICAL = "ZZ Fixture Line"
GROUP = "ZZ Fixture Group"

# The parent section, declared by partner_api/section_seed.py — ours, never an operator's.
PARENT_SECTION = "lead"

_MAPPING = "CRM Lead API Mapping"
_CATALOG = "CRM Lead API Field"
_LEAD = "CRM Lead"

# (doctype, name) for what THIS module actually created, torn down in reverse. A row that already
# existed is left alone: the fixture may not delete something it did not mint.
_MADE = []


def _make(doctype, name, values):
	if frappe.db.exists(doctype, name):
		return name
	frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
	_MADE.append((doctype, name))
	return name


def mint_grain():
	"""The masters the contract Links to (`vertical` is reqd on the mapping)."""
	_make("CRM Vertical", VERTICAL, {"vertical_name": VERTICAL})
	_make("CRM Group", GROUP, {"group_name": GROUP})


def mint_catalog_row(fieldname, section=PARENT_SECTION):
	"""One catalog row this test owns, so a tick can name a key no seed decided. Returns its field_key.

	Outside a key-value section a catalog fieldname names a COLUMN, and a row naming one that does not
	exist declares a field the API could never write — so the column is minted alongside it, and dropped
	with it. A key-value fieldname addresses a row, so there is no column to mint."""
	key = f"{section}:{fieldname}"
	if not frappe.db.get_value("CRM Lead Section", section, "is_key_value"):
		_mint_column(fieldname)
	_make(_CATALOG, key, {
		"field_key": key, "label": fieldname, "section": section, "fieldname": fieldname,
	})
	return key


def _mint_column(fieldname):
	"""The CRM Lead column a parent-section catalog row names, created only when it is not already there."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	name = f"{_LEAD}-{fieldname}"
	if frappe.db.exists("Custom Field", name):
		return
	create_custom_field(_LEAD, {"fieldname": fieldname, "label": fieldname, "fieldtype": "Data"})
	_MADE.append(("Custom Field", name))


def mint_partner(email, ticks=()):
	"""A partner user + its enabled contract on the fixture grain, ticking exactly `ticks`.

	No `program` and no allowed_programs grid: that is program-mode NONE, so a lead resolves without one
	and the caller never has to name a program the seed happened to carry."""
	mint_grain()
	_make("User", email, {"email": email, "first_name": "Fixture Partner",
	                      "send_welcome_email": 0, "user_type": "System User"})
	user = frappe.get_doc("User", email)
	if "Partner API User" not in [r.role for r in user.roles]:
		user.append("roles", {"role": "Partner API User"})
		user.save(ignore_permissions=True)
	# The contract is keyed on its grain composite, so it is found by the partner_user COLUMN, never by name.
	if not frappe.db.exists(_MAPPING, {"partner_user": email}):
		doc = frappe.get_doc({
			"doctype": _MAPPING, "partner_user": email, "enabled": 1, "contract_name": email,
			"vertical": VERTICAL, "crm_group": GROUP,
			"allowed_fields": [{"field": k} for k in ticks],
		}).insert(ignore_permissions=True)
		_MADE.append((_MAPPING, doc.name))


def teardown():
	"""Drop everything this module minted, newest first. Leads are the consumer's own to purge — it
	knows its distinctive mobile range, and they must go before the grain they hang off."""
	for doctype, name in reversed(_MADE):
		if frappe.db.exists(doctype, name):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
	_MADE.clear()


def _sample_answer(field):
	"""A valid value for one declared field, taken from its own declaration."""
	options = [o for o in (field.get("options") or "").split("\n") if o] if field.get("options") else []
	if options:
		return options[0]
	return {
		"Int": 1, "Float": 1.0, "Check": 0, "Currency": 1.0,
		"Date": "2026-08-20", "Datetime": "2026-08-20 11:30:00", "Time": "11:30:00",
	}.get(field.get("fieldtype"), "fixture")


def minimal_answers(task_type):
	"""The smallest submission an activity type accepts, computed from the type's OWN schema.

	A form is refused when a field it SHOWS is mandatory and blank, and refused again when a field it does
	NOT show carries a value — so the answer set cannot be guessed, and naming one field here would make
	the test a copy of a seed that an operator may change. This asks the product's own resolvers instead
	(`_settled` for what the form shows, `_required_here` for what it insists on), filling each newly
	required field from its own options until nothing further is asked for. Answering one question can
	reveal the next, so it repeats — bounded by the field count, which is the most rounds that can add
	anything.
	"""
	from tatva_connect.activity.api import _required_here, _settled, compiled_fields

	fields = compiled_fields(frappe.get_cached_doc("CRM Task Type", task_type))
	answers = {}
	for _round in range(len(fields) + 1):
		shown, live = _settled(fields, answers)
		pending = [f for f in fields
		           if f.fieldname not in answers and _required_here(f, shown, live)]
		if not pending:
			return answers
		for f in pending:
			answers[f.fieldname] = _sample_answer(f)
	return answers
