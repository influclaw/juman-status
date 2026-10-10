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
import socket
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

# --- Bloqueo de IPs por el futbol (LaLiga) -------------------------------
# Los dominios de juman.ch van por Cloudflare y resuelven a IPs compartidas
# (188.114.96.5 / 188.114.97.5). Durante los partidos, varios operadores
# espanoles bloquean esas IPs por orden judicial, asi que el servidor sigue
# PERFECTAMENTE EN MARCHA pero los usuarios de esos operadores no llegan.
#
# Sin este aviso la pagina resulta enganosa: dice "todo bien" (y es verdad,
# se comprueba desde GitHub, fuera de Espana) mientras el usuario no entra.
#
# hayahora.futbol publica las IPs bloqueadas por operador. Lo leemos desde
# aqui porque no envia cabeceras CORS: el navegador no podria consultarlo.
BLOCK_ISPS = ["movistar", "vodafone", "orange", "masmovil", "digi"]
BLOCK_LIST_URL = "https://hayahora.futbol/estado/blocked-{isp}.txt"
# Rangos espanoles reales (Telefonica / Orange). Se usan solo como "desde
# donde pregunto" en el DNS, para que Cloudflare devuelva las IPs de Espana.
ES_SUBNETS = ["88.0.0.0/24", "217.127.0.0/24", "83.36.0.0/24"]
# IPs anycast de Cloudflare a las que resuelven estos dominios DESDE ESPANA,
# medidas el 2026-10-10 desde una linea espanola. Van fijas porque desde
# GitHub Actions es imposible obtenerlas (ver resolve_ips).
ES_PINNED_IPS = ["188.114.96.2", "188.114.96.5",
                 "188.114.97.2", "188.114.97.5"]
ISP_NAMES = {
    "movistar": "Movistar",
    "vodafone": "Vodafone",
    "orange": "Orange",
    "masmovil": "MasMovil / Yoigo",
    "digi": "DIGI",
}


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


def resolve_ips(host: str) -> list[str]:
    """IPs v4 del dominio **tal y como se ven desde Espana**.

    Cloudflare no reparte por geo-DNS sino por *anycast*: la respuesta depende
    de a que POP llega la consulta, no de quien pregunta. Por eso:
      - desde GitHub Actions (EEUU) el dominio resuelve a 104.21.x / 172.67.x,
      - desde Espana resuelve a 188.114.96.x / 188.114.97.x,
    y esas ultimas son justo las que bloquean los operadores. Tampoco sirve
    DNS-over-HTTPS con EDNS Client Subnet: Cloudflare lo ignora para esto
    (probado: desde Actions sigue devolviendo las IPs de EEUU).

    Solucion: unir lo que resolvamos dinamicamente con el rango espanol ya
    medido desde Espana (ES_PINNED_IPS). Si el dominio se moviera de IPs
    habria que re-medirlas, pero son las anycast de Cloudflare para Espana y
    cambian muy rara vez.
    """
    ips: set[str] = set(ES_PINNED_IPS)

    for subnet in ES_SUBNETS:
        try:
            url = (f"https://dns.google/resolve?name={host}&type=A"
                   f"&edns_client_subnet={subnet}")
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode())
            got = {a["data"] for a in data.get("Answer", []) if a.get("type") == 1}
            if got:
                ips.update(got)
                break
        except Exception:
            continue

    try:                              # y lo que vea el propio runner
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        ips.update(str(i[4][0]) for i in infos)
    except Exception:
        pass
    return sorted(ips)


def fetch_blocklist(isp: str) -> set[str] | None:
    """IPs bloqueadas por un operador. None si no se pudo consultar."""
    try:
        req = urllib.request.Request(
            BLOCK_LIST_URL.format(isp=isp),
            headers={"User-Agent": UA, "Accept": "text/plain"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
        return {ln.strip() for ln in body.splitlines() if ln.strip()}
    except Exception:
        return None


def check_blocking(hosts: list[str]) -> dict:
    """Cruza las IPs de nuestros dominios con las listas de bloqueo.

    Devuelve que operadores tienen bloqueada alguna de nuestras IPs, para
    poder avisar: "el servidor va, pero desde Vodafone no se llega".
    """
    ips: set[str] = set()
    per_host = {}
    for h in hosts:
        got = resolve_ips(h)
        per_host[h] = got
        ips.update(got)

    blocked_by = []
    checked = 0
    for isp in BLOCK_ISPS:
        lst = fetch_blocklist(isp)
        if lst is None:
            continue
        checked += 1
        hit = sorted(ips & lst)
        if hit:
            blocked_by.append({"isp": isp, "name": ISP_NAMES.get(isp, isp),
                               "ips": hit})

    return {
        "checked_at": now_iso(),
        "ok": checked > 0,                 # False = no se pudo consultar
        "isps_checked": checked,
        "our_ips": sorted(ips),
        "dns": per_host,
        "blocked_by": blocked_by,
        "source": "https://hayahora.futbol",
    }


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

    # Bloqueo por futbol: mismo instante que el resto del chequeo
    hosts = sorted({s["url"].split("//", 1)[1].split("/", 1)[0] for s in SERVICES})
    blk = check_blocking(hosts)

    # historial de bloqueos: guardamos los periodos, no un punto por check
    prev_blk = prev.get("blocking") or {}
    episodes = list(prev_blk.get("episodes") or [])
    was_blocked = bool((prev_blk.get("blocked_by") or []))
    now_blocked = bool(blk["blocked_by"])
    if blk["ok"]:
        if now_blocked and not was_blocked:
            episodes.append({"start": ts, "end": None,
                             "isps": [b["name"] for b in blk["blocked_by"]]})
        elif was_blocked and not now_blocked:
            for ep in reversed(episodes):
                if ep.get("end") is None:
                    ep["end"] = ts
                    try:
                        a = datetime.fromisoformat(ep["start"])
                        ep["seconds"] = int((datetime.fromisoformat(ts) - a)
                                            .total_seconds())
                    except Exception:
                        pass
                    break
        elif now_blocked and episodes and episodes[-1].get("end") is None:
            # sigue bloqueado: refresca la lista de operadores afectados
            names = {b["name"] for b in blk["blocked_by"]}
            episodes[-1]["isps"] = sorted(names | set(episodes[-1].get("isps") or []))
        blk["since"] = (episodes[-1]["start"]
                        if now_blocked and episodes else None)
    else:
        # no se pudo consultar: conserva lo anterior en vez de mentir
        blk["blocked_by"] = prev_blk.get("blocked_by") or []
        blk["since"] = prev_blk.get("since")

    blk["episodes"] = episodes[-INCIDENTS_MAX:]
    out["blocking"] = blk
    print(f"Bloqueo futbol: {'SI -> ' + ', '.join(b['name'] for b in blk['blocked_by']) if blk['blocked_by'] else 'no'}"
          f" (IPs {', '.join(blk['our_ips']) or '?'}, {blk['isps_checked']} operadores consultados)")

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
