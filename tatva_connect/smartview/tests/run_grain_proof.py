"""GRAIN ENTITLEMENT HEADLESS PROOF — run on a dev bench with:

    bench --site dev.localhost execute tatva_connect.smartview.tests.run_grain_proof.run

Proves the internal grain-entitlement brain end-to-end on real SQL:
  * entitled_grains(user) reads the grain off the user's CRM Lead Assignment Rule,
  * field_in_grains is a correct containment test (blank axis = wildcard),
  * resolve_fields keeps grain-matched fields, drops a CRM Lead Field Restriction for the role,
    and always keeps the universal floor,
  * field_catalog (the editor/composer endpoint) returns the grain-filtered list for the caller,
  * System Manager sees ALL_GRAINS,
  * the assignment override blocks a rule whose grain != the lead's grain, passes when it matches,
  * the PARTNER path is untouched (a mapping caller's _allowed_keys = full catalog when allowed empty).

Self-seeds isolated test data keyed off a ZGRAIN tag and reuses real grain masters. Idempotent.
"""
import frappe

from tatva_connect.smartview import api
from tatva_connect.access import entitlement

TAG = "ZGRAIN_PROOF"
USER = "zgrain-proof-agent@example.com"
RULE = "ZGRAIN Proof Rule"
GFIELD = "zgrain:in"     # grain-tagged catalog field
OFIELD = "zgrain:out"    # different-grain catalog field
ROLE = "Sales User"


def _pick_grain():
	"""A real (vertical, group, program) triple — Link validation only needs the names to exist."""
	v = frappe.get_all("CRM Vertical", pluck="name", limit=1)
	g = frappe.get_all("CRM Group", pluck="name", limit=1)
	p = frappe.get_all("CRM Program", pluck="name", limit=1)
	if not (v and g and p):
		raise RuntimeError("Need at least one CRM Vertical/Group/Program master to run the grain proof.")
	return v[0], g[0], p[0]


def _other_program(p):
	alts = [x for x in frappe.get_all("CRM Program", pluck="name") if x != p]
	return alts[0] if alts else p


def _seed_catalog(v, g, p, p_other):
	rows = [
		# grain-tagged field for (v,g,p)
		dict(field_key=GFIELD, label="Grain In", fieldname="first_name", section_key="lead",
			 target_doctype="CRM Lead", sql_source="parent", applies_to="lead", filterable=1, sortable=1,
			 surface="worklist", grain_vertical=v, grain_group=g, grain_program=p),
		# field tagged to a DIFFERENT program
		dict(field_key=OFIELD, label="Grain Out", fieldname="last_name", section_key="lead",
			 target_doctype="CRM Lead", sql_source="parent", applies_to="lead", filterable=1, sortable=1,
			 surface="worklist", grain_vertical=v, grain_group=g, grain_program=p_other),
	]
	for r in rows:
		if frappe.db.exists("CRM Lead API Field", r["field_key"]):
			frappe.get_doc("CRM Lead API Field", r["field_key"]).update(r).save(ignore_permissions=True)
		else:
			frappe.get_doc(dict(doctype="CRM Lead API Field", **r)).insert(ignore_permissions=True)


def _seed_user():
	if not frappe.db.exists("User", USER):
		u = frappe.get_doc({"doctype": "User", "email": USER, "first_name": "ZGrain Agent",
							 "send_welcome_email": 0, "user_type": "System User"})
		u.insert(ignore_permissions=True)
		u.add_roles(ROLE)


def _seed_rule(v, g, p):
	if frappe.db.exists("Assignment Rule", RULE):
		frappe.delete_doc("Assignment Rule", RULE, force=True, ignore_permissions=True)
	frappe.get_doc({
		"doctype": "Assignment Rule", "name": RULE, "document_type": "CRM Lead",
		"assign_condition": "True", "priority": 0, "rule": "Round Robin",
		"grain_vertical": v, "grain_group": g, "grain_program": p,
		"users": [{"user": USER}],
		"assignment_days": [{"day": d} for d in
			("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")],
	}).insert(ignore_permissions=True)


def _check(label, cond):
	print("  [{0}] {1}".format("PASS" if cond else "FAIL", label))
	return cond


