# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Retire Tatva User Checkin, renamed Tatva Activity Log: its DocType, its Custom DocPerm rows and its table go, as every retired doctype goes."""
from tatva_connect.patches import _schema


def execute():
	_schema.drop_doctype("Tatva User Checkin")
