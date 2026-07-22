# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The registry of valid (vertical, group, program) combinations.

Deliberately inert. The three axes ARE the composite name (`format:{vertical}::{group}::{program}`), so
the primary key enforces "unique on the three" — a second row for the same tuple cannot be inserted, and
`set_only_once` on each axis stops an edit from drifting a row's tuple away from the name it was given.
A `validate` that re-checked the same rule would be a second copy of it.

Blank program is a real combination, not a hole: a lead exists before programme enrolment, and a group
may be declared as a whole region. The registry is CURATED config seeded from reality — never a raw
mirror of the lead table, which carries typos.
"""
from frappe.model.document import Document


class CRMGrain(Document):
	pass
