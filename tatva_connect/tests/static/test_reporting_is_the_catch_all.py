# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Reporting is the one page holding every operational number card and chart the app ships.

Each space keeps its own view; Reporting gathers them all. A card or chart added to a space or to the
fixtures without also landing here turns this red. Pure stdlib — runs in tcsec and in bench run-tests.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.static.test_reporting_is_the_catch_all
"""
import glob
import json
import os
import unittest

_KEYS = {"number_card": "number_card_name", "chart": "chart_name"}
_REPORTING = "Reporting"


def _app_root():
	d = os.path.dirname(os.path.abspath(__file__))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "hooks.py")):
			return d
		d = os.path.dirname(d)
	raise RuntimeError("app root not found")


def _workspaces():
	for path in sorted(glob.glob(os.path.join(_app_root(), "*", "workspace", "*", "*.json"))):
		with open(path) as f:
			yield json.load(f)


def _placed(workspace):
	"""(type, name) for every card and chart a workspace's page draws."""
	return {(b["type"], b["data"][_KEYS[b["type"]]]) for b in json.loads(workspace["content"]) if b["type"] in _KEYS}


def _shipped():
	"""(type, name) for every card and chart the fixtures ship."""
	out = set()
	for kind, fname in (("chart", "dashboard_chart.json"), ("number_card", "number_card.json")):
		with open(os.path.join(_app_root(), "fixtures", fname)) as f:
			out |= {(kind, row["name"]) for row in json.load(f)}
	return out


class TestReportingIsTheCatchAll(unittest.TestCase):
	def setUp(self):
		spaces = {w["name"]: w for w in _workspaces()}
		self.reporting = spaces.pop(_REPORTING)
		self.others = spaces

	def test_every_card_and_chart_on_a_space_is_on_reporting(self):
		missing = sorted(set().union(*map(_placed, self.others.values())) - _placed(self.reporting))
		self.assertEqual(missing, [], "on a space but not on Reporting: " + ", ".join(f"{t} {n!r}" for t, n in missing))

	def test_every_shipped_card_and_chart_is_on_reporting(self):
		missing = sorted(_shipped() - _placed(self.reporting))
		self.assertEqual(missing, [], "shipped but not on Reporting: " + ", ".join(f"{t} {n!r}" for t, n in missing))

	def test_the_page_and_its_rows_name_the_same_cards_and_charts(self):
		"""frappe renders a block only when the workspace also lists it as a row."""
		rows = {("chart", r["chart_name"]) for r in self.reporting["charts"]}
		rows |= {("number_card", r["number_card_name"]) for r in self.reporting["number_cards"]}
		self.assertEqual(rows, _placed(self.reporting))
