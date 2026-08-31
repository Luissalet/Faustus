"""CUDA Sysmem Fallback Policy, read through NVAPI's driver settings store.

Since driver 536 the NVIDIA driver, rather than failing a CUDA allocation that
does not fit in VRAM, can place it in shared system memory. For a game that is
a stutter; for a language model it is a 20x slowdown that shows up nowhere —
`nvidia-smi` still reports the card nearly full, `ollama ps` still says the
model is on the GPU. The setting that governs it lives in the NVIDIA Control
Panel under *Manage 3D settings → CUDA - Sysmem Fallback Policy*.

What this module found out about automating that, on a GeForce RTX 4070 Ti
with driver 560.94 (probed, not assumed — see tests/test_nvidia_drs.py):

* NVAPI's driver settings store (DRS) is reachable from an ordinary process:
  ``NvAPI_Initialize``, ``DRS_CreateSession``, ``DRS_LoadSettings`` and
  ``DRS_GetBaseProfile`` all succeed without elevation.
* ``NvAPI_DRS_EnumAvailableSettingIds`` returns 102 settings, and the sysmem
  fallback policy is **not one of them**. ``DRS_GetSetting`` and
  ``DRS_SetSetting`` for its documented id both answer
  ``NVAPI_SETTING_NOT_FOUND``: the Control Panel writes it through a private
  path that the public API does not expose.
* ``DRS_SaveSettings`` returns ``NVAPI_ACCESS_DENIED`` unelevated anyway, so
  even a supported setting would need an administrator.

So this module does not pretend. It reports what the driver actually exposes,
reads the value where a future driver does expose it, and otherwise tells the
caller — and the user — that this one is a manual step, while
``gpu_shared_memory`` measures the *effect* and says whether it is happening
right now. Measuring the symptom beats reading a flag we cannot see.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Community-documented id for "CUDA - Sysmem Fallback Policy".
SETTING_ID = 0x10ECECC9
VALUES = {0: "Driver Default", 1: "Prefer No Sysmem Fallback", 2: "Prefer Sysmem Fallback"}
PREFER_NO_FALLBACK = 1

UNI_MAX = 2048
BIN_MAX = 4096

_FUNC_IDS = {
    "Initialize": 0x0150E828,
    "GetErrorMessage": 0x6C2D048C,
    "DRS_CreateSession": 0x0694D52E,
    "DRS_DestroySession": 0xDAD9CFF8,
    "DRS_LoadSettings": 0x375DBD6B,
    "DRS_SaveSettings": 0xFCBC7E14,
    "DRS_GetBaseProfile": 0xDA8466A0,
    "DRS_GetSetting": 0x73BF8338,
    "DRS_SetSetting": 0x577DD202,
    "DRS_EnumAvailableSettingIds": 0xF020614A,
}

CONTROL_PANEL_STEPS = [
    "Open the NVIDIA Control Panel (right-click the desktop, or nvcplui.exe).",
    "Manage 3D settings → Program Settings, and pick ollama.exe (Add it if it is not listed).",
    'Set "CUDA - Sysmem Fallback Policy" to "Prefer No Sysmem Fallback", then Apply.',
    "Reload the model. If an allocation no longer fits it now fails loudly instead of "
    "crawling, and Ollama re-plans the layer split.",
]


def _api():
    """(query_interface, ctypes) or None. Windows with an NVIDIA driver only."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes

        dll = ctypes.WinDLL("nvapi64.dll")
    except Exception as e:
        logger.debug("nvapi64.dll not loadable: %s", e)
        return None
    try:
        qi = dll.nvapi_QueryInterface
        qi.restype = ctypes.c_void_p
        qi.argtypes = [ctypes.c_uint32]
    except AttributeError:
        return None
    return qi, ctypes


def _bind(qi, ctypes, name, restype, argtypes):
    ptr = qi(_FUNC_IDS[name])
    if not ptr:
        raise OSError(f"NVAPI does not export {name}")
    return ctypes.CFUNCTYPE(restype, *argtypes)(ptr)


def _setting_struct(ctypes):
    class BinarySetting(ctypes.Structure):
        _fields_ = [("valueLength", ctypes.c_uint32), ("valueData", ctypes.c_uint8 * BIN_MAX)]

    class SettingUnion(ctypes.Union):
        _fields_ = [("u32Value", ctypes.c_uint32), ("binaryValue", BinarySetting),
                    ("wszValue", ctypes.c_uint16 * UNI_MAX)]

    class NVDRS_SETTING(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("settingName", ctypes.c_uint16 * UNI_MAX),
                    ("settingId", ctypes.c_uint32), ("settingType", ctypes.c_uint32),
                    ("settingLocation", ctypes.c_uint32), ("isCurrentPredefined", ctypes.c_uint32),
                    ("isPredefinedValid", ctypes.c_uint32), ("predefined", SettingUnion),
                    ("current", SettingUnion)]

    return NVDRS_SETTING


