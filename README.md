# Estado de los servidores · juman.ch

Página pública para que los usuarios sepan si **Jellyfin** y **Plex** están en marcha,
y desde cuándo llevan caídos si ha habido un problema.

👉 **Página:** https://influclaw.github.io/juman-status/

## Qué muestra

- Estado actual de cada servicio: en marcha o caído
- **Desde cuándo** lleva en ese estado ("caído desde hace 2 h 15 min")
- Tiempo de respuesta y porcentaje de disponibilidad registrado
- Barra con el histórico de las últimas comprobaciones
- Lista de incidencias recientes con su duración

## Cómo funciona

Las comprobaciones **no** se hacen desde el navegador del visitante, por dos razones:

1. Los servidores no envían cabeceras CORS, así que un `fetch` desde la página
   fallaría siempre.
2. Haría que cada visitante conectara directamente contra el servidor,
   exponiendo su IP y generando tráfico innecesario.

En su lugar, un workflow de **GitHub Actions** comprueba los servicios cada 5 minutos
y guarda el resultado en `data/status.json`. La página es estática y solo lee ese
fichero, que se sirve desde GitHub Pages.

```
GitHub Actions (cada 5 min) → data/status.json → GitHub Pages → visitante
```

### Endpoints comprobados

Se usan rutas que responden `200` **sin autenticación**, para no necesitar
ninguna credencial en el repositorio:

| Servicio | Endpoint | Por qué |
|---|---|---|
| Jellyfin | `https://ver.juman.ch/health` | Endpoint de salud propio de Jellyfin |
| Plex | `https://plex.juman.ch/identity` | Devuelve versión del servidor, sin token |

No se comprueba la raíz (`/`) porque Jellyfin responde `302` y Plex `401`:
ambos indicarían "caído" aunque el servidor esté perfectamente.

Un servicio se marca como caído solo si falla **3 intentos seguidos**
(con 4 s de espera entre ellos), para no registrar falsos positivos por un
microcorte de red del runner.

## Activar GitHub Pages

En el repositorio: **Settings → Pages → Source → GitHub Actions**.

El primer despliegue ocurre solo al hacer push a `main` o al lanzar el workflow
a mano desde la pestaña **Actions → Comprobar estado → Run workflow**.

## Añadir o cambiar servicios

Edita la lista `SERVICES` en [`scripts/check.py`](scripts/check.py):

```python
SERVICES = [
    {
        "id": "jellyfin",
        "name": "Jellyfin",
        "url": "https://ver.juman.ch/",        # enlace del botón
        "check_url": "https://ver.juman.ch/health",  # lo que se comprueba
        "expect": [200],                       # códigos considerados "arriba"
    },
]
```

## Probar en local

```bash
python3 scripts/check.py      # actualiza data/status.json
python3 -m http.server 8000   # abre http://localhost:8000
```

## Notas

- El historial guarda unos 7 días (2016 comprobaciones a 5 min).
- GitHub puede retrasar los cron de Actions si hay cola; no es un monitor
  de precisión milimétrica, es un indicador para usuarios.
- `data/status.json` lo escribe el bot de Actions. Si editas el repo a mano,
  haz `git pull` antes para evitar conflictos.

## Licencia

MIT
