# Lot L wiring — app.py (integrator-owned, not touched by this lot)

Three small, additive changes to `app.py`, each next to the equivalent
`model_warmup` line it mirrors. `tests/test_l_wiring.py` asserts all three
(`xfail(strict=True)`) and should be un-xfailed once they are applied.

## 1. `GET /api/health` reports which instance answered

```diff
 @app.get("/api/health")
 async def health_check() -> Dict[str, str]:
-    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
+    payload = {
+        "status": "healthy",
+        "timestamp": datetime.now(timezone.utc).isoformat(),
+        "service": "faustus",
+    }
+    try:
+        from src import model_lease
+        rec = model_lease.self_record()
+        payload["instance_id"] = rec.get("instance_id")
+        payload["port"] = rec.get("port")
+        payload["leases"] = bool(rec)
+    except Exception:  # noqa: BLE001
+        payload["leases"] = False
+    return payload
```

## 2. Startup — register this instance's lease

Right after `model_warmup.start()` in the startup handler:

```diff
     try:
         from src import model_warmup
         model_warmup.start()
     except Exception as e:  # noqa: BLE001
         logger.warning(f"Model warmup not started (non-critical): {e}")
+
+    # Shared model lease (src/model_lease.py, lot L): register this
+    # instance so sibling instances sharing the same local Ollama can see
+    # it (residency leadership election, VRAM reservation visibility).
+    try:
+        from src import model_lease
+        model_lease.start()
+    except Exception as e:  # noqa: BLE001
+        logger.warning(f"Model lease not started (non-critical): {e}")
```

## 3. Shutdown — release this instance's lease

Right after `await model_warmup.stop()` in the shutdown handler:

```diff
         try:
             from src import model_warmup
             await model_warmup.stop()
         except Exception:  # noqa: BLE001
             pass
+        try:
+            from src import model_lease
+            await model_lease.stop()
+        except Exception:  # noqa: BLE001
+            pass
```