def status() -> Dict[str, Any]:
    """What the driver exposes, and the current value when it exposes it.

    Never raises: the usage endpoint calls this on a timer.
    """
    out: Dict[str, Any] = {
        "available": False,          # can we talk to NVAPI at all
        "exposed": False,            # does this driver expose the setting
        "value": None,
        "label": None,
        "manual_only": True,
        "setting_id": f"{SETTING_ID:#010x}",
        "steps": CONTROL_PANEL_STEPS,
        "reason": None,
    }
    api = _api()
    if not api:
        out["reason"] = "NVAPI not available (not Windows, or no NVIDIA driver)"
        return out
    qi, ctypes = api
    try:
        init = _bind(qi, ctypes, "Initialize", ctypes.c_int32, [])
        rc = init()
        if rc != 0:
            out["reason"] = f"NvAPI_Initialize returned {rc}"
            return out
        out["available"] = True

        enum = _bind(qi, ctypes, "DRS_EnumAvailableSettingIds", ctypes.c_int32,
                     [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)])
        ids = (ctypes.c_uint32 * 4096)()
        count = ctypes.c_uint32(4096)
        if enum(ids, ctypes.byref(count)) == 0:
            known = {ids[i] for i in range(count.value)}
            out["driver_settings_count"] = count.value
            out["exposed"] = SETTING_ID in known
        if not out["exposed"]:
            out["reason"] = (
                "this driver does not expose the sysmem fallback policy through NVAPI — "
                "the Control Panel writes it privately"
            )
            return out

        HANDLE = ctypes.c_void_p
        create = _bind(qi, ctypes, "DRS_CreateSession", ctypes.c_int32, [ctypes.POINTER(HANDLE)])
        load = _bind(qi, ctypes, "DRS_LoadSettings", ctypes.c_int32, [HANDLE])
        base = _bind(qi, ctypes, "DRS_GetBaseProfile", ctypes.c_int32, [HANDLE, ctypes.POINTER(HANDLE)])
        NVDRS_SETTING = _setting_struct(ctypes)
        get = _bind(qi, ctypes, "DRS_GetSetting", ctypes.c_int32,
                    [HANDLE, HANDLE, ctypes.c_uint32, ctypes.POINTER(NVDRS_SETTING)])
        destroy = _bind(qi, ctypes, "DRS_DestroySession", ctypes.c_int32, [HANDLE])
        sess = HANDLE()
        if create(ctypes.byref(sess)) != 0:
            out["reason"] = "DRS_CreateSession failed"
            return out
        try:
            load(sess)
            prof = HANDLE()
            if base(sess, ctypes.byref(prof)) != 0:
                out["reason"] = "DRS_GetBaseProfile failed"
                return out
            st = NVDRS_SETTING()
            st.version = ctypes.sizeof(NVDRS_SETTING) | (1 << 16)
            rc = get(sess, prof, SETTING_ID, ctypes.byref(st))
            if rc == 0:
                out["value"] = int(st.current.u32Value)
                out["label"] = VALUES.get(out["value"], f"unknown ({out['value']})")
                out["manual_only"] = False
            else:
                out["reason"] = f"setting not present in the base profile (NVAPI {rc})"
                out["manual_only"] = False
        finally:
            destroy(sess)
    except Exception as e:  # pragma: no cover - driver-specific
        out["reason"] = f"NVAPI: {e}"
    return out


def open_control_panel() -> Dict[str, Any]:
    """Launch the NVIDIA Control Panel so the user is one click from the setting.

    There is no documented way to deep-link to a settings page, so we open the
    app and hand the steps over with it.
    """
    if not sys.platform.startswith("win"):
        return {"ok": False, "error": "Windows only", "steps": CONTROL_PANEL_STEPS}
    for cmd in (
        ["nvcplui.exe"],
        [r"C:\Program Files\NVIDIA Corporation\Control Panel Client\nvcplui.exe"],
        ["control.exe", "nvcpl.cpl"],
    ):
        try:
            subprocess.Popen(cmd, close_fds=True)
            return {"ok": True, "launched": cmd[0], "steps": CONTROL_PANEL_STEPS}
        except OSError:
            continue
    return {
        "ok": False,
        "error": "could not launch the NVIDIA Control Panel",
        "steps": CONTROL_PANEL_STEPS,
    }
