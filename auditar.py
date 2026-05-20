#!/usr/bin/env python3
r"""
auditar.py — Analiza logs de descarga, detecta .part huérfanos, genera lista_retry.txt
aplicando discriminación de errores y extrayendo la URL específica del fallo.

Uso: python auditar.py [--rips-dir G:/Rips]
"""

import csv
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    LOG_DIR = r"G:\Rips\logs"
    RIPS_DIR = r"G:\Rips"
    RETRY_FILE = r"C:\gallery-dl\lista_retry.txt"
    LISTA = r"C:\gallery-dl\lista.txt"
else:
    LOG_DIR = os.path.expanduser("~/Rips/logs")
    RIPS_DIR = os.path.expanduser("~/Rips")
    RETRY_FILE = os.path.expanduser("~/gallery-dl/lista_retry.txt")
    LISTA = os.path.expanduser("~/gallery-dl/lista.txt")

# Archivo acumulativo de auditoría
CSV_HISTORIAL = os.path.join(LOG_DIR, "historial_fallos.csv")

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
GRAY = "\033[90m"
RED = "\033[31m"
WHITE = "\033[37m"

RE_FECHA = re.compile(r"^Fecha:\s*(.+)$", re.MULTILINE)
RE_URL = re.compile(r"^URL:\s*(.+)$", re.MULTILINE)
RE_TIMEOUT = re.compile(r"^TIMEOUT:", re.MULTILINE)

# Regex para extraer URLs específicas dentro del texto del error (ej. for 'https://...')
RE_EXTRAER_URL = re.compile(r"for\s+'([^']+)'")

ERRORES_FATALES = ["404 not found", "unsupported url", "thread deleted", "410 gone"]
ERRORES_TRANSITORIOS = [
    "502 bad gateway",
    "504 gateway",
    "500 internal",
    "html response",
    "timed out",
    "connection reset",
]

# ── Helpers ──────────────────────────────────────────────────────────────────


