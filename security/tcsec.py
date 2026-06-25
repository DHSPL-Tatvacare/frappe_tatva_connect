#!/usr/bin/env python3
"""tcsec — the unified security + test harness for frappe_tatva_connect.

ONE flag-driven CLI that runs every static security scan AND the runtime Frappe suite from a
single isolated pyenv venv, with a rich at-a-glance UI. See security/README.md for install/usage.

  Static (offline, runs in the venv):
    lint     ruff    — lint + format check (quality gate; bandit stays the SAST authority)
    sast     bandit  — Python SAST (AST scan of shipped app code)
    deps     pip-audit — dependency CVE / advisory scan
    secrets  detect-secrets — secret-leak scan vs baseline (repo is public)
    semgrep  semgrep — pattern SAST incl. custom Frappe-aware rules
    locks    pytest  — the decoupled AST source-locks (no bench needed)

  Runtime / active (need the devbench container or a running app):
    runtime  bench run-tests via `docker exec` into the devbench
    dast     sqlmap  — active SQLi probe of LIVE endpoints (devbench ONLY; see safety guard)

  Aggregates:
    static = lint + sast + deps + secrets + semgrep + locks   (all offline)
    all    = static + runtime

UI: each step shows a spinner while running, then a color-coded PASS/FAIL line with parsed stats;
findings are shown in an output panel; a summary table closes the run. Two opt-in side outputs:
`--ai-explain` pipes results to the local `claude -p` CLI (your existing login — no API key, no
network call we manage) for a plain-English analysis; `--pdf` renders a branded report. Exit code
is non-zero if any run step failed, or if a requested side output couldn't be produced — CI-ready.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import tomllib
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parent.parent
# sys.prefix is the active venv root; its bin/ holds the console scripts. (Do NOT derive this from
# sys.executable + .resolve() — a pyenv-virtualenv python is a symlink back to the base install,
# whose bin/ lacks the venv-only tools.)
VENV_BIN = Path(sys.prefix) / "bin"
SEMGREP_RULES = REPO_ROOT / "security" / "semgrep-rules"
SECRETS_BASELINE = REPO_ROOT / "security" / ".secrets.baseline"

# Hosts sqlmap (active attack) is allowed to point at without an explicit authorization override.
DEVBENCH_HOSTS = ("localhost", "dev.localhost", "127.0.0.1")
# Steps whose raw output may contain secret VALUES — never forwarded to `claude -p` by --explain.
SENSITIVE_STEPS = {"secrets", "dast"}
MAX_PANEL_CHARS = 6000  # cap shown output (and per-step text sent to --explain) to bound noise/tokens

# PDF generation — ALL generated/installed artifacts live in this gitignored working dir.
PDF_DIR = REPO_ROOT / "custom-user-work" / "security" / "pdf"
BROWSERS_DIR = PDF_DIR / ".browsers"  # playwright chromium installs here (PLAYWRIGHT_BROWSERS_PATH)
DEFAULT_PDF = REPO_ROOT / "custom-user-work" / "security" / "reports" / "tcsec-report.pdf"
LOGO_SRC = Path.home() / ".claude" / "skills" / "research-company" / "assets" / "tatvacare-logo.svg"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

console = Console()


# ── plumbing ──────────────────────────────────────────────────────────────────────────────


def tool(name: str) -> str:
	"""Absolute path to a venv tool, so tcsec works whether or not the venv is activated."""
	p = VENV_BIN / name
	if p.exists():
		return str(p)
	found = shutil.which(name)
	if not found:
		sys.exit(f"tcsec: '{name}' not found — install: pip install -r security/requirements.txt")
	return found


def run(label: str, cmd: list[str], outdir: Path, args, *, cwd: Path = REPO_ROOT) -> dict:
	"""Run one step: spinner while running, capture output to <outdir>/<label>.log, render a
	color-coded result line + a findings panel. Returns a result dict (label/rc/dur/stats/output)."""
	outdir.mkdir(parents=True, exist_ok=True)
	log = outdir / f"{label}.log"
	start = time.monotonic()
	if args.stream:  # live output, no spinner — for long runs you want to watch (e.g. runtime)
		console.print(f"[bold cyan]▶ {label}[/]  [dim]{' '.join(cmd)}[/]")
		proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
		console.print(proc.stdout, end="")
	else:
		with console.status(f"[bold cyan]{label}[/] running…", spinner="dots"):
			proc = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)
	dur = time.monotonic() - start
	output = (proc.stdout or "") + (proc.stderr or "")
	log.write_text(f"$ {' '.join(cmd)}\n\n{output}")

	stats = parse_stats(label, output, proc.returncode)
	ok = proc.returncode == 0
	icon, color = ("✓", "green") if ok else ("✗", "red")
	rel = log.relative_to(REPO_ROOT)
	console.print(
		f"[{color}]{icon} {label:<8}[/] {stats}  [dim]{dur:.1f}s · {rel}[/]"
	)
	if not ok and not args.quiet:
		shown = output.strip()[-MAX_PANEL_CHARS:]
		if len(output.strip()) > MAX_PANEL_CHARS:
			shown = f"… (truncated — full log: {rel})\n{shown}"
		console.print(Panel(shown or "(no output)", title=f"{label} findings", border_style=color, expand=True))
	return {"label": label, "rc": proc.returncode, "dur": dur, "stats": stats, "output": output}


# ── per-tool stat parsers (best-effort; fall back to a generic verdict) ──────────────────────


def parse_stats(label: str, out: str, rc: int) -> str:
	try:
		if label == "lint":
			errs = re.search(r"Found (\d+) error", out)
			fmt = re.search(r"(\d+) files? would be reformatted", out)
			if rc == 0:
				return "[green]clean[/]"
			bits = []
			if errs:
				bits.append(f"{errs.group(1)} lint")
			if fmt:
				bits.append(f"{fmt.group(1)} unformatted")
			return ", ".join(bits) or "issues"
		if label == "sast":
			sev = {s: len(re.findall(rf"Severity: {s}", out)) for s in ("High", "Medium", "Low")}
			total = sum(sev.values())
			return f"{total} findings ([red]{sev['High']}H[/] {sev['Medium']}M {sev['Low']}L)" if total else "[green]0 findings[/]"
		if label == "deps":
			if "No known vulnerabilities found" in out:
				return "[green]0 vulns[/]"
			n = len(re.findall(r"^\S+\s+\S+\s+\S+", out, re.M))
			return f"{n or '?'} vulnerable"
		if label == "secrets":
			if rc == 0:
				return "[green]no new secrets[/]"
			n = len(re.findall(r"Secret Type:", out))
			return f"[red]{n or '?'} potential secret(s)[/]"
		if label == "semgrep":
			m = re.search(r"(\d+) findings?", out)
			return "[green]0 findings[/]" if (m and m.group(1) == "0") or rc == 0 else f"{m.group(1) if m else '?'} findings"
		if label == "locks":
			p = re.search(r"(\d+) passed", out)
			f = re.search(r"(\d+) failed", out)
			if f:
				return f"[red]{f.group(1)} failed[/], {p.group(1) if p else 0} passed"
			return f"[green]{p.group(1) if p else '?'} passed[/]"
		if label == "runtime":
			ran = re.findall(r"Ran (\d+) tests?", out)
			fails = re.findall(r"FAILED|errors=\d", out)
			total = sum(int(x) for x in ran) if ran else "?"
			return f"[red]failures[/] in {total} tests" if (rc != 0 or fails) else f"[green]{total} tests OK[/]"
		if label == "dast":
			if re.search(r"is vulnerable|sqlmap identified", out, re.I):
				return "[red]injection found[/]"
			return "[green]no injection[/]" if rc == 0 else "see log"
	except Exception:
		pass
	return "[green]ok[/]" if rc == 0 else f"rc={rc}"


# ── steps ─────────────────────────────────────────────────────────────────────────────────


def step_lint(args, outdir: Path) -> dict:
	# Two ruff invocations folded into one step: lint check + format check (both non-mutating).
	chk = run("lint", [tool("ruff"), "check", *args.app], outdir, args)
	if chk["rc"] == 0:  # only run the format check if lint passed, to keep one verdict line clean
		fmt = subprocess.run([tool("ruff"), "format", "--check", *args.app], cwd=str(REPO_ROOT), text=True, capture_output=True)
		if fmt.returncode != 0:
			chk["rc"] = fmt.returncode
			chk["stats"] = parse_stats("lint", fmt.stdout + fmt.stderr, fmt.returncode)
			console.print(f"[red]✗ lint[/]     {chk['stats']}  [dim](ruff format --check)[/]")
	return chk


def step_sast(args, outdir: Path) -> dict:
	cmd = [tool("bandit"), "-r", *args.app, "-c", "pyproject.toml"]
	if args.format == "json":
		cmd += ["-f", "json", "-o", str(outdir / "bandit.json")]
	return run("sast", cmd, outdir, args)


def step_deps(args, outdir: Path) -> dict:
	if args.in_container:
		freeze = subprocess.run(
			["docker", "exec", args.container, "bash", "-lc", f"cd {args.bench_path} && ./env/bin/python -m pip freeze"],
			capture_output=True, text=True,
		)
		if freeze.returncode != 0:
			console.print(f"[red]✗ deps[/]     container freeze failed: {freeze.stderr.strip()[:200]}")
			return {"label": "deps", "rc": freeze.returncode, "dur": 0, "stats": "container error", "output": freeze.stderr}
		reqs = outdir / "bench-freeze.txt"
		reqs.write_text(freeze.stdout)
	else:
		meta = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
		deps = meta.get("project", {}).get("dependencies", [])
		reqs = outdir / "declared-deps.txt"
		reqs.write_text("\n".join(deps) + "\n")
	cmd = [tool("pip-audit"), "-r", str(reqs)]
	if args.format == "json":
		cmd += ["-f", "json", "-o", str(outdir / "pip-audit.json")]
	return run("deps", cmd, outdir, args)


def step_secrets(args, outdir: Path) -> dict:
	if not SECRETS_BASELINE.exists():
		console.print(f"[red]✗ secrets[/]  no baseline at {SECRETS_BASELINE} — run: tcsec secrets-baseline")
		return {"label": "secrets", "rc": 1, "dur": 0, "stats": "no baseline", "output": ""}
	tracked = subprocess.run(["git", "ls-files"], cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.split()
	cmd = [tool("detect-secrets-hook"), "--baseline", str(SECRETS_BASELINE), *tracked]
	return run("secrets", cmd, outdir, args)


def step_semgrep(args, outdir: Path) -> dict:
	cmd = [tool("semgrep"), "scan", "--error", "--metrics", "off"]
	for cfg in args.semgrep_config:
		cmd += ["--config", cfg]
	if args.format == "json":
		cmd += ["--json", "--output", str(outdir / "semgrep.json")]
	cmd += list(args.app)
	return run("semgrep", cmd, outdir, args)


def _static_lock_files() -> list[str]:
	"""Auto-discover the decoupled AST locks: test files carrying the standalone fallback marker."""
	hits = []
	for p in sorted((REPO_ROOT / "tatva_connect").rglob("test_*.py")):
		if "FrappeTestCase = unittest.TestCase" in p.read_text(encoding="utf-8"):
			hits.append(str(p.relative_to(REPO_ROOT)))
	return hits


def step_locks(args, outdir: Path) -> dict:
	files = _static_lock_files()
	if not files:
		console.print("[red]✗ locks[/]    no decoupled AST locks found")
		return {"label": "locks", "rc": 1, "dur": 0, "stats": "none found", "output": ""}
	junit = ["--junit-xml", str(outdir / "locks-junit.xml")] if args.format == "json" else []
	return run("locks", [tool("pytest"), "-q", *junit, *files], outdir, args)


def step_runtime(args, outdir: Path) -> dict:
	apps = " ".join(f"--app {a}" for a in args.app)
	script = f"cd {args.bench_path} && bench --site {args.site} run-tests {apps}"
	return run("runtime", ["docker", "exec", args.container, "bash", "-lc", script], outdir, args)


def step_dast(args, outdir: Path) -> dict:
	from urllib.parse import urlparse

	if not args.target:
		sys.exit("tcsec dast: --target URL is required (e.g. http://dev.localhost/api/method/...)")
	host = (urlparse(args.target).hostname or "").lower()
	if host not in DEVBENCH_HOSTS and not args.i_have_authorization:
		sys.exit(
			f"tcsec dast: REFUSING active attack on non-devbench host '{host}'. sqlmap fires real "
			f"injection payloads. Allowed: {DEVBENCH_HOSTS}. Override only for an authorized target "
			f"with --i-have-authorization (NEVER prod)."
		)
	cmd = [
		tool("sqlmap"), "-u", args.target, "--batch",
		f"--level={args.level}", f"--risk={args.risk}", "--output-dir", str(outdir / "sqlmap"),
	]
	if args.cookie:
		cmd += ["--cookie", args.cookie]
	if args.data:
		cmd += ["--data", args.data]
	return run("dast", cmd, outdir, args)


def step_secrets_baseline(args, outdir: Path) -> int:
	"""Generate/refresh security/.secrets.baseline from the tracked tree."""
	tracked = subprocess.run(["git", "ls-files"], cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.split()
	out = subprocess.run([tool("detect-secrets"), "scan", *tracked], cwd=str(REPO_ROOT), capture_output=True, text=True)
	if out.returncode != 0:
		console.print(out.stderr)
		return out.returncode
	SECRETS_BASELINE.write_text(out.stdout)
	console.print(f"[green]wrote[/] {SECRETS_BASELINE.relative_to(REPO_ROOT)} — REVIEW before committing.")
	return 0


STATIC_STEPS = [step_lint, step_sast, step_deps, step_secrets, step_semgrep, step_locks]


# ── --explain via the local `claude -p` CLI (no API key, no network call we manage) ──────────

# Shared project context prepended to every per-command prompt, so claude judges findings against
# THIS app's invariants (it is public; it deliberately interpolates CONSTANT SQL identifiers while
# binding all values; it names Frappe `password` fields; tests carry deliberate attack payloads).
_PROJECT = (
	"Context: this is `tatva_connect`, a PUBLIC Frappe v15 CRM app (WhatsApp/telephony/intake). "
	"Known, intentional patterns that often read as false positives: (a) raw SQL that interpolates "
	"a CONSTANT table/column identifier fixed in code while binding every VALUE as a %(name)s param "
	"(allowed, marked `# sqli-ok:`); (b) Frappe Password-type fields literally named 'password'; "
	"(c) test files under tests/ hold deliberate injection/secret payloads as corpora."
)

# Per-command framing: what the tool is + how to read its output. Keeps each prompt clean & focused.
_TOOL_CONTEXT = {
	"lint": "ruff lint + format check. Findings are code-quality/style, NOT security. Flag only what blocks correctness.",
	"sast": "bandit Python SAST. B608=hardcoded_sql (judge vs the constant-identifier pattern), B105/106/107=hardcoded password (judge vs Frappe field names), B113=request without timeout, B110/112=try/except pass/continue.",
	"deps": "pip-audit dependency CVE scan. Each row is a vulnerable package@version with an advisory ID and fixed version.",
	"semgrep": "semgrep with custom Frappe rules + p/python. frappe-raw-sql-interpolation and frappe-ignore-permissions fire on the intentional patterns above — judge carefully.",
	"locks": "pytest AST source-locks (no-SQLi / no-perm-bypass). A failure means a real new violation was planted in the source.",
	"runtime": "Frappe bench run-tests suite. Failures are real test regressions.",
}


def _analyze_one(claude: str, r: dict) -> str | None:
	"""Run ONE clean, per-command prompt through `claude -p`; return the analysis text (or None)."""
	label, rc = r["label"], r["rc"]
	if label in SENSITIVE_STEPS:
		# Never forward raw secret/dast output off-box — send the verdict only.
		body = (
			f"The `{label}` step finished with rc={rc} and verdict: {r['stats']}. Its raw output is "
			f"withheld because it can contain secret values. Explain what rc={rc} means for this step "
			f"and the exact next action the operator should take."
		)
	else:
		ctx = _TOOL_CONTEXT.get(label, "")
		tail = r["output"].strip()[-MAX_PANEL_CHARS:] or "(no output)"
		body = f"Tool `{label}`: {ctx}\nExit code: {rc} (0 = clean).\n\nRaw output (most recent {MAX_PANEL_CHARS} chars):\n{tail}"
	prompt = (
		f"{_PROJECT}\n\nYou are a security analyst reviewing ONE tool's output. {body}\n\n"
		f"Respond ONLY about `{label}`, in three short parts: (1) one-line verdict; (2) real issues "
		f"to fix, ranked (or 'none'); (3) likely false positives given the project context. Be concise."
	)
	with console.status(f"[bold]claude · {label}[/] analyzing…", spinner="dots"):
		proc = subprocess.run([claude, "-p", prompt], text=True, capture_output=True)
	return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def gather_analyses(results: list[dict], args, *, to_console: bool) -> dict[str, str | None]:
	"""Per-command `claude -p` analysis. Used by --explain (console) and --pdf (report)."""
	claude = shutil.which(args.claude_bin or "claude")
	if not claude:  # normally caught by preflight; defensive guard
		if to_console:
			console.print("[yellow]analysis skipped:[/] `claude` CLI not found (install Claude Code & log in).")
		return {}
	if to_console:
		console.print("\n[bold]━━ analysis (claude -p, per command) ━━[/]")
	out: dict[str, str | None] = {}
	for r in results:
		text = _analyze_one(claude, r)
		out[r["label"]] = text
		if to_console:
			if text:
				console.print(Panel(text, title=f"claude · {r['label']}", border_style="cyan", expand=True))
			else:
				console.print(f"[yellow]analysis {r['label']} unavailable[/]")
	return out


# ── PDF report (branded HTML → playwright → PDF; all artifacts in the gitignored PDF_DIR) ────

_PDF_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
:root{--cream:#FAF6F1;--white:#fff;--coral:#D97757;--coral-dark:#C4623F;--coral-light:#F5E6DE;
--text:#1F1F1F;--text-2:#525252;--text-3:#8B8B8B;--border:#E5DDD5;--surface:#F9F5F0;
--green-bg:#E8F5E9;--green:#2E7D32;--red-bg:#FDECEA;--red:#C0392B;}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Inter',sans-serif;font-size:9pt;line-height:1.5;color:var(--text);background:var(--white);
-webkit-print-color-adjust:exact;print-color-adjust:exact;}
@page{size:A4;margin:14mm 18mm;}
.content{width:210mm;margin:0 auto;}
.banner{background:linear-gradient(135deg,#D97757 0%,#C4623F 50%,#A84E32 100%);padding:16px 28px;
display:flex;align-items:center;border-radius:8px;margin-bottom:16px;}
.banner-left{width:120px;flex-shrink:0;}
.banner-logo-light{filter:brightness(0) invert(1);}
.banner-logo-light svg{height:30px;width:auto;}
.banner-title{text-align:center;flex:1;display:flex;flex-direction:column;align-items:center;}
.banner-title h1{font-size:17pt;font-weight:700;color:#fff;letter-spacing:-0.3px;}
.banner-sub{font-size:7.5pt;color:rgba(255,255,255,.85);font-weight:500;letter-spacing:1.5px;text-transform:uppercase;}
.banner-right{width:120px;flex-shrink:0;}
.sh{break-after:avoid;display:flex;align-items:center;gap:8px;margin:18px 0 8px;padding-bottom:6px;
border-bottom:1.5px solid var(--coral);}
.sh .num{background:var(--coral);color:#fff;width:22px;height:22px;border-radius:5px;display:flex;
align-items:center;justify-content:center;font-weight:700;font-size:10pt;}
.sh h2{font-size:12pt;font-weight:700;}
.sh .tag-co{margin-left:auto;background:var(--coral-light);color:var(--coral-dark);padding:2px 10px;
border-radius:12px;font-size:7pt;font-weight:600;text-transform:uppercase;letter-spacing:.8px;}
h3{break-after:avoid;font-size:9.5pt;font-weight:700;color:var(--coral-dark);margin:12px 0 5px;
padding-left:8px;border-left:2.5px solid var(--coral);display:flex;align-items:center;gap:8px;}
.mg{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:6px 0 10px;break-inside:avoid;}
.mc{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center;}
.mv{font-size:18pt;font-weight:700;color:var(--coral-dark);line-height:1.1;}
.mv.g{color:var(--green);} .mv.r{color:var(--red);}
.ml{font-size:7pt;color:var(--text-3);font-weight:500;margin-top:2px;text-transform:uppercase;letter-spacing:.5px;}
.tag{display:inline-block;padding:1px 8px;border-radius:8px;font-size:7pt;font-weight:700;letter-spacing:.5px;}
.tg{background:var(--green-bg);color:var(--green);} .tr{background:var(--red-bg);color:var(--red);}
p{margin:4px 0;font-size:8.5pt;} strong{font-weight:700;color:var(--text);}
pre.out{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:8px 10px;
font-family:'SFMono-Regular',Consolas,monospace;font-size:7pt;white-space:pre-wrap;word-break:break-word;
color:var(--text-2);margin:6px 0;}
.src{font-size:7.5pt;color:var(--text-3);margin:4px 0;}
.an{margin:4px 0 10px;}
"""


