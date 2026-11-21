"""Keep the public product guides independent of the installation."""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GUIDES = ("FAUSTUS.md", "README.md", "README.es.md")

PROFILE_PATH = re.compile(r"(?:(?<!\w)[a-z]:[\\/]+users[\\/]+|(?<![\w/])/(?:home|users)/)", re.I)
PRIVATE_HOST = re.compile(r"\b(?:[\w-]+\.)+ts\.net\b", re.I)
NETWORK_ADDRESS = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)"
    r"|(?<![\w:])(?:f[cd][\da-f]{2}|fe[89ab][\da-f]):[\da-f:]+(?![\w:])",
    re.I,
)
PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "169.254.0.0/16", "fc00::/7", "fe80::/10",
)))


def _installation_references(text: str, machine_user: str) -> list[str]:
    username = re.compile(rf"(?<!\w){re.escape(machine_user)}(?!\w)", re.I) if machine_user else None
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if PROFILE_PATH.search(line):
            findings.append(f"line {number}: user profile path")
        if PRIVATE_HOST.search(line):
            findings.append(f"line {number}: private mesh hostname")
        if username and username.search(line):
            findings.append(f"line {number}: machine username")
        for match in NETWORK_ADDRESS.finditer(line):
            try:
                address = ipaddress.ip_address(match[0])
            except ValueError:
                continue
            if any(address in network for network in PRIVATE_NETWORKS):
                findings.append(f"line {number}: private network address")
    return findings


@pytest.mark.parametrize("name", PUBLIC_GUIDES)
def test_public_guides_do_not_contain_installation_references(name):
    machine_user = os.environ.get("FAUSTUS_DOCUMENTATION_MACHINE_USER") or os.environ.get("USERNAME", "")
    findings = _installation_references((ROOT / name).read_text(encoding="utf-8"), machine_user)
    assert not findings, f"{name} contains installation references:\n" + "\n".join(findings)


@pytest.mark.parametrize(("sample", "finding"), [
    ("C:/Users/example-user/project", "user profile path"),
    (r"c:\\USERS\\example-user\\project", "user profile path"),
    ("/home/example-user/project", "user profile path"),
    ("http://192.168.10.20:8000", "private network address"),
    ("Host 100.64.10.20.", "private network address"),
    ("http://[fd00::1]:8000", "private network address"),
    ("https://sample-node.example-tailnet.ts.net", "private mesh hostname"),
    ("owner: EXAMPLE-USER", "machine username"),
])
def test_installation_reference_detection(sample, finding):
    assert f"line 1: {finding}" in _installation_references(sample, "example-user")


def test_installation_reference_detection_accepts_portable_examples():
    text = "http://127.0.0.1:8810 https://model-host.example %USERPROFILE%/project ./docs /api/users/list"
    assert _installation_references(text, "example-user") == []