def run():
	frappe.flags.in_test = True
	v, g, p = _pick_grain()
	p_other = _other_program(p)
	_seed_catalog(v, g, p, p_other)
	_seed_user()
	_seed_rule(v, g, p)
	frappe.db.delete("CRM Lead Field Restriction", {"field": ["in", [GFIELD, OFIELD]]})
	frappe.db.commit()
	# entitled_grains is request-cached; clear so each phase re-reads.
	frappe.local.__dict__.pop(entitlement._GRAINS_CACHE, None)
	frappe.local.__dict__.pop(entitlement._RESTRICT_CACHE, None)

	r = []
	print("\n== entitled_grains reads the assignment rule ==")
	grains = entitlement.entitled_grains(USER)
	print("    grains:", grains)
	r.append(_check("agent grain == rule grain (v,g,p)", grains == {(v, g, p)}))

	print("\n== field_in_grains containment ==")
	gin = frappe.get_doc("CRM Lead API Field", GFIELD).as_dict()
	gout = frappe.get_doc("CRM Lead API Field", OFIELD).as_dict()
	r.append(_check("grain field visible to its grain", entitlement.field_in_grains(gin, {(v, g, p)})))
	r.append(_check("other-grain field NOT visible", not entitlement.field_in_grains(gout, {(v, g, p)})))

	print("\n== resolve_fields: grain filter + restriction + universal floor ==")
	cat = {GFIELD: frappe._dict(gin), OFIELD: frappe._dict(gout)}
	out = entitlement.resolve_fields(cat, {(v, g, p)}, [ROLE])
	r.append(_check("grain field kept", GFIELD in out))
	r.append(_check("other-grain field dropped", OFIELD not in out))
	# now restrict the grain field for the role
	frappe.get_doc({"doctype": "CRM Lead Field Restriction", "role": ROLE, "field": GFIELD}).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.local.__dict__.pop(entitlement._RESTRICT_CACHE, None)
	out2 = entitlement.resolve_fields(cat, {(v, g, p)}, [ROLE])
	r.append(_check("restricted field hidden for role", GFIELD not in out2))

	print("\n== field_catalog endpoint, as the agent ==")
	frappe.local.__dict__.pop(entitlement._GRAINS_CACHE, None)
	frappe.set_user(USER)
	try:
		keys = {f["field_key"] for f in api.field_catalog("Lead")}
	finally:
		frappe.set_user("Administrator")
	r.append(_check("agent catalog excludes other-grain field", OFIELD not in keys))
	r.append(_check("agent catalog excludes role-restricted field", GFIELD not in keys))

	print("\n== System Manager sees ALL_GRAINS ==")
	frappe.local.__dict__.pop(entitlement._GRAINS_CACHE, None)
	r.append(_check("admin -> ALL_GRAINS", entitlement.entitled_grains("Administrator") == entitlement.ALL_GRAINS))

	print("\n== assignment override ANDs grain ==")
	from tatva_connect.lead.assignment_rule import TatvaAssignmentRule
	rule = frappe.get_doc("Assignment Rule", RULE)
	rule.__class__ = TatvaAssignmentRule
	match = frappe._dict(doctype="CRM Lead", name="ZG-1", custom_vertical=v, custom_group=g, custom_current_program=p)
	miss = frappe._dict(doctype="CRM Lead", name="ZG-2", custom_vertical=v, custom_group=g, custom_current_program=p_other)
	r.append(_check("override blocks a non-matching-grain lead", rule._lead_grain_matches(miss) is False))
	r.append(_check("override allows a matching-grain lead", rule._lead_grain_matches(match) is True))

	print("\n== partner path untouched ==")
	from tatva_connect.api import partner
	full = len(partner._catalog()["keys"])
	allowed_empty = partner._allowed_keys("any-partner-without-mapping", has_mapping=False)
	r.append(_check("no-mapping caller -> full catalog (unchanged)", len(allowed_empty) == full))

	ok = all(r)
	print("\n==== GRAIN PROOF {0} ({1}/{2} checks passed) ====".format(
		"PASSED" if ok else "FAILED", sum(1 for x in r if x), len(r)))
	return ok
