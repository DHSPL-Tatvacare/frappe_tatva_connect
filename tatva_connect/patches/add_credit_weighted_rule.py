"""Merge "Credit Weighted" into Assignment Rule's `rule` Select; a fixture would replace core's four options."""

from tatva_connect.lead.assignment_rule import CREDIT_WEIGHTED
from tatva_connect.patches.add_acefone_telephony_medium import _add_option


def execute():
	_add_option("Assignment Rule", "rule", option=CREDIT_WEIGHTED)
