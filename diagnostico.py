#!/usr/bin/env python3
"""diagnostico.py — Verificación del ENTORNO REAL del pipeline.

Complemento de la suite de tests. Los tests validan la lógica con mocks;
este script consulta el sistema de verdad: qué devuelve GetDriveTypeW en
esta máquina, si gallery-dl está en el PATH, si las rutas de config.json
existen, y qué modo elegiría el monitor.

Motivación: un bug real donde es_ruta_local() comparaba contra el valor
equivocado de DRIVE_FIXED. Los tests pasaban (mockeaban el mismo valor
equivocado) y el disco local se clasificaba como NAS. Ningún mock puede
detectar una suposición errónea sobre el entorno; esto sí.

Uso:
    python diagnostico.py

No modifica nada. Solo lee e informa.
Código de salida: 0 si todo OK, 1 si hay algún problema detectado.
"""

import json
import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

if IS_WINDOWS:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Colores ──
RESET = "\033[0m"
BOLD = "\033[1m"
GRAY = "\033[90m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

OK = f"{GREEN}[OK]{RESET}"
FAIL = f"{RED}[X]{RESET}"
WARN = f"{YELLOW}[!]{RESET}"
INFO = f"{CYAN}[i]{RESET}"

# Nombres oficiales de GetDriveTypeW (Windows).
DRIVE_TYPES = {
    0: ("DRIVE_UNKNOWN", False),
    1: ("DRIVE_NO_ROOT_DIR", False),
    2: ("DRIVE_REMOVABLE", True),
    3: ("DRIVE_FIXED", True),
    4: ("DRIVE_REMOTE", False),
    5: ("DRIVE_CDROM", False),
    6: ("DRIVE_RAMDISK", True),
}

problemas = []


def seccion(titulo: str):
    print(f"\n{BOLD}{'─' * 62}{RESET}")
    print(f"{BOLD}  {titulo}{RESET}")
    print(f"{BOLD}{'─' * 62}{RESET}")


def fallo(msg: str):
    problemas.append(msg)


def expandir(valor: str) -> str:
    """Misma expansión que usa monitor.py, para comparar peras con peras."""
    return os.path.expandvars(os.path.expanduser(str(valor)))


# =============================================================================
# 1. SISTEMA
# =============================================================================


def check_sistema() -> str:
    seccion("SISTEMA")
    entorno = "windows" if IS_WINDOWS else "linux"
    print(f"  {INFO} Plataforma        : {sys.platform}")
    print(f"  {INFO} Python            : {sys.version.split()[0]}")
    print(f"  {INFO} Sección de config : {BOLD}{entorno}{RESET}")
    print(f"  {GRAY}    (config.json tiene secciones separadas por SO;{RESET}")
    print(f"  {GRAY}     las rutas se resuelven automáticamente){RESET}")
    return entorno


# =============================================================================
# 2. CONFIG.JSON
# =============================================================================


def check_config(entorno: str):
    seccion("CONFIG.JSON")
    if not CONFIG_PATH.exists():
        print(f"  {FAIL} No existe: {CONFIG_PATH}")
        fallo("config.json no encontrado")
        return None, None
    print(f"  {OK} Encontrado: {CONFIG_PATH}")

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  {FAIL} JSON inválido: {e}")
        fallo("config.json no se puede parsear")
        return None, None

    if entorno not in data:
        print(f"  {FAIL} Falta la sección '{entorno}'")
        fallo(f"config.json sin sección '{entorno}'")
        return None, None

    cfg = data[entorno]
    paths = cfg.get("paths", {})
    gdl = cfg.get("gallery_dl", {})

    print(f"\n  {BOLD}Rutas declaradas:{RESET}")
    # Estos deben existir para que el pipeline funcione.
    obligatorios = {"rips_dir", "log_dir"}
    # Estos se crean solos en el primer uso; su ausencia no es error.
    opcionales = {"state_file", "audit_csv", "skip_posts_file", "posts_fallidos_file"}

    for clave, valor in paths.items():
        ruta = Path(expandir(valor))
        existe = ruta.exists()
        if existe:
            marca = OK
        elif clave in opcionales:
            marca = f"{GRAY}[-]{RESET}"
        else:
            marca = FAIL
        print(f"    {marca} {clave:18s} {ruta}")
        if not existe:
            if clave in obligatorios:
                fallo(f"ruta obligatoria no existe: {clave} -> {ruta}")
            elif clave not in opcionales:
                print(f"        {GRAY}(se creará al usarse){RESET}")

    return paths, gdl


