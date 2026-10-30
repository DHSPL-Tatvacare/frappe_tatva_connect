# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every Desk tile shows an icon: Frappe v16.53 draws the label SVG of the active style, else the tile's `logo_url` (frappe/public/js/frappe/ui/desktop_icons_item.html:2-8)."""
import json
import os
import unittest

# A tile reaches the flyout only when it opens a Workspace Sidebar; an External link tile and an App
# tile are drawn by the launcher, which reads `logo_url` instead and is not this rule's business.
_FLYOUT_LINK_TYPE = "Workspace Sidebar"
_VARIANTS = ("solid", "subtle")


def _app_root():
	d = os.path.dirname(os.path.abspath(__file__))
	while d != os.path.dirname(d):
		if os.path.exists(os.path.join(d, "hooks.py")):
			return d
		d = os.path.dirname(d)
	raise RuntimeError("app root not found")


def _scrub(label):
	"""frappe.scrub: the flyout's filename is the label, lowercased, with spaces and dashes as _."""
	return label.replace(" ", "_").replace("-", "_").lower()


def _flyout_tiles():
	"""(name, label) for every Desktop Icon fixture that opens a Workspace Sidebar."""
	root = _app_root()
	tiles = []
	icon_dir = os.path.join(root, "desktop_icon")
	for fname in sorted(os.listdir(icon_dir)):
		if not fname.endswith(".json"):
			continue
		with open(os.path.join(icon_dir, fname)) as f:
			doc = json.load(f)
		if doc.get("link_type") != _FLYOUT_LINK_TYPE:
			continue
		tiles.append((doc.get("name"), doc.get("label") or doc.get("name"), doc.get("logo_url") or ""))
	return tiles


def _shipped(root, url):
	"""Is `url` an asset this app ships (`/assets/tatva_connect/<path>` -> `public/<path>`)?"""
	prefix = "/assets/tatva_connect/"
	return url.startswith(prefix) and os.path.exists(os.path.join(root, "public", url[len(prefix):]))


class TestDeskFlyoutIcons(unittest.TestCase):
	def test_every_flyout_tile_ships_an_icon_frappe_draws(self):
		"""Both label SVGs, or a shipped `logo_url`: anything else draws a grey letter."""
		root = _app_root()
		tiles = _flyout_tiles()
		self.assertTrue(tiles, "no Desktop Icon fixture opens a Workspace Sidebar — the glob is wrong")

		missing = []
		for name, label, logo in tiles:
			svgs = [os.path.join(root, "public", "icons", "desktop_icons", variant, f"{_scrub(label)}.svg") for variant in _VARIANTS]
			if not all(os.path.exists(path) for path in svgs) and not _shipped(root, logo):
				missing.append(f"{name!r} (label {label!r}): no {'/'.join(_VARIANTS)} {_scrub(label)}.svg and no shipped logo_url ({logo or 'blank'})")

		self.assertEqual(
			missing, [],
			"these tiles will draw a grey letter on the Desk, which draws the label SVG, else `logo_url`:\n  "
			+ "\n  ".join(missing),
		)

	def test_no_svg_is_stranded_by_a_rename(self):
		"""The other half: an SVG no tile resolves to is dead weight, and is the fingerprint of a rename
		that moved the records and left the artwork behind."""
		root = _app_root()
		wanted = {_scrub(label) for _name, label, _logo in _flyout_tiles()}

		stranded = []
		for variant in _VARIANTS:
			d = os.path.join(root, "public", "icons", "desktop_icons", variant)
			for fname in sorted(os.listdir(d)):
				if fname.endswith(".svg") and fname[:-4] not in wanted:
					stranded.append(f"{variant}/{fname}")

		self.assertEqual(
			stranded, [],
			"no flyout tile resolves to these; a renamed tile leaves its old artwork behind and goes "
			"grey in silence:\n  " + "\n  ".join(stranded),
		)

	def test_both_variants_ship_together(self):
		"""The flyout picks solid or subtle from the user's desktop icon style, so one without the other
		is an icon that appears for half the users."""
		root = _app_root()
		names = {}
		for variant in _VARIANTS:
			d = os.path.join(root, "public", "icons", "desktop_icons", variant)
			names[variant] = {f[:-4] for f in os.listdir(d) if f.endswith(".svg")}

		self.assertEqual(
			names["solid"], names["subtle"],
			"solid/ and subtle/ must hold the same names; a user whose desktop icon style is the missing "
			"one sees a letter",
		)
