"""Append "AI Voice" to the CRM Call Log telephony_medium Select via a code Property Setter (stock options drift between crm versions); idempotent."""
from tatva_connect.patches.add_acefone_telephony_medium import _add_option
from tatva_connect.voice.adapters.bolna import CALL_MEDIUM

# Only the call log. An AI call is placed by automation, never by a rep picking a medium, so
# `CRM Telephony Agent.default_medium` deliberately does NOT gain it — offering it there would let
# someone set their personal default to a medium they cannot dial with.
_TARGET = ("CRM Call Log", "telephony_medium")


def execute():
	_add_option(*_TARGET, option=CALL_MEDIUM)
