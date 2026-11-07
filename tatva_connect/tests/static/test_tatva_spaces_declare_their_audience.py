# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every Tatva Connect space declares the roles its page is for.

frappe filters a sidebar per viewer and a dock entry opens its first surviving link, so the page reaches its
audience and anyone else lands on a list they can read. An empty role list opens the page to every
module reader. The spaces are the Sidebar rows of our app's Dock. Pure stdlib.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.static.test_tatva_spaces_declare_their_audience
"""
import glob
import json
import os
import unittest

_DOCK = "dock/tatva_connect/tatva_connect.json"


def _app_root():
	d = os.path.dirname(os.path.abspath(__file__))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "hooks.py")):
			return d
		d = os.path.dirname(d)
	raise RuntimeError("app root not found")


def _load(pattern):
	for path in sorted(glob.glob(os.path.join(_app_root(), pattern))):
		with open(path) as f:
			yield json.load(f)


class TestTatvaSpacesDeclareTheirAudience(unittest.TestCase):
	def test_every_tatva_space_names_its_audience(self):
		dock = next(_load(_DOCK))
		spaces = [row["link_to"] for row in dock["items"] if row.get("link_type") == "Sidebar"]
		sidebars = {s["name"]: s for s in _load("*/sidebar/*/*.json")}
		workspaces = {w["name"]: w for w in _load("*/workspace/*/*.json")}
		self.assertTrue(spaces, "the dock lists no Sidebar — the path is wrong")
		# A space opens its sidebar's first link (frappe's module_landing_route), which must be a workspace and need not share the sidebar's name.
		first = {space: next((i for i in sidebars.get(space, {}).get("items", []) if i.get("type") == "Link"), {}) for space in spaces}
		pages = {space: item.get("link_to") if item.get("link_type") == "Workspace" else None for space, item in first.items()}
		unshipped = sorted(space for space, page in pages.items() if page not in workspaces)
		self.assertEqual(unshipped, [], "these spaces open no shipped workspace: " + ", ".join(unshipped))
		open_to_all = sorted(space for space, page in pages.items() if not workspaces[page].get("roles"))
		self.assertEqual(open_to_all, [], "these spaces name no audience: " + ", ".join(open_to_all))
