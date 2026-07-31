"""Append "calendar" to the CRM View Settings type Select via a code Property Setter; idempotent.

A view type is not a view type until the row that STORES it will accept it. `crm_view_settings.json`
declares `type` as `list\ngroup_by\nkanban`, so saving a calendar view raised ValidationError — and
`ViewControls` re-saves the standard view on every param change, so one rejected value became a loop of
417s and the calendar never persisted.

A Property Setter, not a fixture, for the reason `add_acefone_telephony_medium` gives: stock options drift
between crm versions and a fixture would REPLACE them. This merges.
"""

from tatva_connect.patches.add_acefone_telephony_medium import _add_option

_TARGET = ("CRM View Settings", "type")
VIEW_TYPE = "calendar"


def execute():
	_add_option(*_TARGET, option=VIEW_TYPE)
