# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Retire CRM Lead Field Restriction: no screen offered it, and whoever owns the lead sees every field."""
from tatva_connect.patches import _schema


def execute():
	_schema.drop_doctype("CRM Lead Field Restriction")
