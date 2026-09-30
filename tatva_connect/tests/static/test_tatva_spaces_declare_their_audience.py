# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every Tatva Setup and Tatva Connect space declares the roles its page is for.

frappe filters a sidebar per viewer and a tile opens its first surviving link, so the page reaches its
audience and anyone else lands on a list they can read. An empty role list opens the page to every
module reader and shows the tile to people with nothing inside it. Pure stdlib.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.static.test_tatva_spaces_declare_their_audience
"""
import glob
import json
import os
import unittest

_FOLDERS = ("Tatva Setup", "Tatva Connect")


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
		spaces = {d["link_to"] for d in _load("desktop_icon/*.json") if d.get("parent_icon") in _FOLDERS}
		workspaces = {w["name"]: w for w in _load("*/workspace/*/*.json")}
		self.assertTrue(spaces, "no tile sits in a Tatva folder — the glob is wrong")
		self.assertEqual(sorted(spaces - set(workspaces)), [], "a Tatva tile opens no shipped workspace")
		open_to_all = sorted(name for name in spaces if not workspaces[name].get("roles"))
		self.assertEqual(open_to_all, [], "these spaces name no audience: " + ", ".join(open_to_all))
