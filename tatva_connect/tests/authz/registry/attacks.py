# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The 13 attack vectors — the directions in which access could break (TESTS.md §5).

Each vector names a breakage and the oracle that judges it (audit 2026-06-26: the oracle MUST
match the surface — has_permission is blind to PQC and to permlevel-1 fields). `cases.py` attaches
concrete principals/targets to these vectors; `mutation.py` must plant at least one bug per vector
so recall==1.0 actually means coverage (not "untested vector silently excluded").
"""

# oracle keys map to functions in oracle.py:
#   visible_names      -> native_visible_names      (row reads, runs PQC)
#   can_read_row       -> native_can_read_row       (single-doc read on PQC-scoped doctypes)
#   permitted_fields   -> native_permitted_fields   (field/permlevel leaks)
#   would_allow        -> native_would_allow        (write/create/delete on a specific doc)
#   capability         -> native_doctype_capability (deny-sweeps only)
ATTACKS = {
	"A1": {"title": "Horizontal grain leak", "oracle": "visible_names",
	       "desc": "see another grain's rows"},
	"A2": {"title": "Same-program / different-vertical leak", "oracle": "visible_names",
	       "desc": "grains #4 & #5 share program 'InsideSales' — must not leak across vertical/group"},
	"A3": {"title": "Vertical (privilege) escalation", "oracle": "capability",
	       "desc": "role1 user gains role2's perms"},
	"A4": {"title": "Grain-vs-role contradiction", "oracle": "permitted_fields",
	       "desc": "grain shows a field/row a role restriction should hide — restriction must win"},
	"A5": {"title": "reports_to roll-up over-widening", "oracle": "visible_names",
	       "desc": "manager inherits more than the union of reports' grains; cycle; depth bypass"},
	"A6": {"title": "Bypass-write escalation", "oracle": "would_allow",
	       "desc": "an ignore_permissions path writes what the permission-checked path would deny"},
	"A7": {"title": "Field / permlevel leak", "oracle": "permitted_fields",
	       "desc": "permlevel-1 grain field rendered to a user without permlevel-1 read"},
	"A8": {"title": "Orphan / forged parent", "oracle": "can_read_row",
	       "desc": "child with missing/forged parent becomes visible"},
	"A9": {"title": "Switch-OFF exposure", "oracle": "visible_names",
	       "desc": "a visibility switch OFF silently opens a child doctype (document the delta)"},
	"A10": {"title": "Native-method bypass", "oracle": "would_allow",
	        "desc": "the 11 wrapped methods leak via the raw native call"},
	"A11": {"title": "Cross-app doctype leak", "oracle": "capability",
	        "desc": "a role reaches another app's doctype outside its remit"},
	"A12": {"title": "User-perm / DocShare over-grant", "oracle": "visible_names",
	        "desc": "a fence or share grants more than intended"},
	"A13": {"title": "Partner-mapping abuse", "oracle": "would_allow",
	        "desc": "disabled / multi-mapping / wrong-tenant attribution (invariant 16)"},
}


def attack(key):
	return ATTACKS[key]


def all_keys():
	return list(ATTACKS.keys())
