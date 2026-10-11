import json,pathlib
from src.source_claims import find_source_claims
from src.agent_harness import TurnLedger
import pytest
REAL='El documento incluye una sección final de verificación que confirma cada cita línea por línea y deja constancia de que no se ha añadido ningún requisito inexistente (la línea 1 solo nombra el proyecto).'
@pytest.mark.parametrize('text',[REAL,'The report contains a verification section: each quote was checked line by line.'])
def test_section_description_is_not_external_check(text):assert not find_source_claims(text)
@pytest.mark.parametrize('text',[
 'Confirmé todas las citas.',
 'El documento incluye una sección de citas verificadas contra Wikipedia.',
 'El informe contiene una sección que confirma las citas de Shakespeare.',
 'The report includes a section with quotes checked against the canonical text.',
 'El documento incluye una sección final que confirma el inventario histórico.',
])
def test_external_checks_are_preserved(text):assert find_source_claims(text)
def test_real_ledger_records_local_quote_report(tmp_path):
 source='Proyecto ficticio Bruma\nLa entrega inicial incluye un inventario CSV y un informe Markdown.\n'
 written='# Decisiones\nLas citas se comprobaron contra guia.txt línea por línea.\n'
 led=TurnLedger(str(tmp_path),'Lee guia.txt y crea decisiones.md con citas exactas.')
 (tmp_path/'guia.txt').write_text(source,encoding='utf-8')
 led.record('read_file',json.dumps({'path':'guia.txt'}),{'output':source,'exit_code':0})
 led.record('grep',json.dumps({'pattern':'.','path':'guia.txt'}),{'output':'1: Proyecto ficticio Bruma','exit_code':0})
 body=written;(tmp_path/'decisiones.md').write_text(body,encoding='utf-8')
 led.record('write_file',json.dumps({'path':'decisiones.md','content':body}),{'output':'Written','exit_code':0})
 assert not led.unconsulted_source_claims(REAL)
 assert led.unconsulted_source_claims('He verificado las citas contra Wikipedia.')