def _strip_ansi(s: str) -> str:
	return _ANSI.sub("", s)


def _plain(s: str) -> str:
	"""Strip rich console markup (e.g. [green]…[/]) for plain PDF text."""
	return re.sub(r"\[/?[a-z0-9_ #]*\]", "", s).strip()


def _md(text: str | None) -> str:
	"""Minimal markdown → HTML for claude's analysis text (bold + paragraphs)."""
	t = html.escape(text or "")
	t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
	paras = [p.strip() for p in t.split("\n\n") if p.strip()]
	return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras) or "<p>(no analysis)</p>"


def _tag(ok: bool) -> str:
	return f'<span class="tag {"tg" if ok else "tr"}">{"PASS" if ok else "FAIL"}</span>'


def build_html(results: list[dict], analyses: dict, args) -> str:
	date = datetime.now().strftime("%d %b %Y · %H:%M")
	logo = LOGO_SRC.read_text() if LOGO_SRC.exists() else ""
	passed = sum(1 for r in results if r["rc"] == 0)
	failed = len(results) - passed

	# Section 1: Claude analysis FIRST (per command), then Section 2: per-command details.
	analysis_html, n = "", 1
	if analyses:
		items = "".join(
			f'<h3>{r["label"]} {_tag(r["rc"] == 0)}</h3><div class="an">{_md(analyses.get(r["label"]))}</div>'
			for r in results
		)
		analysis_html = f'<div class="sh"><div class="num">{n}</div><h2>Analysis</h2><span class="tag-co">claude</span></div>{items}'
		n += 1

	det = ""
	for r in results:
		ok = r["rc"] == 0
		out = _strip_ansi(r["output"]).strip()[-MAX_PANEL_CHARS * 2:]
		body = f'<pre class="out">{html.escape(out)}</pre>' if (out and not ok) else (
			'<p class="src">No findings.</p>' if ok else '<p class="src">(no output)</p>'
		)
		det += f'<h3>{r["label"]} {_tag(ok)}<span class="src" style="margin-left:auto">{_plain(r["stats"])} · {r["dur"]:.1f}s</span></h3>{body}'
	details_html = f'<div class="sh"><div class="num">{n}</div><h2>Results detail</h2><span class="tag-co">{", ".join(args.app)}</span></div>{det}'

	return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<title>tcsec security report</title><style>{_PDF_CSS}</style></head><body><div class="content">
