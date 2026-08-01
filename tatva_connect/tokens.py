# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Read the clock once, then replace `__NOW__`-style tokens inside a declared structure. One walker."""


def snapshot(tokens):
	"""Every token resolved ONCE, so a row cannot be filtered against one instant and shown against another."""
	return {token: read() for token, read in tokens.items()}


def substitute(value, snap):
	"""A declared value with its tokens replaced. Walks lists and dicts; anything else is returned as it is."""
	if isinstance(value, str):
		return snap.get(value, value)
	if isinstance(value, list | tuple):
		return [substitute(item, snap) for item in value]
	if isinstance(value, dict):
		return {key: substitute(item, snap) for key, item in value.items()}
	return value
