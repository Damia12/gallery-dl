#!/usr/bin/env python3
"""auditar2.py — Auditoría forense sobre eventos .jsonl.

Reemplazo en construcción de auditar.py. Vive aparte hasta que el bloque C
esté verde, porque descarga.py ejecuta auditar.py al final de cada lote y no
puede quedar a medias.

Diferencia de fondo con auditar.py: no re-parsea texto plano. El .log mezcla
paths de archivo (datos) con líneas de log (metadatos), y clasificar eso con
regex de subcadena produce falsos FATAL — un nombre de Instagram con "404"
adentro de un ID numérico es indistinguible de un HTTP 404. Acá los eventos
vienen tipados y cada campo tiene un solo significado.

Estado: firmas definidas, lógica pendiente (bloque C del plan).
"""

import json
import re

# =============================================================================
# IDENTIDAD
# =============================================================================

# Caracteres inválidos en un nombre de archivo Windows. Misma regla que usa
# descarga.py:1328 para nombrar el .log/.jsonl.
#
# PENDIENTE: esta regla está duplicada entre descarga.py y este módulo, que es
# justo el patrón de desincronización que el rewrite viene a eliminar. La
# alternativa es que descarga.py emita `nombre` dentro del evento `inicio`.
# A decidir antes del bloque B.
RE_INVALIDOS = re.compile(r'[\\/*?:"<>|]')


def nombre_desde_url(url: str) -> str:
    """Deriva el nombre del modelo desde la URL, igual que descarga.py."""
    if not url:
        return ""
    return RE_INVALIDOS.sub("_", url.rstrip("/").split("/")[-1])[:60]


# =============================================================================
# CLASIFICACIÓN DE ERRORES
# =============================================================================

# Mismos patrones que auditar.py, pero aplicados SOLO al campo `msg` de los
# eventos de error — nunca a paths de archivo. Ese cambio de dominio es el
# arreglo; los patrones en sí estaban bien.
PATRONES_FATAL = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b404\b",
        r"\b410\b",
        r"thread.*deleted",
        r"invalid.*thread",
        r"unsupported.*url",
        r"unable to extract",
        r"failed to parse",
    )
]

ESTADOS = ("OK", "TRANSITORIO", "FATAL", "TIMEOUT")


# =============================================================================
# LECTURA
# =============================================================================


def leer_eventos(ruta) -> list:
    """Lee un .jsonl y devuelve la lista de eventos como dicts.

    Una línea mal formada se descarta sin abortar: un .jsonl truncado por un
    kill del watchdog debe poder auditarse igual hasta donde llegó.
    """
    eventos = []
    with open(ruta, "r", encoding="utf-8", errors="replace") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                evento = json.loads(linea)
            except json.JSONDecodeError:
                continue
            if isinstance(evento, dict) and "t" in evento:
                eventos.append(evento)
    return eventos


# =============================================================================
# RESUMEN
# =============================================================================


def resumir(eventos: list) -> dict:
    """Colapsa una lista de eventos en el resumen de una URL.

    Cada campo tiene una sola fuente, sin reconciliación:

      nuevos, ya        <- contados de los eventos `archivo`, deduplicados por
                           `path` (gallery-dl puede repetir una ruta en stdout)
      errores, warnings <- contados de los eventos `error` / `warning`
      posts_con_error   <- post_id de los eventos `error`, ordenados y únicos
      url               <- del evento `inicio`
      duracion,
      returncode,
      timeout           <- del evento `fin` (nadie más los conoce)

    `completo` es False si falta el evento `fin`: el proceso murió antes de
    terminar y el resumen no es comparable con los demás.
    """
    inicio = {}
    fin = None
    vistos = set()  # paths ya contados
    nuevos = ya = 0
    errores = warnings = 0
    posts = set()

    for e in eventos:
        tipo = e.get("t")
        if tipo == "inicio":
            inicio = e
        elif tipo == "archivo":
            path = e.get("path")
            if path in vistos:
                continue  # gallery-dl puede repetir una ruta en stdout
            vistos.add(path)
            if e.get("nuevo", True):
                nuevos += 1
            else:
                ya += 1
        elif tipo == "error":
            errores += 1
            if (pid := e.get("post_id")) is not None:
                posts.add(pid)
        elif tipo == "warning":
            warnings += 1
        elif tipo == "fin":
            fin = e

    url = inicio.get("url", "")
    return {
        "completo": fin is not None,
        "nombre_modelo": nombre_desde_url(url),
        "url": url,
        "nuevos": nuevos,
        "ya": ya,
        "errores": errores,
        "warnings": warnings,
        "posts_con_error": sorted(posts),
        "duracion": (fin or {}).get("duracion", 0),
        "returncode": (fin or {}).get("returncode", -1),
        "timeout": (fin or {}).get("timeout", False),
        "errores_msg": [
            e["msg"] for e in eventos if e.get("t") == "error" and "msg" in e
        ],
    }


# =============================================================================
# ESTADO
# =============================================================================


def es_fatal(mensaje: str) -> bool:
    """True si el mensaje de error corresponde a un fallo no recuperable.

    Solo debe recibir el campo `msg` de un evento `error`. Pasarle un path de
    archivo es el bug que este módulo existe para eliminar.
    """
    return any(p.search(mensaje) for p in PATRONES_FATAL)


def clasificar(resumen: dict) -> str:
    """Devuelve uno de ESTADOS a partir de un resumen.

    Prioridad: TIMEOUT > FATAL > TRANSITORIO > OK.

      TIMEOUT      el watchdog mató el proceso
      FATAL        algún evento `error` matchea PATRONES_FATAL
      TRANSITORIO  hubo errores no fatales, o returncode != 0
      OK           ninguna de las anteriores

    Los estados TIMEOUT_SANANDO / TIMEOUT_ATASCADO de auditar.py no
    sobreviven: codificaban un umbral inventado (errores < nuevos * 0.20) que
    nunca se validó contra datos. La distinción se lee de las columnas
    Nuevos y Errores del CSV.
    """
    if resumen.get("timeout"):
        return "TIMEOUT"
    if any(es_fatal(m) for m in resumen.get("errores_msg", ())):
        return "FATAL"
    if resumen.get("errores", 0) > 0 or resumen.get("returncode", 0) != 0:
        return "TRANSITORIO"
    return "OK"


# =============================================================================
# CSV
# =============================================================================

CSV_HEADER = [
    "Fecha",
    "Nombre_Modelo",
    "URL",
    "Nuevos",
    "Ya_descargados",
    "Errores",
    "Warnings",
    "Posts_con_error",
    "Duracion_s",
    "Returncode",
    "Timeout",
    "Estado",
]


def fila_csv(resumen: dict, fecha: str) -> list:
    """Arma la fila del CSV a partir de un resumen. Mismo orden que CSV_HEADER.

    Los post_id van separados por coma: el delimitador del CSV es ';' y no
    puede aparecer dentro de una celda.
    """
    return [
        fecha,
        resumen["nombre_modelo"],
        resumen["url"],
        resumen["nuevos"],
        resumen["ya"],
        resumen["errores"],
        resumen["warnings"],
        ", ".join(str(p) for p in resumen["posts_con_error"]),
        resumen["duracion"],
        resumen["returncode"],
        "Sí" if resumen["timeout"] else "No",
        clasificar(resumen),
    ]