<div class="banner">
  <div class="banner-left"><span class="banner-logo-light">{logo}</span></div>
  <div class="banner-title"><h1>Security &amp; Test Report</h1>
    <div class="banner-sub">tatva_connect &bull; {date}</div></div>
  <div class="banner-right"></div>
</div>
<div class="mg">
  <div class="mc"><div class="mv">{len(results)}</div><div class="ml">Checks run</div></div>
  <div class="mc"><div class="mv g">{passed}</div><div class="ml">Passed</div></div>
  <div class="mc"><div class="mv r">{failed}</div><div class="ml">Findings / fails</div></div>
</div>
{analysis_html}
{details_html}
</div></body></html>"""


def ensure_chromium() -> str | None:
	"""Make chromium available in the gitignored browsers dir. Return None on success, or a clear
	error string if it's missing and can't be installed (e.g. offline)."""
	os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
	if any(BROWSERS_DIR.glob("chromium-*")):
		return None
	console.print("[dim]installing chromium into the gitignored browsers dir (one-time, ~120 MB)…[/]")
	try:
		r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
		                   env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_DIR)},
		                   capture_output=True, text=True)
		detail = r.stderr.strip()[:300] if r.returncode != 0 else ""
		ok = r.returncode == 0 and any(BROWSERS_DIR.glob("chromium-*"))
	except Exception as e:  # bad interpreter, perms, etc. — never crash the harness
		ok, detail = False, str(e)
	if not ok:
		return (f"chromium is not installed and the install failed (offline?). Install manually:\n"
		        f"  PLAYWRIGHT_BROWSERS_PATH={BROWSERS_DIR} {sys.executable} -m playwright install chromium\n"
		        f"  {detail}")
	return None


def render_pdf(html_str: str, pdf_path: Path) -> None:
	"""Render HTML → PDF via playwright chromium (browsers + working HTML in the gitignored PDF_DIR).
	Chromium is ensured by preflight; here we render and ALWAYS clean up the intermediate HTML and
	the browser, even on error or Ctrl-C."""
	PDF_DIR.mkdir(parents=True, exist_ok=True)
	pdf_path.parent.mkdir(parents=True, exist_ok=True)
	os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
	html_path = PDF_DIR / "report.html"
	html_path.write_text(html_str)
	from playwright.sync_api import sync_playwright

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch()
			try:
				page = browser.new_page()
				page.goto(html_path.as_uri(), wait_until="load")
				try:  # let webfonts settle; fine to skip if offline
					page.wait_for_load_state("networkidle", timeout=5000)
				except Exception:
					pass
				page.pdf(path=str(pdf_path), format="A4", print_background=True,
				         margin={"top": "0mm", "right": "0mm", "bottom": "0mm", "left": "0mm"})
			finally:
				browser.close()
	finally:
		html_path.unlink(missing_ok=True)  # clean up the intermediate HTML


def preflight(args) -> list[str]:
	"""Validate prerequisites for the OPT-IN side outputs BEFORE running any scan, so a missing
	`claude` CLI or chromium fails the command fast with a clear message instead of after the work."""
	errs: list[str] = []
	if args.ai_explain and not shutil.which(args.claude_bin or "claude"):
		errs.append("--ai-explain needs the `claude` CLI (install Claude Code and log in), "
		            "or pass --claude-bin PATH to an executable.")
	if args.pdf is not None:
		if importlib.util.find_spec("playwright") is None:
			errs.append("--pdf needs playwright — run: pip install -r security/requirements.txt")
		else:
			err = ensure_chromium()
			if err:
				errs.append(err)
	return errs


def _pdf_path(args) -> Path:
	if args.pdf == "__default__":
		return DEFAULT_PDF
	p = Path(args.pdf)
	return p if p.is_absolute() else (REPO_ROOT / p)


def finalize(results: list[dict], args) -> int:
	"""Post-run side outputs — both fully opt-in: claude analysis (--ai-explain) and/or a branded
	PDF (--pdf). The PDF includes the analysis section ONLY when --ai-explain is also passed.
	Prerequisites are validated in preflight; a failure here returns non-zero so a requested output
	that couldn't be produced fails the command (it never flips a scan's own pass/fail)."""
	rc = 0
	analyses: dict = {}
	if args.ai_explain:
		analyses = gather_analyses(results, args, to_console=True)
	if args.pdf is not None:
		path = _pdf_path(args)
		try:
			render_pdf(build_html(results, analyses, args), path)
			rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
			console.print(f"[green]PDF:[/] {rel}")
		except Exception as e:
			console.print(f"[red]PDF generation failed:[/] {e}")
			rc = 3
	return rc


# ── aggregate + summary ─────────────────────────────────────────────────────────────────────


def run_many(steps, args, outdir: Path) -> int:
	results = []
	for fn in steps:
		r = fn(args, outdir)
		results.append(r)
		if r["rc"] != 0 and args.fail_fast:
			break
	table = Table(title="tcsec summary", title_style="bold", expand=False)
	table.add_column("step", style="bold")
	table.add_column("result")
	table.add_column("stats")
	table.add_column("time", justify="right")
	worst = 0
	for r in results:
		ok = r["rc"] == 0
		table.add_row(
			r["label"],
			"[green]PASS[/]" if ok else f"[red]FAIL[/] (rc={r['rc']})",
			r["stats"],
			f"{r['dur']:.1f}s",
		)
		worst = worst or r["rc"]
	console.print(table)
	side = finalize(results, args)  # always run side outputs, even when scans found issues
	return worst or side


# ── cli ───────────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
	"""Standard `tcsec <command> [flags]` CLI: shared flags via parent parsers, command-specific
	flags on their own subparser, neat per-command help."""
	p = argparse.ArgumentParser(
		prog="tcsec",
		description="Unified security + test harness for tatva_connect (run `tcsec <command> -h` for flags).",
		epilog=(
			"examples:\n"
			"  tcsec static                          offline scans, at-a-glance\n"
			"  tcsec static --pdf --ai-explain       + claude analysis, + branded PDF\n"
			"  tcsec all --container frappedev-frappe-1   static + bench run-tests\n"
			"  tcsec dast --target http://dev.localhost/api/method/x?id=1 --cookie 'sid=...'\n"
		),
		formatter_class=argparse.RawDescriptionHelpFormatter,
	)
	# Defaults for command-specific flags so aggregates (static/all) never AttributeError when a
	# step reads a flag that only lives on another command's subparser.
	p.set_defaults(in_container=False, semgrep_config=None, target=None, cookie=None,
	               data=None, level=1, risk=1, i_have_authorization=False)

	common = argparse.ArgumentParser(add_help=False)
	g = common.add_argument_group("common options")
	g.add_argument("--app", action="append", metavar="DIR", help="app/dir to scan (repeatable; default tatva_connect)")
	g.add_argument("--format", choices=["txt", "json"], default="txt", help="also emit machine-readable reports")
	g.add_argument("--output-dir", metavar="DIR", default="custom-user-work/security/reports", help="report output dir")
	g.add_argument("--fail-fast", action="store_true", help="stop at the first failing step")
	g.add_argument("--stream", action="store_true", help="stream tool output live (no spinner)")
	g.add_argument("--quiet", action="store_true", help="stats only; suppress findings panels")
	g.add_argument("--ai-explain", action="store_true", help="analyze results with the local `claude -p` CLI")
	g.add_argument("--pdf", nargs="?", const="__default__", metavar="PATH",
	               help="branded PDF report (add --ai-explain to include the analysis section)")
	g.add_argument("--claude-bin", metavar="PATH", help="path to the claude CLI (default: from PATH)")

	bench = argparse.ArgumentParser(add_help=False)
	gb = bench.add_argument_group("devbench options")
	gb.add_argument("--container", default="frappedev-frappe-1", help="devbench docker container")
	gb.add_argument("--site", default="dev.localhost", help="bench site")
	gb.add_argument("--bench-path", default="/workspace/development/frappe-bench", help="bench path in container")

	sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

	def cmd(name, help_, parents=(common,)):
		return sub.add_parser(name, help=help_, description=help_, parents=list(parents),
		                      formatter_class=argparse.ArgumentDefaultsHelpFormatter)

	cmd("lint", "ruff lint + format check (quality, not security)")
	cmd("sast", "bandit Python SAST over app code")
	pd = cmd("deps", "pip-audit dependency CVE scan", parents=(common, bench))
	pd.add_argument("--in-container", action="store_true", help="audit the real bench env (needs container)")
	cmd("secrets", "detect-secrets scan vs baseline")
	ps = cmd("semgrep", "semgrep pattern SAST + custom Frappe rules")
	ps.add_argument("--semgrep-config", action="append", metavar="CFG",
	                help="semgrep config (repeatable; default: local rules + p/python)")
	cmd("locks", "pytest AST source-locks (offline)")
	cmd("runtime", "Frappe bench run-tests via docker exec", parents=(common, bench))
	pdast = cmd("dast", "sqlmap active SQLi probe (devbench ONLY)")
	pdast.add_argument("--target", metavar="URL", help="target URL (required)")
	pdast.add_argument("--cookie", help="session cookie header, e.g. 'sid=...'")
	pdast.add_argument("--data", help="POST body to fuzz")
	pdast.add_argument("--level", type=int, default=1, help="sqlmap --level")
	pdast.add_argument("--risk", type=int, default=1, help="sqlmap --risk")
	pdast.add_argument("--i-have-authorization", action="store_true", help="allow a non-devbench target (NEVER prod)")
	cmd("static", "lint + sast + deps + secrets + semgrep + locks (all offline)", parents=(common, bench))
	cmd("all", "static + runtime", parents=(common, bench))
	cmd("secrets-baseline", "(re)generate security/.secrets.baseline")
	return p


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	args.app = args.app or ["tatva_connect"]
	args.semgrep_config = args.semgrep_config or [str(SEMGREP_RULES), "p/python"]
	outdir = (REPO_ROOT / args.output_dir).resolve()

	if args.command == "secrets-baseline":
		return step_secrets_baseline(args, outdir)

	errs = preflight(args)  # fail fast & clear if an opt-in output's prerequisite is missing
	if errs:
		for e in errs:
			console.print(f"[red]error:[/] {e}")
		return 2

	singles = {
		"lint": step_lint, "sast": step_sast, "deps": step_deps, "secrets": step_secrets,
		"semgrep": step_semgrep, "locks": step_locks, "runtime": step_runtime, "dast": step_dast,
	}
	if args.command in singles:
		r = singles[args.command](args, outdir)
		side = finalize([r], args)  # always run side outputs, even when the scan found issues
		return r["rc"] or side
	if args.command == "static":
		return run_many(STATIC_STEPS, args, outdir)
	if args.command == "all":
		return run_many([*STATIC_STEPS, step_runtime], args, outdir)
	return 2


if __name__ == "__main__":
	try:
		sys.exit(main())
	except KeyboardInterrupt:
		console.print("\n[yellow]interrupted[/] — aborting; partial outputs (if any) were cleaned up.")
		sys.exit(130)
	except BrokenPipeError:
		sys.exit(0)
