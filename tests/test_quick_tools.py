"""Exercise the real frontend parser with Node; includes money and hostile expressions."""
import shutil
import subprocess
from pathlib import Path
import pytest


def test_deterministic_tools():
    node = shutil.which('node')
    if not node: pytest.skip('Node not installed')
    source = (Path(__file__).resolve().parents[1] / 'studio/src/screens/studio/quick-tools.ts').as_uri()
    script = "import {calculate, parseQuickTool as p} from " + repr(source) + ";\n" + r'''
import assert from 'node:assert/strict';
assert.equal(calculate('-(2 + 3) * 4 / 2 + 1,5'), -8.5);
for (const bad of ['1..', '1 2', '(1+2', '1/0', '1;globalThis.alert(1)', 'process.exit()', '('.repeat(21)+'1'+')'.repeat(21)]) {
  assert.throws(() => calculate(bad), bad);
}
assert.equal(p('= 1 / 0').error, 'Division by zero');
assert.equal(p('convert 32 F to C').result, 0);
assert.equal(p('10 km a millas').result, 10_000/1609.344);
assert.equal(p('10 kg a m'), null);
assert.equal(p('10 USD to EUR'), null);
assert.equal(p('timer 1,5 minutos').seconds, 90);
assert.equal(p('timer 25 hours'), null);
assert.deepEqual(p('lista: uno; dos\n- tres').items, ['uno', 'dos', 'tres']);
const bill = p('reparte 10,01 € entre 3 personas');
assert.equal(bill.base*bill.people+bill.extra, 1001);
assert.equal(bill.extra, 2);
assert.equal(p('split 0.29 USD among 3 people').cents, 29);
assert.equal(p('split 1 USD among 0 people'), null);
assert.equal(p('#fff').hex, '#ffffff');
assert.equal(p('#fff').ink, '#000000');
assert.equal(p('#000').ink, '#ffffff');
assert.ok(p('color #888888').contrast >= 4.5);
assert.equal(p('Explícame cómo calcular una distancia'), null);
assert.equal(p('a'.repeat(2001)), null);
console.log('quick tools: deterministic arithmetic, units, bounded timers, exact cents and contrast passed');
'''
    result = subprocess.run([node, '--experimental-strip-types', '--input-type=module'], input=script, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
