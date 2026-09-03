#!/usr/bin/env python3
"""auditar.py — Auditoría forense de las descargas, sobre eventos .jsonl.

Lee los `{nombre}.jsonl` que descarga.py deja en `log_dir`, arma una fila por
corrida en `auditoria.csv`, comprime los logs del lote en un ZIP diario y avisa
de los `.part` que quedaron huérfanos.

No re-parsea texto plano, que era el defecto de fondo de la versión anterior. El
`.log` aplanaba en un solo stream los paths de archivo (datos) y las líneas de
log (metadatos), y clasificar eso con regex de subcadena producía falsos FATAL:
un nombre de Instagram con "404" adentro de un ID numérico es indistinguible de
un HTTP 404. Acá los eventos vienen tipados y cada campo tiene un solo
significado.

Las funciones puras (`resumir`, `clasificar`, `es_fatal`, `fila_csv`) no leen
config.json ni tocan disco al importarse: descarga.py las importa para escribir
su .log y su resumen en pantalla, así que lo que se ve, lo que queda guardado y
lo que se audita salen todos del mismo conteo.
"""

import csv
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# =============================================================================
# ANSI
# =============================================================================
RESET = "\033[0m"
BOLD = "\033[1m"
GRAY = "\033[90m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

# ZIP diario que arma archivar_en_zip(); purgar_zip_antiguos() lo lee de vuelta.
RE_ZIP_FECHA = re.compile(r"logs_(\d{4}-\d{2}-\d{2})\.zip$")

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
      url, nombre       <- del evento `inicio`
      duracion,
      returncode,
      timeout           <- del evento `fin` (nadie más los conoce)

    El evento `fin` NO trae contadores a propósito. En descarga.py los totales
    se calculan contando las mismas listas que producen los eventos, así que
    duplicarlos no sería una segunda medición: sería la misma, escrita dos
    veces, incapaz de detectar el error que justificaría existir. Derivarlos
    tiene además una ventaja concreta: un .jsonl truncado por el watchdog
    (sin evento `fin`) sigue reportando bien lo que alcanzó a bajar.

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

    # El nombre lo emite descarga.py en el evento `inicio`; acá NO se recalcula
    # desde la URL. Es la misma regla de nombrado con la que descarga.py bautiza
    # el .log y el .jsonl, y tenerla en un solo lado es lo que evita que el CSV
    # apunte a carpetas que no existen si algún día esa regla cambia.
    return {
        "completo": fin is not None,
        "nombre_modelo": inicio.get("nombre", ""),
        "url": inicio.get("url", ""),
        # Momento en que arrancó la descarga, no en que se auditó: es el dato
        # que hace comparable una fila del CSV con lo que pasaba esa noche.
        "ts": inicio.get("ts", ""),
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


# =============================================================================
# CONFIGURACIÓN
# =============================================================================
# Nada de esto corre al importar el módulo: descarga.py lo importa solo por
# resumir()/clasificar(), y los tests por las funciones puras. La config se lee
# recién cuando alguien pide la orquestación.


def cargar_config(ruta=None) -> dict:
    """Devuelve TODAS las rutas declaradas en config.json, sin filtrar.

    El auditar viejo se quedaba solo con log_dir/rips_dir/audit_csv y por eso
    tuvo que inventar dónde estaba posts_fallidos.json: lo buscaba junto al CSV
    mientras descarga.py lo escribía en la carpeta del script. Su aviso no
    disparó nunca. Ahora la ruta se declara una vez y los dos la leen.
    """
    ruta = Path(ruta) if ruta else Path(__file__).parent / "config.json"
    if not ruta.exists():
        print(f"  {RED}[X] No se encontró config.json en: {ruta}{RESET}")
        sys.exit(1)
    with open(ruta, encoding="utf-8") as f:
        data = json.load(f)
    entorno = "windows" if sys.platform == "win32" else "linux"
    return {k: Path(os.path.expanduser(v)) for k, v in data[entorno]["paths"].items()}


# =============================================================================
# CSV
# =============================================================================


def registrar_filas_en_csv(csv_path, filas: list):
    """Append de todas las filas del lote en una sola apertura.

    El `sep=;` de la primera línea es para que Excel abra el archivo con las
    columnas separadas sin preguntar. Reintenta unas veces porque el CSV puede
    estar abierto en Excel justo cuando termina un lote.
    """
    if not filas:
        return
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        try:
            with open(csv_path, "a+", newline="", encoding="utf-8") as f:
                f.seek(0)
                if not f.read(4):
                    f.write("sep=;\n")
                    csv.writer(f, delimiter=";").writerow(CSV_HEADER)
                f.seek(0, os.SEEK_END)
                w = csv.writer(f, delimiter=";")
                for fila in filas:
                    w.writerow(fila)
                f.flush()
                os.fsync(f.fileno())
            return
        except OSError:
            time.sleep(0.1)


# =============================================================================
# MANTENIMIENTO
# =============================================================================


def archivar_en_zip(log_dir, rutas: list) -> int:
    """Comprime los logs del lote en el ZIP del día y los borra del directorio.

    Entran el .jsonl y el .log de cada URL: el .jsonl es el dato y el .log es la
    vista, y archivar solo uno dejaría la mitad de la corrida sin respaldo.
    """
    rutas = [Path(r) for r in rutas if Path(r).exists()]
    if not rutas:
        return 0
    zip_file = Path(log_dir) / f"logs_{datetime.now().strftime('%Y-%m-%d')}.zip"
    try:
        with zipfile.ZipFile(zip_file, "a", compression=zipfile.ZIP_DEFLATED) as z:
            ts = datetime.now().strftime("%H%M%S")
            for ruta in rutas:
                z.write(ruta, arcname=f"{ts}_{ruta.name}")
    except OSError as e:
        print(f"  {RED}[X] Error comprimiendo logs: {e}{RESET}")
        return 0
    # Recién se borran con el ZIP ya cerrado: si la escritura falla, los
    # originales siguen en disco.
    for ruta in rutas:
        try:
            ruta.unlink()
        except OSError:
            pass
    return len(rutas)


def purgar_zip_antiguos(log_dir, dias: int = 60) -> int:
    log_dir = Path(log_dir)
    if not log_dir.exists():
        return 0
    ahora = datetime.now()
    purgados = 0
    for archivo in log_dir.iterdir():
        m = RE_ZIP_FECHA.match(archivo.name)
        if not m:
            continue
        try:
            if (ahora - datetime.strptime(m.group(1), "%Y-%m-%d")).days > dias:
                archivo.unlink()
                purgados += 1
        except (ValueError, OSError):
            pass
    return purgados


def buscar_part_huerfanos(rips_dir) -> list:
    """Un .part sin proceso vivo detrás es una descarga que quedó a mitad."""
    rips_dir = Path(rips_dir)
    if not rips_dir.exists():
        return []
    return [
        str(Path(raiz) / archivo)
        for raiz, _, archivos in os.walk(rips_dir)
        if "logs" not in raiz.lower()
        for archivo in archivos
        if archivo.endswith(".part")
    ]


# =============================================================================
# ORQUESTACIÓN
# =============================================================================


def analizar_logs(cfg: dict | None = None) -> dict | None:
    """Audita los .jsonl pendientes en log_dir: CSV, ZIP y .part huérfanos."""
    cfg = cfg or cargar_config()
    log_dir, rips_dir = Path(cfg["log_dir"]), Path(cfg["rips_dir"])

    if not log_dir.exists():
        print(f"\n  {RED}[X] Directorio de logs no existe: {log_dir}{RESET}\n")
        return None

    jsonls = sorted(log_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    huerfanos = buscar_part_huerfanos(rips_dir)

    if not jsonls and not huerfanos:
        print(f"\n  {GREEN}[+] Sin logs nuevos ni archivos .part huérfanos.{RESET}\n")
        # El aviso también va acá: los posts fallidos son un pendiente
        # ACUMULADO, no un subproducto del lote que se acaba de auditar. Este
        # return cortaba antes de la llamada del final, así que correr
        # `python auditar.py` para revisar (el uso documentado) contestaba
        # "todo bien" teniendo posts anotados en el reporte.
        avisar_posts_fallidos(cfg.get("posts_fallidos_file"))
        return None

    filas = []
    a_comprimir = []
    conteo = dict.fromkeys(ESTADOS, 0)
    conteo["incompletos"] = 0

    for ruta in jsonls:
        try:
            resumen = resumir(leer_eventos(ruta))
        except OSError as e:
            print(f"  {YELLOW}[!] No se pudo leer {ruta.name}: {e}{RESET}")
            continue

        # La fecha es la de la corrida (evento `inicio`), no la de la auditoría.
        # Si el .jsonl se truncó antes del `inicio`, cae al mtime del archivo.
        ts = resumen["ts"].replace("T", " ") or datetime.fromtimestamp(
            ruta.stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S")

        filas.append(fila_csv(resumen, ts))
        conteo[clasificar(resumen)] += 1
        if not resumen["completo"]:
            conteo["incompletos"] += 1

        a_comprimir.append(ruta)
        log_texto = ruta.with_suffix(".log")
        if log_texto.exists():
            a_comprimir.append(log_texto)

    # .log sueltos sin su .jsonl: no generan fila (no hay eventos que resumir)
    # pero se archivan igual, para que log_dir no los acumule para siempre.
    sueltos = [p for p in log_dir.glob("*.log") if p not in a_comprimir]

    registrar_filas_en_csv(cfg["audit_csv"], filas)
    comprimidos = archivar_en_zip(log_dir, a_comprimir + sueltos)
    purgados = purgar_zip_antiguos(log_dir)

    imprimir_reporte(conteo, huerfanos, len(filas), len(sueltos), purgados)
    avisar_posts_fallidos(cfg.get("posts_fallidos_file"))
    return {"filas": len(filas), "comprimidos": comprimidos, "huerfanos": huerfanos}


def avisar_posts_fallidos(ruta):
    """Aviso al final del lote. La ruta viene del config, no se adivina."""
    if not ruta or not Path(ruta).exists():
        return
    try:
        with open(ruta, encoding="utf-8") as f:
            fallidos = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    total = sum(len(v.get("posts_con_error", [])) for v in fallidos.values())
    if not total:
        return
    print(
        f"  {YELLOW}⚠️  {total} post(s) con errores en {len(fallidos)} URL(s) "
        f"— ver posts_fallidos.json{RESET}"
    )
    # NO dice "copiar a skip_posts.json": ese archivo espera la posición ordinal
    # del post en el hilo, no su id. El link es el primer paso del workflow.
    print(
        f"  {GRAY}    Abrir el post_url y anotar en skip_posts.json el #N "
        f"que muestra XenForo{RESET}\n"
    )


# =============================================================================
# REPORTE
# =============================================================================


def imprimir_reporte(conteo: dict, huerfanos: list, total, sueltos=0, purgados=0):
    print(f"\n{BOLD}{'═' * 55}{RESET}")
    print(f"{BOLD}{MAGENTA}  REPORTE DE AUDITORÍA{RESET}")
    print(f"{GRAY}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{RESET}")
    print(f"{BOLD}{'═' * 55}{RESET}\n")

    print(f"  Logs auditados      : {BOLD}{total}{RESET}")
    print(f"  OK                  : {BOLD}{GREEN}{conteo['OK']}{RESET}")
    print(f"  Transitorios        : {BOLD}{YELLOW}{conteo['TRANSITORIO']}{RESET}")
    print(f"  Timeouts            : {BOLD}{CYAN}{conteo['TIMEOUT']}{RESET}")
    print(f"  Fatales             : {BOLD}{RED}{conteo['FATAL']}{RESET}")

    if conteo.get("incompletos"):
        print(
            f"  Sin cierre          : {BOLD}{GRAY}{conteo['incompletos']}{RESET}"
            f"  {GRAY}(el proceso murió antes de terminar){RESET}"
        )
    if sueltos:
        print(
            f"  .log sin .jsonl     : {BOLD}{GRAY}{sueltos}{RESET}"
            f"  {GRAY}(archivados sin auditar){RESET}"
        )
    if purgados:
        print(
            f"  ZIPs purgados       : {BOLD}{GRAY}{purgados}{RESET} {GRAY}(+60d){RESET}"
        )

    print()

    if huerfanos:
        sep = "\\" if sys.platform == "win32" else "/"
        print(f"  {YELLOW}Archivos .part huérfanos: {len(huerfanos)}{RESET}")
        for path in huerfanos:
            p = Path(path)
            print(
                f"    {RED}└──{RESET} {GRAY}...{sep}{p.parent.name}{sep}{p.name}{RESET}"
            )
        print()
    else:
        print(f"  Archivos .part      : {BOLD}{GREEN}0{RESET}\n")

    print(f"{BOLD}{'═' * 55}{RESET}\n")


def main():
    analizar_logs()


if __name__ == "__main__":
    main()
