"""QA-31 · SSRF y redireccion (docs/spec/v2/acceptance_scenarios.json).

Estimulo: URL redirige a servicio privado/metadata con DNS cambiante.
Resultado exigido (literal): "Revalidacion y bloqueo sin fetch interno no
autorizado."

Requisitos: WEB-02, SEC-04.

Estado: verde. `src/outbound_fetch.py::classify_destination`/`fetch` (WEB-02,
existente) resuelven una unica vez por hop y bloquean tanto un nombre cuya
resolucion "cambia" entre una direccion publica y una privada en la misma
consulta (rebinding), como una redireccion que cruza de zona publica a
zona privada/metadata - re-clasificando cada salto en vez de heredar el
veredicto del primero.
"""
import ipaddress

import httpx
import pytest

from src import outbound_fetch as of

pytestmark = pytest.mark.qa_state("green")


def test_dns_cambiante_straddling_public_and_private_is_blocked():
    # The same hostname resolves to BOTH a public and a link-local/metadata
    # address in one lookup - a classic rebinding setup.
    def flaky_resolver(host):
        return ["93.184.216.34", "169.254.169.254"]

    with pytest.raises(of.OutboundPolicyError, match="link-local|straddles"):
        of.classify_destination(
            "http://flaky.example/", profile=of.PUBLIC_UNTRUSTED, resolver=flaky_resolver,
        )


def test_redirect_to_a_metadata_service_is_blocked_not_followed():
    def public_resolver(host):
        return ["93.184.216.34"]

    def handler(request):
        if request.url.host == "safe.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError("the internal fetch must never actually happen")

    def transport_factory(addr):
        return httpx.MockTransport(handler)

    with pytest.raises(of.OutboundPolicyError, match="169.254.169.254"):
        of.fetch(
            "http://safe.example/start",
            profile=of.PUBLIC_UNTRUSTED,
            resolver=public_resolver,
            transport_factory=transport_factory,
        )