def leer_lista(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [
            linea.strip() for linea in f if linea.strip() and not linea.startswith("#")
        ]


def cargar_urls_lista():
    urls = leer_lista(LISTA)
    mapa = {}
    for url in urls:
        nombre = url.rstrip("/").split("/")[-1][:60]
        mapa[nombre] = url
    return mapa


# ── Análisis Forense de Logs ──────────────────────────────────────────────────


def parsear_log(log_path):
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        contenido = f.read()

    bloques = re.split(r"={10,}", contenido)
    sesiones = []

    for bloque in bloques:
        bloque = bloque.strip()
        if not bloque:
            continue

        url_match = RE_URL.search(bloque)
        fecha_match = RE_FECHA.search(bloque)
        timeout = bool(RE_TIMEOUT.search(bloque))

        errores = []
        en_bloque_errores = False
        for linea in bloque.splitlines():
            if linea.strip() == "ERRORES:":
                en_bloque_errores = True
                continue
            if en_bloque_errores:
                if linea.strip():
                    errores.append(linea.strip())
                else:
                    en_bloque_errores = False

        url = url_match.group(1).strip() if url_match else None
        fecha = fecha_match.group(1).strip() if fecha_match else None

        if url:
            sesiones.append(
                {"url": url, "fecha": fecha, "timeout": timeout, "errores": errores}
            )

    return sesiones


def analizar_logs():
    if not os.path.exists(LOG_DIR):
        print(f"  {RED}No se encontró el directorio de logs: {LOG_DIR}{RESET}")
        return {}

    logs = sorted(Path(LOG_DIR).glob("*.log"))
    if not logs:
        print(f"  {GRAY}No hay archivos .log en {LOG_DIR}{RESET}")
        return {}

    resultados = {}
    for log_path in logs:
        nombre = log_path.stem
        try:
            sesiones = parsear_log(log_path)
        except Exception as e:
            print(f"  {YELLOW}[!] Error leyendo {log_path.name}: {e}{RESET}")
            continue

        if sesiones:
            resultados[nombre] = sesiones[-1]

    return resultados


# ── Detección de .part Huérfanos ──────────────────────────────────────────────


def buscar_parts(rips_dir):
    parts = []
    for root, _, files in os.walk(rips_dir):
        for fname in files:
            if fname.endswith(".part"):
                ruta = os.path.join(root, fname)
                tam = os.path.getsize(ruta)
                parts.append({"ruta": ruta, "nombre": fname, "tam": tam})
    return parts


def mapear_parts_a_urls(parts, mapa_urls):
    mapeados = []
    sin_mapear = []

    for p in parts:
        partes_ruta = Path(p["ruta"]).parts
        url_encontrada = None
        for segmento in partes_ruta:
            segmento_clean = segmento[:60]
            if segmento_clean in mapa_urls:
                url_encontrada = mapa_urls[segmento_clean]
                break

        if url_encontrada:
            mapeados.append({**p, "url": url_encontrada})
        else:
            sin_mapear.append(p)

    return mapeados, sin_mapear


# ── Reporte Visual ───────────────────────────────────────────────────────────


def imprimir_reporte(
    con_errores,
    con_timeout,
    sin_problemas,
    parts_mapeados,
    parts_sin_mapear,
    total_logs,
):
    ancho = 56
    print(f"\n{BOLD}{'═' * ancho}{RESET}")
    print(
        f"{BOLD}  REPORTE DE AUDITORÍA — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}"
    )
    print(f"{BOLD}{'═' * ancho}{RESET}\n")

    print(f"{BOLD}  LOGS ANALIZADOS: {total_logs}{RESET}")
    print(f"  {GREEN}[+] Sin problemas:        {len(sin_problemas)}{RESET}")
    print(
        f"  {YELLOW}[!] Errores Recuperables: {len([h for h in con_errores if h[2]])}{RESET}"
    )
    print(
        f"  {RED}[X] Errores Fatales:      {len([h for h in con_errores if not h[2]])}{RESET}"
    )
    print(f"  {RED}[TIMEOUT] Con Timeout:          {len(con_timeout)}{RESET}")

    if con_errores:
        print(f"\n{BOLD}  DIAGNÓSTICO FORENSE DE ERRORES:{RESET}")
        for nombre, sesion, es_recuperable, diagnostico in con_errores:
            color = YELLOW if es_recuperable else RED
            marca = "[RETRY]" if es_recuperable else "[X] [FATAL - DESCARTADO]"
            print(f"\n  {color}-> {nombre} — {marca}{RESET}")
            print(f"    {GRAY}Diagnóstico: {diagnostico}{RESET}")
            print(f"    {GRAY}URL Hilo:    {sesion['url']}{RESET}")
            for err in sesion["errores"][:3]:
                print(f"    {RED}· {err}{RESET}")

    if con_timeout:
        print(f"\n{BOLD}  HILOS CON TIMEOUT (SIEMPRE RECUPERABLES):{RESET}")
        for nombre, sesion in con_timeout:
            print(f"  {YELLOW}-> {nombre} — [RETRY] [TIMEOUT]{RESET}")
            print(f"    {GRAY}{sesion['url']}{RESET}")

    print(f"\n{BOLD}{'─' * ancho}{RESET}")
    print(
        f"{BOLD}  ARCHIVOS .PART HUÉRFANOS: {len(parts_mapeados) + len(parts_sin_mapear)}{RESET}"
    )

    if parts_mapeados:
        print(f"\n  {YELLOW}Mapeados a URL conocida ({len(parts_mapeados)}):{RESET}")
        for p in parts_mapeados:
            tam_str = (
                f"{p['tam'] / 1024 / 1024:.1f} MB"
                if p["tam"] > 1024 * 1024
                else f"{p['tam'] / 1024:.1f} KB"
            )
            print(f"  {YELLOW}-> {p['nombre']}{RESET} {GRAY}({tam_str}){RESET}")

    if not parts_mapeados and not parts_sin_mapear:
        print(f"  {GREEN}No se encontraron archivos .part{RESET}")

    print(f"\n{BOLD}{'═' * ancho}{RESET}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    rips_dir = RIPS_DIR
    if "--rips-dir" in sys.argv:
        idx = sys.argv.index("--rips-dir")
        if idx + 1 < len(sys.argv):
            rips_dir = sys.argv[idx + 1]

    print(f"{BOLD}{'═' * 56}{RESET}")
    print(f"  Auditando logs en: {LOG_DIR}")
    print(f"  Buscando .part en: {rips_dir}")
    print(f"{BOLD}{'═' * 56}{RESET}\n")

    resultados_logs = analizar_logs()
    parts = buscar_parts(rips_dir)
    mapa_urls = cargar_urls_lista()
    parts_mapeados, parts_sin_mapear = mapear_parts_a_urls(parts, mapa_urls)

    sin_problemas = []
    con_timeout = []
    con_errores = []

    urls_para_retry = []
    csv_rows_nuevas = []
    ahora_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for nombre, sesion in resultados_logs.items():
        if not sesion["errores"] and not sesion["timeout"]:
            sin_problemas.append((nombre, sesion))
            continue

        if sesion["timeout"]:
            con_timeout.append((nombre, sesion))
            urls_para_retry.append(sesion["url"])
            csv_rows_nuevas.append(
                [ahora_str, nombre, "TIMEOUT_ACTIVIDAD", sesion["url"], "N/A"]
            )
            continue

        if sesion["errores"]:
            es_recuperable = True
            diagnostico = "RECUPERABLE: Error desconocido / No clasificado (Reintento por precaución)"

            # Buscaremos si alguna línea expone una URL específica caída
            url_problematica = "No especificada en el log"

            for err in sesion["errores"]:
                err_lower = err.lower()

                # Intentar extraer la URL específica de este error (ej: Bunkr o Gofile directo)
                match_url = RE_EXTRAER_URL.search(err)
                if match_url:
                    url_problematica = match_url.group(1)

                if any(f in err_lower for f in ERRORES_FATALES):
                    es_recuperable = False
                    diagnostico = "FATAL: Enlace caído, borrado o URL no soportada (404/Unsupported)"
                    break  # Prioridad fatal de descarte

                elif any(t in err_lower for t in ERRORES_TRANSITORIOS):
                    es_recuperable = True
                    diagnostico = "RECUPERABLE: Sobrecarga transitoria del servidor externo (502/504/HTML-Response)"

            con_errores.append((nombre, sesion, es_recuperable, diagnostico))

            tipo_csv = (
                f"TRANSITORIO ({diagnostico.split(': ')[0]})"
                if es_recuperable
                else f"FATAL ({diagnostico.split(': ')[0]})"
            )
            if es_recuperable:
                urls_para_retry.append(sesion["url"])

            csv_rows_nuevas.append(
                [ahora_str, nombre, tipo_csv, sesion["url"], url_problematica]
            )

    for p in parts_mapeados:
        if p["url"] not in urls_para_retry:
            urls_para_retry.append(p["url"])

    if urls_para_retry:
        with open(RETRY_FILE, "w", encoding="utf-8") as f:
            for url in urls_para_retry:
                f.write(url + "\n")
    else:
        if os.path.exists(RETRY_FILE):
            os.remove(RETRY_FILE)

    # Escritura en CSV con la nueva columna 'URL_Problematica'
    if csv_rows_nuevas:
        existe_csv = os.path.exists(CSV_HISTORIAL)
        with open(CSV_HISTORIAL, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, delimiter=";")
            if not existe_csv:
                writer.writerow(
                    [
                        "Fecha_Registro",
                        "Hilo",
                        "Tipo_Fallo",
                        "URL_Hilo_Simpcity",
                        "URL_Problematica_Directa",
                    ]
                )
            writer.writerows(csv_rows_nuevas)

    imprimir_reporte(
        con_errores,
        con_timeout,
        sin_problemas,
        parts_mapeados,
        parts_sin_mapear,
        len(resultados_logs),
    )

    if resultados_logs:
        archivo_dir = os.path.join(LOG_DIR, "archivo")
        os.makedirs(archivo_dir, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        zip_path = os.path.join(archivo_dir, f"sesion_{stamp}.zip")
        logs_a_empaquetar = [f for f in os.listdir(LOG_DIR) if f.endswith(".log")]

        if logs_a_empaquetar:
            print(f"  {DIM}Empaquetando logs en {Path(zip_path).name}...{RESET}")
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for filename in logs_a_empaquetar:
                        ruta_original = os.path.join(LOG_DIR, filename)
                        zipf.write(ruta_original, filename)

                for filename in logs_a_empaquetar:
                    os.remove(os.path.join(LOG_DIR, filename))

                print(
                    f"  {GRAY}Refactor completo: logs comprimidos y carpeta principal despejada con mapeo de URLs. {RESET}\n"
                )
            except Exception as e:
                print(
                    f"  {RED}[!] Error crítico en la compresión del ZIP: {e}{RESET}\n"
                )

    if IS_WINDOWS:
        input("Presiona Enter para cerrar...")