# =============================================================================
# 3. GALLERY-DL
# =============================================================================


def check_gallery_dl(gdl: dict, paths: dict):
    seccion("GALLERY-DL")
    if not gdl:
        print(f"  {FAIL} Sin sección gallery_dl en config.json")
        fallo("config.json sin sección gallery_dl")
        return

    ejecutable = gdl.get("executable", "")
    encontrado = shutil.which(ejecutable) if ejecutable else None
    if encontrado:
        print(f"  {OK} Ejecutable    : {ejecutable}")
        print(f"       {GRAY}resuelto en {encontrado}{RESET}")
    else:
        print(f"  {FAIL} Ejecutable    : '{ejecutable}' no está en el PATH")
        fallo(f"gallery-dl '{ejecutable}' no encontrado en PATH")

    conf_path = Path(expandir(gdl.get("config_file", "")))
    if conf_path.exists():
        print(f"  {OK} Config        : {conf_path}")
    else:
        print(f"  {FAIL} Config        : no existe {conf_path}")
        fallo(f"gallery-dl .conf no existe: {conf_path}")
        return

    # ── Coherencia: base-directory del .conf vs rips_dir de config.json ──
    print(f"\n  {BOLD}Coherencia de rutas:{RESET}")
    try:
        with open(conf_path, "r", encoding="utf-8") as f:
            gconf = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  {WARN} No se pudo parsear el .conf: {e}")
        return

    base_dir = gconf.get("extractor", {}).get("base-directory")
    rips_dir = paths.get("rips_dir", "") if paths else ""

    if base_dir is None:
        print(f"  {INFO} El .conf no define base-directory")
        print(f"       {GRAY}descarga.py pasa -d, así que manda config.json{RESET}")
        return

    norm_a = os.path.normcase(os.path.normpath(expandir(base_dir)))
    norm_b = os.path.normcase(os.path.normpath(expandir(rips_dir)))
    if norm_a == norm_b:
        print(f"  {OK} base-directory coincide con rips_dir")
        print(f"       {GRAY}{base_dir}{RESET}")
    else:
        print(f"  {WARN} base-directory y rips_dir DIFIEREN")
        print(f"       .conf        : {base_dir}")
        print(f"       config.json  : {rips_dir}")
        print(f"       {GRAY}descarga.py pasa -d, así que gana config.json.{RESET}")
        print(f"       {GRAY}Si corres gallery-dl a mano, irá al otro sitio.{RESET}")


# =============================================================================
# 4. DETECCIÓN LOCAL / REMOTA
# =============================================================================


