# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Workspaces flyout draws a tile from a FILE NAME, so a renamed tile silently loses its icon.

`sidebar_header.js` resolves a flyout icon only through `get_desktop_icon` ->
`assets/<app>/icons/desktop_icons/<variant>/<scrub(label)>.svg`. It reads neither the Lucide `icon`
field nor `logo_url`, so a tile can carry a perfect Desktop Icon row, a valid sprite name, a real PNG
and a correct Workspace, and still render a grey letter. That is what `12482f7` established when it
shipped the six SVGs in the first place.

Nothing links those files to the record they draw. `partner_api.svg` was left behind when the tile was
renamed to External Leads (`87f84d2`) — the Desktop Icon, the Workspace Sidebar, the Workspace and the
launcher PNG all moved together under a patch, and these two did not, because they are keyed by scrubbed
LABEL in a directory the rename never touched. Nothing failed; the tile just went grey, and it took
weeks to notice.

This is the link. Pure stdlib — runs in tcsec and in bench run-tests.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.static.test_desk_flyout_icons
"""
import json
import os
import unittest

# A tile reaches the flyout only when it opens a Workspace Sidebar; an External link tile and an App
# tile are drawn by the launcher, which reads `logo_url` instead and is not this rule's business.
_FLYOUT_LINK_TYPE = "Workspace Sidebar"
_VARIANTS = ("solid", "subtle")

# Tiles that open a workspace belonging to ANOTHER app (Learning, Wiki). They have never shipped a
# branded SVG, so they draw the letter that says the destination leaves Tatva Connect. Deliberate:
# remove a name here the moment its SVG ships, and this test starts holding it to the same rule.
_UNBRANDED = {"LMS Admin", "Wiki Editor"}


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
		tiles.append((doc.get("name"), doc.get("label") or doc.get("name")))
	return tiles


class TestDeskFlyoutIcons(unittest.TestCase):
	def test_every_flyout_tile_ships_the_svg_its_label_resolves_to(self):
		"""The rule the rename broke: the file is named after the LABEL, not the record."""
		root = _app_root()
		tiles = _flyout_tiles()
		self.assertTrue(tiles, "no Desktop Icon fixture opens a Workspace Sidebar — the glob is wrong")

		missing = []
		for name, label in tiles:
			if name in _UNBRANDED:
				continue
			for variant in _VARIANTS:
				path = os.path.join(root, "public", "icons", "desktop_icons", variant,
				                    f"{_scrub(label)}.svg")
				if not os.path.exists(path):
					missing.append(f"{name!r} (label {label!r}) -> {variant}/{_scrub(label)}.svg")

		self.assertEqual(
			missing, [],
			"these tiles will draw a grey letter in the Workspaces flyout, because it resolves an icon "
			"ONLY by <scrub(label)>.svg and reads neither `icon` nor `logo_url`:\n  " + "\n  ".join(missing),
		)

	def test_no_svg_is_stranded_by_a_rename(self):
		"""The other half: an SVG no tile resolves to is dead weight, and is the fingerprint of a rename
		that moved the records and left the artwork behind."""
		root = _app_root()
		wanted = {_scrub(label) for _name, label in _flyout_tiles()}

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
