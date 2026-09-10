"""Lote 16 (Seguridad) - SEC-03 / SEC-04.

SEC-03: safe paths. `src/tool_execution.py` already confines `..` and
symlink escapes; the Windows reserved-device-name gap this file tracked
(CON, PRN, AUX, NUL, COM1-9, LPT1-9 opening the physical device instead of a
regular file on Windows) is closed as of Lote 20 —
`_is_windows_reserved_name`/`_WINDOWS_RESERVED_NAMES_CF` in
`src/tool_execution.py` reject any such component in a resolved path. Per
that gap-tracking test's own failure message ("if so, this gap-tracking
guard should become a positive test and SEC_ESTADO.md's open item should be
closed"), this test is now converted to prove the rejection directly instead
of asserting the gap still exists.

SEC-04: network egress (`src/outbound_fetch.py`, owned). Extends QA-31 with
the specific evasions the lot brief names that QA-31 does not cover: an
IPv4-mapped IPv6 metadata address, and a REDIRECT CHAIN (not just one hop)
where only the *second* hop turns hostile - proving every hop is
re-classified, not just the first.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

import httpx
import pytest

from src import outbound_fetch as of
from src import tool_execution as te

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── SEC-03 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["CON", "con", "CON.txt", "PRN", "NUL", "COM1", "LPT9.log"])
def test_windows_reserved_device_names_are_rejected_in_any_path_component(tmp_path, name):
    """Lote 20 closes the gap this test used to only document: a path whose
    resolution passes through a reserved device name — the leaf, or any
    intermediate directory — is rejected, not silently allowed to resolve
    onto the physical device."""
    with pytest.raises(ValueError, match="sensitive"):
        te._resolve_tool_path_in_workspace(str(tmp_path), name)


def test_windows_reserved_device_name_in_an_intermediate_directory_is_also_rejected(tmp_path):
    with pytest.raises(ValueError, match="sensitive"):
        te._resolve_tool_path_in_workspace(str(tmp_path), "CON/out.log")


def test_an_ordinary_filename_sharing_a_reserved_prefix_is_not_rejected(tmp_path):
    """No over-matching: "console.log" and "company" are ordinary names, not
    the reserved device "CON" — only an exact reserved stem (before the
    first dot) trips the check."""
    resolved = te._resolve_tool_path_in_workspace(str(tmp_path), "console.log")
    assert resolved == str(Path(tmp_path / "console.log").resolve())


# ── SEC-04 ────────────────────────────────────────────────────────────────

def test_ipv4_mapped_ipv6_metadata_address_is_blocked_not_unwrapped_past_the_check():
    """`::ffff:169.254.169.254` is the IPv4-mapped IPv6 form of the metadata
    address - a classic filter-bypass shape for a check that only
    pattern-matches the dotted-quad string or only inspects `IPv4Address`.
    `_classify_address`/`_unwrap_mapped` exist precisely so this cannot slip
    through disguised as an "unrecognised" IPv6 address."""
    def resolver(host):
        return ["::ffff:169.254.169.254"]

    with pytest.raises(of.OutboundPolicyError, match="169.254.169.254|link-local|metadata"):
        of.classify_destination(
            "http://sneaky.example/", profile=of.PUBLIC_UNTRUSTED, resolver=resolver,
        )


def test_a_second_hop_that_turns_hostile_is_still_caught_not_just_the_first():
    """safe.example -> relay.example (both public, allowed) -> the metadata
    service. If only the first hop were classified, this chain would sail
    through; each hop is re-resolved and re-classified, so the THIRD leg is
    the one that trips."""
    def public_resolver(host):
        return {"safe.example": ["93.184.216.34"], "relay.example": ["93.184.216.35"]}.get(
            host, ["93.184.216.34"]
        )

    def handler(request):
        if request.url.host == "safe.example":
            return httpx.Response(302, headers={"location": "http://relay.example/next"})
        if request.url.host == "relay.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("the internal metadata fetch must never actually happen")

    def transport_factory(addr):
        return httpx.MockTransport(handler)

    with pytest.raises(of.OutboundPolicyError, match="169.254.169.254"):
        of.fetch(
            "http://safe.example/start",
            profile=of.PUBLIC_UNTRUSTED,
            resolver=public_resolver,
            transport_factory=transport_factory,
        )


def test_a_redirect_out_of_a_local_only_profile_into_the_public_internet_is_blocked():
    """The reverse SSRF shape: a URL trusted as local (e.g. the operator's own
    LAN device) redirecting the client out to the public internet, which
    would exfiltrate whatever the caller sends on the follow-up request
    (auth headers, cookies). Mixed-zone redirects are refused in both
    directions, not only local-bound ones."""
    def resolver(host):
        return {"nas.local": ["192.168.1.50"]}.get(host, ["93.184.216.34"])

    def handler(request):
        if request.url.host == "nas.local":
            return httpx.Response(302, headers={"location": "http://evil.example/collect"})
        raise AssertionError("must never actually reach the public redirect target")

    def transport_factory(addr):
        return httpx.MockTransport(handler)

    with pytest.raises(of.OutboundPolicyError, match="mixed-zone|straddles"):
        of.fetch(
            "http://nas.local/status",
            profile=of.OPERATOR_LOCAL,
            allow_local=True,
            resolver=resolver,
            transport_factory=transport_factory,
        )
