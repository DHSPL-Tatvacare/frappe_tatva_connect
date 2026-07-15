# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One logging brain for the whole harness: per-call lines, phase headers, live progress, a summary.

Every module that talks to the wire (client.Partner, lsq.LSQ) and the loader (run.py) writes through a
single Logger so the operator sees one consistent stream instead of each module's ad-hoc prints. The
Logger is the ONLY place that knows about `rich`: client.py and lsq.py stay dependency-free and merely
call `logger.event(...)`. If `rich` is not installed the Logger degrades to plain prints, so the harness
never fails to run for want of a pretty-printer.

Thread-safe: the loader drives leads on a ThreadPoolExecutor, so every write holds one lock. Verbose
mode streams a line per call (no progress bar — the lines are the detail); quiet mode shows a progress
bar per phase and the summary. Both are rich.
"""
import threading

try:
	from rich.console import Console
	from rich.table import Table
	_RICH = True
except Exception:  # rich absent -> plain prints, harness still runs
	_RICH = False

_OK = "green"
_FAIL = "red"
_DIM = "bright_black"
_SRC = {"api": "cyan", "lsq": "magenta", "file": "yellow"}


class Logger:
	"""The operator-facing console. `verbose` streams every call; both modes print phases and summary."""

	def __init__(self, verbose=False):
		self.verbose = verbose
		self.lock = threading.Lock()
		self.console = Console() if _RICH else None

	def _print(self, text, markup=""):
		with self.lock:
			if self.console is not None:
				self.console.print(f"[{markup}]{text}[/{markup}]" if markup else text)
			else:
				print(text)

	def line(self, message, style=None):
		"""A narrator line — target, counts, a note the operator should read."""
		self._print(message, style or "")

	def phase(self, title):
		"""A section header between the load's phases (lead, activity, call, file)."""
		with self.lock:
			if self.console is not None:
				self.console.rule(f"[bold]{title}", style=_DIM)
			else:
				print(f"\n=== {title} ===")

	def event(self, source, endpoint, status, ms, ok, detail=""):
		"""One wire call. Streams only in verbose mode; the summary carries the aggregate either way.

		`source` is api / lsq / file (colours the row); `detail` is the action, error code, or row count.
		"""
		if not self.verbose:
			return
		src = _SRC.get(source, "white")
		mark = "ok " if ok else "ERR"
		colour = _OK if ok else _FAIL
		with self.lock:
			if self.console is not None:
				self.console.print(
					f"[{src}]{source:<4}[/{src}] [{colour}]{mark}[/{colour}] "
					f"{endpoint:<34} [{_DIM}]{status:>3}  {ms:6.0f}ms[/{_DIM}]  {detail}")
			else:
				print(f"{source:<4} {mark} {endpoint:<34} {status:>3} {ms:6.0f}ms  {detail}")

	def progress(self, phase, done, total, extra=""):
		"""A single progress line, printed periodically by the loader (safe under verbose streaming)."""
		self._print(f"  {phase}: {done}/{total}  {extra}".rstrip(), _DIM)

	def summary(self, title, rows, note=""):
		"""The end-of-run table: one row per (label, value). `rows` is a list of (label, value) pairs."""
		with self.lock:
			if self.console is not None:
				table = Table(title=title, show_header=False, title_style="bold", border_style=_DIM)
				table.add_column("k", style="white")
				table.add_column("v", justify="right", style="bold")
				for label, value in rows:
					table.add_row(str(label), str(value))
				self.console.print(table)
				if note:
					self.console.print(f"[{_DIM}]{note}[/{_DIM}]")
			else:
				print(f"\n{title}")
				for label, value in rows:
					print(f"  {label:<24} {value}")
				if note:
					print(f"  {note}")
