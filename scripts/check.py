#!/usr/bin/env python3
"""Comprueba el estado de los servicios y actualiza data/status.json.

Se ejecuta desde GitHub Actions (no desde el navegador): los servidores no
envian cabeceras CORS, y asi tampoco exponemos la IP de los visitantes.

Mantiene:
  - estado actual de cada servicio (up/down) y latencia
  - "since": desde cuando lleva en ese estado (para el "caido desde...")
  - history: ultimos N checks, para pintar la barra de uptime
  - incidents: registro de caidas con inicio/fin y duracion
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "status.json"

# Endpoints elegidos a proposito: responden 200 SIN autenticacion.
# - Jellyfin expone /health
# - Plex expone /identity (XML con la version, sin token)
SERVICES = [
    {
        "id": "jellyfin",
        "name": "Jellyfin",
        "url": "https://ver.juman.ch/",
        "check_url": "https://ver.juman.ch/health",
        "expect": [200],
    },
    {
        "id": "plex",
        "name": "Plex",
        "url": "https://plex.juman.ch/",
        "check_url": "https://plex.juman.ch/identity",
        "expect": [200],
    },
]

TIMEOUT = 15
ATTEMPTS = 3          # reintentos antes de declarar caida (evita falsos positivos)
RETRY_WAIT = 4
HISTORY_MAX = 2016    # ~7 dias a 5 min por check
INCIDENTS_MAX = 50
UA = "juman-status-page/1.0 (+https://github.com/)"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def probe(svc: dict) -> dict:
    """Un servicio se considera caido solo si falla ATTEMPTS veces seguidas."""
    last_err = None
    code = None
    for i in range(ATTEMPTS):
        start = time.monotonic()
        try:
            req = urllib.request.Request(
                svc["check_url"],
                headers={"User-Agent": UA, "Accept": "*/*"},
                method="GET",
            )
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                code = r.status
                r.read(2048)
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:                      # red, DNS, TLS, timeout
            last_err = type(e).__name__
            code = None

        ms = int((time.monotonic() - start) * 1000)
        if code in svc["expect"]:
            return {"up": True, "code": code, "ms": ms, "error": None}
        last_err = last_err or f"HTTP {code}"
        if i < ATTEMPTS - 1:
            time.sleep(RETRY_WAIT)

    return {"up": False, "code": code, "ms": None, "error": last_err}


def load() -> dict:
    try:
        return json.loads(DATA.read_text())
    except Exception:
        return {"services": {}}


def main() -> int:
    prev = load()
    prev_svcs = prev.get("services", {})
    ts = now_iso()
    out = {"updated": ts, "services": {}}

    for svc in SERVICES:
        res = probe(svc)
        old = prev_svcs.get(svc["id"], {})
        was_up = old.get("up")

        # "since": si el estado cambia, el reloj se reinicia ahora
        since = old.get("since") or ts
        if was_up is not None and was_up != res["up"]:
            since = ts

        history = list(old.get("history") or [])
        history.append({"t": ts, "u": 1 if res["up"] else 0,
                        "ms": res["ms"]})
        history = history[-HISTORY_MAX:]

        incidents = list(old.get("incidents") or [])
        if was_up is True and not res["up"]:
            incidents.append({"start": ts, "end": None, "error": res["error"]})
        elif was_up is False and res["up"]:
            for inc in reversed(incidents):
                if inc.get("end") is None:
                    inc["end"] = ts
                    try:
                        a = datetime.fromisoformat(inc["start"])
                        b = datetime.fromisoformat(ts)
                        inc["seconds"] = int((b - a).total_seconds())
                    except Exception:
                        pass
                    break
        incidents = incidents[-INCIDENTS_MAX:]

        ups = sum(h["u"] for h in history)
        lat = [h["ms"] for h in history[-100:] if h.get("ms")]

        out["services"][svc["id"]] = {
            "name": svc["name"],
            "url": svc["url"],
            "up": res["up"],
            "code": res["code"],
            "ms": res["ms"],
            "error": res["error"],
            "since": since,
            "checks": len(history),
            "uptime_pct": round(100.0 * ups / len(history), 2) if history else None,
            "avg_ms": int(sum(lat) / len(lat)) if lat else None,
            "history": history,
            "incidents": incidents,
        }
        print(f"{svc['name']}: {'UP' if res['up'] else 'DOWN'} "
              f"({res['code']}, {res['ms']}ms) err={res['error']}")

    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