def check_deteccion(paths: dict):
    seccion("DETECCIÓN LOCAL / REMOTA")
    if not paths or "rips_dir" not in paths:
        print(f"  {FAIL} Sin rips_dir para evaluar")
        return

    rips_dir = expandir(paths["rips_dir"])
    print(f"  {INFO} Ruta evaluada : {rips_dir}")

    # ── Valor crudo del sistema, sin pasar por nuestra lógica ──
    if IS_WINDOWS:
        try:
            import ctypes

            # Mismo cálculo que hace monitor.py (ruta[:2] + "\\"), para
            # consultar exactamente la misma cadena y poder comparar.
            # No se usa os.path.splitdrive: su comportamiento depende del SO.
            ruta_norm = os.path.normpath(rips_dir)
            if len(ruta_norm) >= 2 and ruta_norm[1] == ":":
                unidad = ruta_norm[:2] + "\\"
            else:
                unidad = os.path.normpath(os.path.abspath(rips_dir))[:2] + "\\"
            codigo = ctypes.windll.kernel32.GetDriveTypeW(unidad)
            nombre, es_local_esperado = DRIVE_TYPES.get(codigo, ("DESCONOCIDO", False))
            print(f"  {INFO} GetDriveTypeW : {unidad} -> {codigo} ({nombre})")
        except Exception as e:
            print(f"  {WARN} GetDriveTypeW falló: {e}")
            es_local_esperado = None
    else:
        es_local_esperado = None
        try:
            ruta_abs = os.path.abspath(rips_dir)
            mejor, fs = "", "?"
            with open("/proc/mounts", "r", encoding="utf-8") as f:
                for linea in f:
                    partes = linea.split()
                    if len(partes) < 3:
                        continue
                    mp, tipo = partes[1], partes[2]
                    if (
                        ruta_abs == mp or ruta_abs.startswith(mp.rstrip("/") + "/")
                    ) and len(mp) > len(mejor):
                        mejor, fs = mp, tipo
            print(f"  {INFO} Mount point   : {mejor or '?'} (fs: {fs})")
            es_local_esperado = fs not in (
                "nfs",
                "nfs4",
                "cifs",
                "fuse",
                "fuse.sshfs",
                "fuse.rclone",
            )
        except OSError as e:
            print(f"  {WARN} No se pudo leer /proc/mounts: {e}")

    # ── Lo que decide TU código ──
    try:
        import monitor
    except Exception as e:
        print(f"  {FAIL} No se pudo importar monitor.py: {e}")
        fallo("monitor.py no importable")
        return

    decision = monitor.es_ruta_local(rips_dir)
    print(f"  {INFO} es_ruta_local : {BOLD}{decision}{RESET}")

    if es_local_esperado is not None and decision != es_local_esperado:
        print(
            f"  {FAIL} DISCREPANCIA: el sistema dice "
            f"{'local' if es_local_esperado else 'remoto'}, "
            f"pero es_ruta_local devuelve {decision}"
        )
        fallo("es_ruta_local no coincide con lo que reporta el sistema")
    elif es_local_esperado is not None:
        print(f"  {OK} Coincide con lo que reporta el sistema")

    return decision


# =============================================================================
# 5. WATCHDOG Y MODO ELEGIDO
# =============================================================================


def check_watchdog(es_local):
    seccion("WATCHDOG Y MODO DE MONITOREO")
    try:
        import watchdog

        version = getattr(watchdog, "__version__", "(sin __version__)")
        print(f"  {OK} watchdog instalado: {version}")
        disponible = True
    except ImportError:
        print(f"  {WARN} watchdog NO instalado")
        print(f"       {GRAY}pip install watchdog{RESET}")
        print(f"       {GRAY}(opcional: sin él, el monitor usa polling){RESET}")
        disponible = False

    if es_local is None:
        return

    if not es_local:
        modo, color = "POLLING", YELLOW
        motivo = "ruta remota/NAS detectada"
    elif not disponible:
        modo, color = "POLLING", YELLOW
        motivo = "watchdog no instalado"
    else:
        modo, color = "WATCHDOG", GREEN
        motivo = "ruta local + watchdog disponible"

    print(f"\n  {INFO} crear_monitor() elegiría: {BOLD}{color}{modo}{RESET}")
    print(f"       {GRAY}motivo: {motivo}{RESET}")

    if modo == "POLLING" and es_local and not disponible:
        fallo("watchdog ausente: el monitor cae a polling en una ruta local")


# =============================================================================
# MAIN
# =============================================================================


def main():
    print(f"\n{BOLD}{'═' * 62}{RESET}")
    print(f"{BOLD}  DIAGNÓSTICO DE ENTORNO — pipeline forense{RESET}")
    print(f"{BOLD}{'═' * 62}{RESET}")

    entorno = check_sistema()
    paths, gdl = check_config(entorno)
    if paths is not None:
        check_gallery_dl(gdl, paths)
        es_local = check_deteccion(paths)
        check_watchdog(es_local)

    seccion("RESUMEN")
    if not problemas:
        print(f"  {GREEN}{BOLD}Todo en orden.{RESET} Sin problemas detectados.\n")
        return 0

    print(f"  {RED}{BOLD}{len(problemas)} problema(s) detectado(s):{RESET}\n")
    for p in problemas:
        print(f"    {RED}·{RESET} {p}")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
