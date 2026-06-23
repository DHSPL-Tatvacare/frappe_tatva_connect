"""Multi-grain UI test fixture — seed a Sales user with TWO grain'd CRM Lead assignment rules and
a distinguishing catalog field per grain, so the grain selector + per-grain field resolution can be
click-verified in the browser. Dev-only, idempotent.

    bench --site dev.localhost execute tatva_connect.smartview.tests.run_multigrain_setup.run
"""
import frappe

USER = "zmulti-agent@example.com"
PWD = "zmulti-pass-2026"
GA = ("TatvaPractice", "Inside Sales", "FieldSales")
GB = ("TatvaPractice", "Inside Sales", "GoodFlip")
FA = "zma:onlyA"   # catalog field tagged to grain A only
FB = "zmb:onlyB"   # catalog field tagged to grain B only
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _user():
	if not frappe.db.exists("User", USER):
		u = frappe.get_doc({"doctype": "User", "email": USER, "first_name": "ZMulti Agent",
							 "send_welcome_email": 0, "user_type": "System User"})
		u.insert(ignore_permissions=True)
		u.add_roles("Sales User")
	from frappe.utils.password import update_password
	update_password(USER, PWD)


def _rule(name, grain):
	if frappe.db.exists("Assignment Rule", name):
		frappe.delete_doc("Assignment Rule", name, force=True, ignore_permissions=True)
	frappe.get_doc({
		"doctype": "Assignment Rule", "name": name, "document_type": "CRM Lead",
		"assign_condition": "True", "priority": 0, "rule": "Round Robin",
		"grain_vertical": grain[0], "grain_group": grain[1], "grain_program": grain[2],
		"users": [{"user": USER}],
		"assignment_days": [{"day": d} for d in DAYS],
	}).insert(ignore_permissions=True)


def _field(key, grain):
	spec = dict(field_key=key, label=key, fieldname="first_name", section_key="lead",
				target_doctype="CRM Lead", sql_source="parent", applies_to="lead",
				filterable=1, sortable=1, surface="worklist",
				grain_vertical=grain[0], grain_group=grain[1], grain_program=grain[2])
	if frappe.db.exists("CRM Lead API Field", key):
		frappe.get_doc("CRM Lead API Field", key).update(spec).save(ignore_permissions=True)
	else:
		frappe.get_doc(dict(doctype="CRM Lead API Field", **spec)).insert(ignore_permissions=True)


def run():
	_user()
	_rule("ZMulti Rule A", GA)
	_rule("ZMulti Rule B", GB)
	_field(FA, GA)
	_field(FB, GB)
	frappe.db.commit()

	# ---- DB EVIDENCE ----
	rules = frappe.get_all("Assignment Rule", filters={"name": ["like", "ZMulti%"]},
						   fields=["name", "grain_vertical", "grain_group", "grain_program"])
	members = frappe.get_all("Assignment Rule User", filters={"user": USER}, pluck="parent")
	from tatva_connect.access import entitlement
	setattr(frappe.local, entitlement._GRAINS_CACHE, {})
	grains = entitlement.entitled_grains(USER)
	print("USER:", USER, "| PWD:", PWD)
	print("RULES:", rules)
	print("USER_IN_RULES:", members)
	print("ENTITLED_GRAINS:", grains)
	print("FIELDS:", FA, "(grain A)", "|", FB, "(grain B)")
	return {"user": USER, "rules": rules, "grains": list(grains)}
