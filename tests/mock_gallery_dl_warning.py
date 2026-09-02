#!/usr/bin/env python3
"""
Mock de gallery-dl para el escenario de FUSIÓN warning+error.

Simula el caso documentado en descarga.py: un warning transitorio de
[downloader.http] seguido de un error sobre el MISMO post_id. descarga.py
debe fusionar ambos en una sola línea ("<error> — <causa>") en vez de
imprimir dos líneas sueltas.

Todas las líneas llevan el prefijo `post N:` porque el `output.log.format` de
gallery-dl_win.conf lo fuerza para TODAS. La versión anterior de este mock lo
omitía en el warning y en el error, y por eso no podía detectar el bug de
atribución: sin post_id en la línea, la única fuente era la variable corriente
`post_id_activo`, que es justo lo que estaba mal.

La línea de debug del post 9999 es un SEÑUELO: llega entre el warning y el
error, sobre otro post. El código viejo habría atribuido el error al 9999.
"""

import sys
import time

# 1) Log de parent-metadata: identifica el post_id activo (5555)
sys.stderr.write("[bunkr][debug] post 5555: Using archive\n")
sys.stderr.flush()
time.sleep(0.05)

# 2) Warning HTTP transitorio sobre ese post — descarga.py debe RETENERLO
#    (no imprimirlo todavía) mientras espera a ver si viene un error después.
sys.stderr.write("[downloader.http][warning] post 5555: Rate limit exceeded, retrying\n")
sys.stderr.flush()
time.sleep(0.05)

# 3) SEÑUELO: otro post entra en escena antes de que llegue el error del 5555.
sys.stderr.write("[simpcity][debug] post 9999: Sleeping 1.00 seconds\n")
sys.stderr.flush()
time.sleep(0.05)

# 4) Error sobre el post 5555 — debe fusionarse con SU warning previo y
#    atribuirse al 5555, no al 9999 que pasó por el medio.
sys.stderr.write("[downloader.http][error] post 5555: 404 Not Found\n")
sys.stderr.flush()
time.sleep(0.05)

# Un archivo nuevo, solo para que el hilo principal tenga algo en stdout.
sys.stdout.write("G:\\Rips\\Simpcity\\Modelo\\foto_fallida.jpg\n")
sys.stdout.flush()

sys.exit(1)
