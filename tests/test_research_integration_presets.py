from src.integrations import INTEGRATION_PRESETS, preset_headers

def test_verified_presets_have_configurable_origins_and_correct_auth():
    assert INTEGRATION_PRESETS['apollo']['auth_header']=='x-api-key'
    assert all(INTEGRATION_PRESETS[key]['base_url'].startswith('https://') for key in ('apollo','plausible','calcom','cloudflare'))
    assert preset_headers({'preset':'calcom'},'/v2/slots?start=2026-10-01')['cal-api-version']=='2024-09-04'
    assert preset_headers({'preset':'calcom'},'/v2/bookings/uid/cancel')['cal-api-version']=='2024-08-13'
    assert preset_headers({'preset':'other'},'/v2/slots')=={}
