"""src/security_scan.py — static, table-driven security pre-scan for
third-party content Faustus is about to trust: an MCP server's command/args/
local script and the tool descriptions it advertises once connected, and an
imported skill's folder. Runs BEFORE the user approves/enables either one
(``routes/mcp/mcp_routes.py``'s manifest/approve flow, ``src.skill_import_
review``'s approve flow) — never after.

Deliberately dumb: no network call, no LLM, no code execution. It is a table
of regexes with a severity each, matched against text, plus a couple of
proximity checks (e.g. "was there an `eval(` near this `atob(`") that a
single regex cannot express cleanly. That is the whole design — a scanner
that could itself be tricked into running something is worse than none.

Nothing here blocks anything by itself. It produces a `ScanResult` the
caller attaches to whatever the human is about to review; the one place that
turns "critical findings" into a hard stop is the approval route, gated by
the `security_scan_block_critical` setting (default True) and only when the
caller does not pass an explicit override.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Finding", "ScanResult", "SEVERITY_WEIGHT", "SEVERITY_ORDER",
    "scan_text", "scan_paths", "mask_secrets", "risk_level_for_score",
    "combine_results",
]

# ── severities ───────────────────────────────────────────────────────────

SEVERITY_ORDER: Tuple[str, ...] = ("critical", "high", "medium", "low")
SEVERITY_WEIGHT: Dict[str, int] = {"critical": 40, "high": 22, "medium": 10, "low": 4}

# ── secret masking (applied to every snippet before it leaves this module) ─

_SECRET_PATTERNS: Tuple[re.Pattern, ...] = tuple(re.compile(p) for p in (
    r"AKIA[0-9A-Z]{16}",                                   # AWS access key id
    r"sk-[A-Za-z0-9]{20,}",                                 # OpenAI-shaped key
    r"gh[pousr]_[A-Za-z0-9]{20,}",                           # GitHub token
    r"xox[baprs]-[A-Za-z0-9-]{10,}",                         # Slack token
    r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]*-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"][^'\"\s]{6,}['\"]",
    r"\b[A-Za-z0-9+/]{40,}={0,2}\b",                        # generic long base64/hex-ish blob
))


def mask_secrets(text: str) -> str:
    """Replace anything that looks like a credential with a fixed-width mask.
    Applied to every snippet before it is returned — a finding must never be
    the thing that leaks the secret it is warning about."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: "****" + (m.group(0)[-4:] if len(m.group(0)) > 4 else ""), out)
    return out


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _snippet(line: str, start_col: int, end_col: int, *, max_len: int = 160) -> str:
    line = _CTRL_RE.sub(" ", line).strip()
    if len(line) > max_len:
        # Keep the match roughly centered.
        lo = max(0, start_col - max_len // 2)
        line = ("…" if lo > 0 else "") + line[lo:lo + max_len] + "…"
    return mask_secrets(line)


# ── rules ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Rule:
    id: str
    category: str
    severity: str
    pattern: re.Pattern
    description: str
    # When set, the match is only kept if this second pattern is found
    # within `window` chars before or after the primary match — for rules
    # that need proximity ("a base64 blob THAT IS decoded and eval'd") that
    # a single regex expresses badly.
    requires_near: Optional[re.Pattern] = None
    window: int = 220
    # Categories/kinds this rule only fires for (e.g. prompt-injection rules
    # are pointless against a shell script). Empty = always applies.
    only_kinds: Tuple[str, ...] = ()


def _r(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern:
    return re.compile(pattern, flags)


RULES: Tuple[_Rule, ...] = (
    # ── remote script execution ────────────────────────────────────────
    _Rule("REMOTE_CURL_PIPE_SHELL", "remote_exec", "critical",
          _r(r"\bcurl\b[^\n|]{0,200}\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
          "Downloads a remote script and pipes it directly into a shell."),
    _Rule("REMOTE_WGET_PIPE_SHELL", "remote_exec", "critical",
          _r(r"\bwget\b[^\n|]{0,200}\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
          "Downloads a remote script and pipes it directly into a shell."),
    _Rule("REMOTE_IEX_DOWNLOAD", "remote_exec", "critical",
          _r(r"(invoke-expression|\biex\b)\s*\(?[^\n)]{0,160}(net\.webclient|invoke-webrequest|invoke-restmethod|downloadstring|\biwr\b|\birm\b)"),
          "PowerShell downloads content and evaluates it with Invoke-Expression."),
    _Rule("REMOTE_DOWNLOAD_PIPE_IEX", "remote_exec", "critical",
          _r(r"(invoke-webrequest|invoke-restmethod|\biwr\b|\birm\b|\bcurl\b|\bwget\b)[^\n|]{0,200}\|\s*(iex|invoke-expression)\b"),
          "Downloaded content is piped straight into Invoke-Expression."),
    _Rule("REMOTE_POWERSHELL_ENCODED", "remote_exec", "critical",
          _r(r"powershell(\.exe)?\b[^\n]{0,120}-(e|enc|encodedcommand)\b"),
          "Runs PowerShell with an opaque base64-encoded command."),

    # ── obfuscated payloads (proximity: decode → eval/exec) ─────────────
    _Rule("OBFUSC_B64_DECODE_EXEC", "obfuscated", "critical",
          _r(r"base64\.b64decode\("),
          "A base64 blob is decoded and then executed as code.",
          requires_near=_r(r"\b(eval|exec)\s*\("), window=200),
    _Rule("OBFUSC_ATOB_EVAL", "obfuscated", "critical",
          _r(r"\batob\("),
          "A base64 blob is decoded (atob) and then evaluated as JS.",
          requires_near=_r(r"\b(eval|new\s+Function)\s*\("), window=200),
    _Rule("OBFUSC_CHARCODE_BUILDER", "obfuscated", "high",
          _r(r"String\.fromCharCode\((?:\s*\d+\s*,){4,}"),
          "A string is assembled character-code by character-code (common "
          "obfuscation), and fed into eval.",
          requires_near=_r(r"\beval\s*\("), window=200),

    # ── credential access ────────────────────────────────────────────────
    _Rule("CRED_SSH_KEY", "credential_access", "high",
          _r(r"\.ssh[/\\](id_rsa|id_ed25519|id_ecdsa|id_dsa)\b"),
          "Reads a private SSH key file."),
    _Rule("CRED_AWS_CREDENTIALS", "credential_access", "high",
          _r(r"\.aws[/\\]credentials\b"),
          "Reads the local AWS credentials file."),
    _Rule("CRED_BROWSER_COOKIES", "credential_access", "high",
          _r(r"(user data[\\/].*[\\/]login data|library/application support/google/chrome/.*/cookies|"
             r"\.mozilla[\\/]firefox[\\/].*[\\/]cookies\.sqlite)"),
          "Reads a browser's saved-login or cookie database."),
    _Rule("CRED_WINDOWS_VAULT", "credential_access", "high",
          _r(r"\bvaultcmd\b|credenumeratew?\(|\bcmdkey\s+/list\b"),
          "Reads the Windows Credential Manager vault."),
    _Rule("CRED_MACOS_KEYCHAIN", "credential_access", "high",
          _r(r"security\s+find-generic-password|security\s+find-internet-password"),
          "Reads the macOS keychain."),
    _Rule("CRED_ENV_SECRET_DUMP", "credential_access", "medium",
          _r(r"(os\.environ|process\.env)\b"),
          "Reads the process environment, which may contain provider "
          "tokens/keys.",
          requires_near=_r(r"(_TOKEN|_KEY|_SECRET)\b"), window=120),

    # ── exfiltration ─────────────────────────────────────────────────────
    _Rule("EXFIL_DISCORD_WEBHOOK", "exfiltration", "high",
          _r(r"discord(app)?\.com/api/webhooks/"),
          "Posts data to a hard-coded Discord webhook URL."),
    _Rule("EXFIL_TELEGRAM_BOT", "exfiltration", "high",
          _r(r"api\.telegram\.org/bot"),
          "Posts data to a hard-coded Telegram bot API URL."),
    _Rule("EXFIL_PASTEBIN", "exfiltration", "medium",
          _r(r"pastebin\.com/(raw/)?[A-Za-z0-9]+"),
          "References a Pastebin paste — a common drop site for exfiltrated data."),
    _Rule("EXFIL_SECRET_TO_NETWORK", "exfiltration", "critical",
          _r(r"(_TOKEN|_KEY|_SECRET|_PASSWORD)\b"),
          "A token/key/secret name is read close to an outbound network call.",
          requires_near=_r(r"\b(requests\.(post|put)|fetch\(|http\.request|urlopen|"
                            r"new\s+XMLHttpRequest|Invoke-WebRequest|Invoke-RestMethod)\b"),
          window=250),
    _Rule("EXFIL_HARDCODED_IP_POST", "exfiltration", "medium",
          _r(r"https?://\d{1,3}(?:\.\d{1,3}){3}[:/]"),
          "Sends data to a hard-coded raw IP address rather than a named host.",
          requires_near=_r(r"\b(post|put|send|upload)\b"), window=160),

    # ── persistence ───────────────────────────────────────────────────────
    _Rule("PERSIST_WINDOWS_RUN_KEY", "persistence", "high",
          _r(r"(hkcu|hklm)\\software\\microsoft\\windows\\currentversion\\run\b|reg(\.exe)?\s+add[^\n]{0,80}\\run\b"),
          "Writes to the Windows Run registry key (auto-start on login)."),
    _Rule("PERSIST_SCHEDULED_TASK", "persistence", "high",
          _r(r"schtasks(\.exe)?\s+/create\b|new-scheduledtask\b"),
          "Creates a Windows scheduled task."),
    _Rule("PERSIST_STARTUP_FOLDER", "persistence", "high",
          _r(r"\\start menu\\programs\\startup\b|shell:startup"),
          "Drops a file in the Windows Startup folder."),
    _Rule("PERSIST_CRONTAB", "persistence", "high",
          _r(r"crontab\s+-e\b|crontab\s+-l[^\n]{0,80}crontab\s+-\b|/etc/cron\.(d|daily|hourly)"),
          "Installs a cron job for persistence."),
    _Rule("PERSIST_LAUNCH_AGENT", "persistence", "high",
          _r(r"~?/library/launchagents\b|/library/launchdaemons\b"),
          "Installs a macOS LaunchAgent/LaunchDaemon (auto-start)."),

    # ── destructive ───────────────────────────────────────────────────────
    _Rule("DESTRUCTIVE_RM_RF_ROOT", "destructive", "critical",
          _r(r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(\s|$|\*|['\"])"),
          "Recursively force-deletes the filesystem root."),
    _Rule("DESTRUCTIVE_FORMAT_DRIVE", "destructive", "critical",
          _r(r"\bformat\s+[a-z]:\s*(/[a-z]\b)?"),
          "Formats a Windows drive."),
    _Rule("DESTRUCTIVE_DEL_SYSTEM", "destructive", "critical",
          _r(r"\bdel\s+/s\s+/q\s+[a-z]:\\\\?windows\b"),
          "Recursively force-deletes the Windows system directory."),
    _Rule("DESTRUCTIVE_RMTREE_HOME", "destructive", "critical",
          _r(r"shutil\.rmtree\([^)]{0,120}(expanduser\(['\"]~['\"]\)|home\(\)|['\"]~['\"])"),
          "Recursively deletes the user's home directory."),

    # ── dynamic code from untrusted input ───────────────────────────────
    _Rule("DYNCODE_EVAL_NONLITERAL", "dynamic_code", "medium",
          _r(r"\beval\((?!\s*['\"])[^)]{2,}\)"),
          "eval() called with a non-literal (computed) argument."),
    _Rule("DYNCODE_EXEC_NONLITERAL", "dynamic_code", "medium",
          _r(r"(?<![.\w])exec\((?!\s*['\"])[^)]{2,}\)"),
          "exec() called with a non-literal (computed) argument."),
    _Rule("DYNCODE_NEW_FUNCTION", "dynamic_code", "medium",
          _r(r"new\s+Function\s*\("),
          "Builds and calls a function from a string at runtime."),
    _Rule("DYNCODE_CHILD_PROCESS_NONLITERAL", "dynamic_code", "high",
          _r(r"child_process\.(exec|spawn|execSync)\((?!\s*['\"])[^)]{2,}\)"),
          "Spawns a child process with a non-literal (computed) command."),

    # ── network listeners ────────────────────────────────────────────────
    _Rule("NETLISTEN_BIND_ALL", "network_listener", "medium",
          _r(r"(bind\(\(?['\"]0\.0\.0\.0['\"]|host\s*=\s*['\"]0\.0\.0\.0['\"]|listen\(\s*['\"]0\.0\.0\.0['\"])"),
          "Binds a network listener to all interfaces (0.0.0.0), not just localhost."),

    # ── prompt injection (text the model will read: tool descriptions, skills) ──
    _Rule("PROMPT_IGNORE_INSTRUCTIONS", "prompt_injection", "high",
          _r(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions\b"),
          "Text tries to override the model's prior instructions."),
    _Rule("PROMPT_HIDDEN_HTML_COMMENT", "prompt_injection", "high",
          _r(r"<!--[^>]{0,400}(instruction|system prompt|do not tell|ignore)[^>]{0,400}-->", re.IGNORECASE | re.DOTALL),
          "An HTML comment carries hidden instructions for the model."),
    _Rule("PROMPT_ZERO_WIDTH_CHARS", "prompt_injection", "medium",
          re.compile(r"[​‌‍]|(?<=[^\^])﻿"),
          "Zero-width characters — often used to hide text from a human "
          "reviewer while the model still reads it."),
    _Rule("PROMPT_DO_NOT_TELL_USER", "prompt_injection", "critical",
          _r(r"\bdo not (tell|inform|mention (this |it )?to) the user\b"),
          "Text explicitly instructs the model to keep something from the user."),
    _Rule("PROMPT_CALL_OTHER_TOOL", "prompt_injection", "medium",
          _r(r"\b(you must|always|first|silently)\s+call\s+the\s+['\"]?[\w.-]+['\"]?\s+tool\b"),
          "Text instructs the model to call a specific other tool — a "
          "classic tool-description injection vector."),
    _Rule("PROMPT_SEND_DATA_TO", "prompt_injection", "high",
          _r(r"\bsend\s+(this|the|all|your)\s+(data|contents|output|conversation|credentials|secrets)\s+to\b"),
          "Text instructs the model to send data somewhere."),
)


# ── markdown fenced-code context (for the install-guide lookalike case) ────

_FENCE_RE = re.compile(r"^\s*```")


def _fenced_line_numbers(text: str) -> set:
    """Line numbers (1-based) that fall inside a markdown ``` fenced block."""
    inside = False
    fenced = set()
    for i, line in enumerate(text.split("\n"), start=1):
        if _FENCE_RE.match(line):
            inside = not inside
            continue
        if inside:
            fenced.add(i)
    return fenced


# A benign-lookalike downgrade: `curl ... | sh` written in a markdown code
# block is documentation a human reads before deciding to run it themselves
# — real risk (copy-paste), but categorically lower than the same line
# inside a script that WILL execute unattended the moment this MCP server or
# skill runs. We downgrade remote_exec findings from critical to medium
# when they occur inside a fenced block of a markdown file, and leave every
# other category (a markdown file has no business containing a working
# rm -rf / or a real exfiltration one-liner either) at full severity.
_MARKDOWN_KINDS = frozenset({"markdown", "skill_file", "mcp_readme"})
_DOWNGRADE_IN_FENCE = frozenset({"remote_exec"})


@dataclass
class Finding:
    rule_id: str
    category: str
    severity: str
    file: str
    line: int
    snippet: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id, "category": self.category,
            "severity": self.severity, "file": self.file, "line": self.line,
            "snippet": self.snippet, "description": self.description,
        }


def risk_level_for_score(score: int) -> str:
    """Score-only fallback (kept for callers with no findings list handy).
    `_risk_level_for_findings` is what `scan_text`/`scan_paths`/
    `combine_results` actually use -- the presence of even one CRITICAL
    finding should always read as "critical", regardless of how the numeric
    score of that one finding compares to a threshold."""
    if score >= 70:
        return "critical"
    if score >= 40:
        return "high"
    if score >= 15:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _risk_level_for_findings(findings: Sequence["Finding"]) -> str:
    for sev in SEVERITY_ORDER:  # critical, high, medium, low -- most severe first
        if any(f.severity == sev for f in findings):
            return sev
    return "none"


@dataclass
class ScanResult:
    findings: List[Finding] = field(default_factory=list)
    risk_score: int = 0
    risk_level: str = "none"
    files_scanned: int = 0
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        by_sev: Dict[str, int] = {s: 0 for s in SEVERITY_ORDER}
        for f in self.findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        return {
            "findings": [f.to_dict() for f in self.findings],
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "counts": by_sev,
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }

    def has_critical(self) -> bool:
        return any(f.severity == "critical" for f in self.findings)


def _score(findings: Sequence[Finding]) -> int:
    # Monotonic: one contribution per distinct rule id, using the highest
    # severity that rule fired at (a rule's severity is otherwise constant,
    # except for the markdown fence downgrade above). Summed and capped —
    # adding any finding can only ever hold the score steady or raise it.
    best: Dict[str, str] = {}
    for f in findings:
        cur = best.get(f.rule_id)
        if cur is None or SEVERITY_ORDER.index(f.severity) < SEVERITY_ORDER.index(cur):
            best[f.rule_id] = f.severity
    return min(100, sum(SEVERITY_WEIGHT[sev] for sev in best.values()))


_MAX_FINDINGS_PER_RULE = 8


def combine_results(results: Sequence[ScanResult]) -> ScanResult:
    """Merge several `ScanResult`s (e.g. a command-line scan + a local
    script's directory scan + the server's advertised tool descriptions)
    into one, rescoring across the union of findings so the combined risk
    is never understated by scoring each piece separately."""
    findings: List[Finding] = []
    files_scanned = 0
    truncated = False
    for r in results:
        findings.extend(r.findings)
        files_scanned += r.files_scanned
        truncated = truncated or r.truncated
    score = _score(findings)
    return ScanResult(findings=findings, risk_score=score,
                       risk_level=_risk_level_for_findings(findings),
                       files_scanned=files_scanned, truncated=truncated)


_URL_LITERAL = re.compile(r"https?://([^/\s'\"`:]+)", re.IGNORECASE)
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1", "0.0.0.0")


def _only_loopback_urls(text: str) -> bool:
    """True when the text names at least one URL and every URL it names is a
    loopback host. Hosts built at runtime cannot be judged; this only lowers
    the severity of a finding, it never removes one."""
    hosts = [h.lower() for h in _URL_LITERAL.findall(text or "")]
    return bool(hosts) and all(h in _LOOPBACK_HOSTS or h.startswith("127.") for h in hosts)


def scan_text(text: str, *, kind: str = "generic", filename: str = "<text>") -> ScanResult:
    """Scan one blob of text (source, config, markdown, or a tool
    description string) and return every rule match. Pure — no I/O, no
    network, no code execution."""
    text = text or ""
    fenced = _fenced_line_numbers(text) if kind in _MARKDOWN_KINDS else set()
    findings: List[Finding] = []
    lines = text.split("\n")

    for rule in RULES:
        if rule.only_kinds and kind not in rule.only_kinds:
            continue
        count = 0
        for m in rule.pattern.finditer(text):
            if count >= _MAX_FINDINGS_PER_RULE:
                break
            if rule.requires_near is not None:
                lo = max(0, m.start() - rule.window)
                hi = min(len(text), m.end() + rule.window)
                if not rule.requires_near.search(text[lo:hi]):
                    continue
            line_no = text.count("\n", 0, m.start()) + 1
            severity = rule.severity
            if rule.id == "EXFIL_SECRET_TO_NETWORK" and _only_loopback_urls(text):
                # A local app's bridge reads its own token to call its own
                # loopback API: every URL literal in the file is loopback.
                severity = "low"
            if rule.category in _DOWNGRADE_IN_FENCE and line_no in fenced:
                severity = "medium" if SEVERITY_ORDER.index(severity) < SEVERITY_ORDER.index("medium") else severity
            line_text = lines[line_no - 1] if 0 < line_no <= len(lines) else text[m.start():m.end()]
            col = m.start() - (text.rfind("\n", 0, m.start()) + 1)
            findings.append(Finding(
                rule_id=rule.id, category=rule.category, severity=severity,
                file=filename, line=line_no,
                snippet=_snippet(line_text, col, col + (m.end() - m.start())),
                description=rule.description,
            ))
            count += 1

    score = _score(findings)
    return ScanResult(findings=findings, risk_score=score,
                       risk_level=_risk_level_for_findings(findings), files_scanned=1)


# ── file/dir scanning ───────────────────────────────────────────────────

_SCANNABLE_EXT = frozenset({
    ".py", ".js", ".ts", ".mjs", ".cjs", ".jsx", ".tsx", ".sh", ".bash",
    ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl", ".php", ".go",
    ".rs", ".java", ".md", ".markdown", ".json", ".yaml", ".yml", ".toml",
    ".txt", ".cfg", ".ini", ".env",
})
_MAX_FILE_BYTES_DEFAULT = 512 * 1024


def _kind_for_ext(path: str, default_kind: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".markdown"):
        return "markdown"
    return default_kind


def _iter_files(paths: Iterable[str], max_files: int) -> Iterable[str]:
    seen = 0
    for p in paths:
        if seen >= max_files:
            return
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d not in (
                    ".git", "node_modules", "__pycache__", ".venv", "venv")]
                for fn in files:
                    if seen >= max_files:
                        return
                    ext = os.path.splitext(fn)[1].lower()
                    if ext and ext not in _SCANNABLE_EXT:
                        continue
                    yield os.path.join(root, fn)
                    seen += 1
        elif os.path.isfile(p):
            yield p
            seen += 1


def scan_paths(paths, *, kind: str = "generic", max_files: int = 200,
                max_bytes: int = _MAX_FILE_BYTES_DEFAULT) -> ScanResult:
    """Scan a file, a list of files, or a directory tree (recursively, with
    common noise directories skipped). Never raises on an unreadable or
    binary file — it is simply skipped."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    paths = [os.fspath(p) for p in paths]
    findings: List[Finding] = []
    files_scanned = 0
    truncated = False
    file_list = list(_iter_files(paths, max_files + 1))
    if len(file_list) > max_files:
        truncated = True
        file_list = file_list[:max_files]

    for path in file_list:
        try:
            size = os.path.getsize(path)
            if size > max_bytes:
                truncated = True
                continue
            with open(path, "rb") as fh:
                raw = fh.read(max_bytes)
        except OSError:
            continue
        if b"\x00" in raw:  # crude binary check
            continue
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - unreadable, skip
            continue
        file_kind = _kind_for_ext(path, kind)
        result = scan_text(text, kind=file_kind, filename=path)
        findings.extend(result.findings)
        files_scanned += 1

    score = _score(findings)
    return ScanResult(findings=findings, risk_score=score,
                       risk_level=_risk_level_for_findings(findings),
                       files_scanned=files_scanned, truncated=truncated)
