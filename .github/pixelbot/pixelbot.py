#!/usr/bin/env python3
"""pixelbot: valida y fusiona automáticamente las pull requests de la Pixel War.

Lo ejecuta el workflow .github/workflows/pixelbot.yml. Solo usa la API de GitHub:
NUNCA descarga ni ejecuta el código de las pull requests (que vienen de forks).

Reglas (configurables con variables del repositorio, ver la guía):
  - La PR solo puede cambiar UNA línea de index.html: dejar una celda como en el dibujo objetivo
    (objetivo.js). Puede ser pintar una celda vacía, corregir una celda de color incorrecto
    o quitar el color de una celda de fondo pintada por error.
  - Fase 1: como mucho MAX_PR_FASE1 celdas por alumno, con al menos DIAS_ENTRE_PR_FASE1 × 24 horas entre una y otra.
  - Sprint final (desde FECHA_SPRINT): sin límite, con un cooldown de COOLDOWN_SPRINT_MIN minutos.
  - Si la PR tiene conflictos, se avisa y se espera a que el alumno los resuelva.
Las PR válidas que no pueden fusionarse todavía se quedan abiertas y se fusionan solas más adelante.

Sabotaje (opcional, si existe SABOTAJE_CADA_MIN): en las ejecuciones programadas o manuales, el bot
pinta de un color incorrecto una celda vacía al azar, como mucho una vez cada SABOTAJE_CADA_MIN minutos
y nunca si ya hay SABOTAJE_MAX celdas incorrectas. Así aparecen conflictos y celdas que arreglar.
"""
import base64
import json
import os
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
RAIZ = Path(__file__).resolve().parents[2]
ARCHIVO = "index.html"
BOT = "github-actions[bot]"
TZ = ZoneInfo(os.environ.get("ZONA_HORARIA") or "Europe/Madrid")


def _usuarios(valor):
    return {u.lstrip("@").lower() for u in re.split(r"[\s,]+", valor or "") if u.strip()}


ALUMNOS = _usuarios(os.environ.get("ALUMNOS"))
MAX_PR = int(os.environ.get("MAX_PR_FASE1") or 2)
DIAS_ENTRE = int(os.environ.get("DIAS_ENTRE_PR_FASE1") or 2)
FECHA_SPRINT = date.fromisoformat(os.environ["FECHA_SPRINT"]) if os.environ.get("FECHA_SPRINT") else None
COOLDOWN = timedelta(minutes=int(os.environ.get("COOLDOWN_SPRINT_MIN") or 60))
SABOTAJE_CADA = (timedelta(minutes=int(os.environ["SABOTAJE_CADA_MIN"]))
                 if os.environ.get("SABOTAJE_CADA_MIN") else None)
SABOTAJE_MAX = int(os.environ.get("SABOTAJE_MAX") or 5)
PREFIJO_SABOTAJE = "Sabotaje del bot:"
PREFIJO_SABOTAJE_DIRIGIDO = "Sabotaje dirigido del bot:"  # distinto, para no reiniciar el reloj del sabotaje aleatorio
# Si existe, en la PR número N de cada alumno (y siguientes) el bot provoca un conflicto antes de fusionar.
CONFLICTO_DESDE_PR = int(os.environ.get("CONFLICTO_DESDE_PR") or 0)

CELDA_RE = re.compile(
    r'^\s*<div class="cell(?: (?P<color>[a-z-]+))?" id="c-(?P<fila>\d+)-(?P<col>\d+)" title="[^"]*"></div>\s*$'
)
ESTADOS = ["pintado", "no-valida", "conflicto", "en-espera"]


# --- Dibujo objetivo -------------------------------------------------------

def cargar_objetivo():
    texto = (RAIZ / "objetivo.js").read_text(encoding="utf-8")
    cuerpo = re.split(r"^const OBJETIVO\s*=", texto, maxsplit=1, flags=re.M)[1]
    return json.loads(cuerpo.strip().rstrip(";"))


OBJ = cargar_objetivo()
N = len(OBJ["sprite"]) * OBJ["escala"]


def objetivo_de(fila, col):
    if not (1 <= fila <= N and 1 <= col <= N):
        return None
    letra = OBJ["sprite"][(fila - 1) // OBJ["escala"]][(col - 1) // OBJ["escala"]]
    return None if letra == "." else OBJ["leyenda"][letra]


TOTAL = sum(1 for f in range(1, N + 1) for c in range(1, N + 1) if objetivo_de(f, c))
# Para sabotear se usan los colores de la paleta que no salen en el dibujo (si no hay, cualquiera).
COLORES_SABOTAJE = [c for c in OBJ["paleta"] if c not in OBJ["leyenda"].values()] or list(OBJ["paleta"])


# --- API de GitHub ---------------------------------------------------------

def api(metodo, ruta, datos=None, raw=False) -> tuple[Any, str | None]:
    url = ruta if ruta.startswith("http") else API + ruta
    cuerpo = json.dumps(datos).encode() if datos is not None else None
    peticion = urllib.request.Request(url, data=cuerpo, method=metodo, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pixelbot",
        **({"Content-Type": "application/json"} if cuerpo else {}),
    })
    with urllib.request.urlopen(peticion) as respuesta:
        contenido = respuesta.read()
        siguiente = re.search(r'<([^>]+)>;\s*rel="next"', respuesta.headers.get("Link", ""))
        siguiente = siguiente.group(1) if siguiente else None
        if raw:
            return contenido.decode("utf-8"), siguiente
        return (json.loads(contenido) if contenido else None), siguiente


def get(ruta) -> Any:
    return api("GET", ruta)[0]


def paginar(ruta):
    resultados, url = [], ruta
    while url:
        datos, url = api("GET", url)
        resultados.extend(datos or [])
    return resultados


def fecha(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def comentado(numero, clave):
    """True si el bot ya ha dejado en la PR un comentario con esa clave."""
    marca = f"<!-- pixelbot:{clave} -->"
    return any(c["user"]["login"] == BOT and marca in (c.get("body") or "")
               for c in paginar(f"/repos/{REPO}/issues/{numero}/comments?per_page=100"))


def comentar_una_vez(numero, clave, texto):
    """Comenta en la PR salvo que el bot ya haya dejado un comentario con esa clave."""
    marca = f"<!-- pixelbot:{clave} -->"
    if comentado(numero, clave):
        return
    api("POST", f"/repos/{REPO}/issues/{numero}/comments", {"body": f"{marca}\n{texto}"})


def poner_estado(pr, estado):
    """Deja en la PR solo la etiqueta de estado indicada."""
    actuales = {etiqueta["name"] for etiqueta in pr["labels"]}
    for e in ESTADOS:
        if e != estado and e in actuales:
            api("DELETE", f"/repos/{REPO}/issues/{pr['number']}/labels/{urllib.parse.quote(e)}")
    if estado not in actuales:
        api("POST", f"/repos/{REPO}/issues/{pr['number']}/labels", {"labels": [estado]})


# --- Reglas del juego ------------------------------------------------------

class NoValida(Exception):
    pass


def validar_cambio(archivos):
    """Comprueba el diff de la PR. Devuelve (fila, columna, color) o lanza NoValida.

    color es None cuando la PR limpia una celda de fondo que estaba pintada por error.
    """
    nombres = ", ".join(f"`{a['filename']}`" for a in archivos)
    if len(archivos) != 1 or archivos[0]["filename"] != ARCHIVO:
        raise NoValida(f"La PR solo puede modificar `{ARCHIVO}`. Tu PR modifica: {nombres}.")
    a = archivos[0]
    if a["status"] != "modified" or a.get("additions") != 1 or a.get("deletions") != 1:
        raise NoValida(
            f"La PR tiene que cambiar **exactamente una línea** de `{ARCHIVO}` (una celda). "
            f"La tuya añade {a.get('additions')} líneas y borra {a.get('deletions')}. Suele pasar por una de estas dos cosas:\n"
            "- El editor ha reformateado el archivo al guardar: desactiva el formateo automático.\n"
            "- Al resolver un conflicto has cambiado también la línea de otra celda: déjala exactamente "
            "como está en `main`, aunque tenga un color incorrecto (arreglarla sería otra PR)."
        )
    lineas = (a.get("patch") or "").splitlines()
    quitada = [l[1:] for l in lineas if l.startswith("-")]
    puesta = [l[1:] for l in lineas if l.startswith("+")]
    antes = CELDA_RE.match(quitada[0]) if len(quitada) == 1 else None
    despues = CELDA_RE.match(puesta[0]) if len(puesta) == 1 else None
    if not antes or not despues:
        raise NoValida(
            "La línea que has cambiado no es una celda del lienzo, o su formato ya no es el original. "
            'Solo tienes que añadir el color dentro de la clase: `<div class="cell rojo" ...>`.'
        )
    sin_clase = lambda s: re.sub(r'class="[^"]*"', 'class=""', s.strip())
    if sin_clase(quitada[0]) != sin_clase(puesta[0]):
        raise NoValida("Solo puedes cambiar la **clase** de la celda; el resto de la línea tiene que quedar igual.")
    fila, col, color = int(despues["fila"]), int(despues["col"]), despues["color"]
    esperado = objetivo_de(fila, col)
    if not color:
        if esperado is None and antes["color"]:
            return fila, col, None  # limpia una celda de fondo pintada por error
        raise NoValida(
            f"Has quitado el color de la celda ({fila}, {col}), pero esa celda forma parte del dibujo. "
            f"Si tenía un color incorrecto, cámbialo directamente por `{esperado}`."
        )
    if color not in OBJ["paleta"]:
        validos = ", ".join(f"`{c}`" for c in OBJ["paleta"])
        raise NoValida(f"`{color}` no es un color de la paleta. Colores válidos: {validos}.")
    if esperado is None:
        raise NoValida(f"La celda ({fila}, {col}) es fondo: en el dibujo objetivo no se pinta. "
                       "Si alguien la ha pintado por error, lo que toca es quitarle el color: `class=\"cell\"`.")
    if color != esperado:
        raise NoValida(f"La celda ({fila}, {col}) tiene que ser `{esperado}`, no `{color}`. Fíjate en el panel *Objetivo*.")
    return fila, col, color


def motivo_de_espera(autor, historial, ahora):
    """Devuelve por qué la PR tiene que esperar, o None si ya le toca."""
    fechas = sorted(historial.get(autor, []))
    if FECHA_SPRINT and ahora.date() >= FECHA_SPRINT:
        if fechas and ahora - fechas[-1] < COOLDOWN:
            hora = (fechas[-1] + COOLDOWN).astimezone(TZ)
            minutos = int(COOLDOWN.total_seconds() // 60)
            return (f"Estamos en el sprint final: puedes pintar una celda cada {minutos} minutos. "
                    f"Tu PR se fusionará sola a partir de las {hora:%H:%M} si sigue sin conflictos.")
        return None
    if len(fechas) >= MAX_PR:
        extra = f" El {FECHA_SPRINT:%d/%m} empieza el sprint final y podrás pintar más." if FECHA_SPRINT else ""
        return f"Ya has pintado tus {MAX_PR} celdas. ¡Gracias!{extra}"
    if fechas:
        # Tiempo exacto (N × 24 horas desde la última fusión), no días del calendario.
        cuando = (fechas[-1] + timedelta(days=DIAS_ENTRE)).astimezone(TZ)
        if ahora < cuando:
            horas = DIAS_ENTRE * 24
            return (f"Entre una celda y la siguiente tienen que pasar al menos {horas} horas, y la última "
                    f"se fusionó el {fechas[-1].astimezone(TZ):%d/%m a las %H:%M}. "
                    f"Tu PR se fusionará sola a partir del {cuando:%d/%m a las %H:%M} si sigue sin conflictos.")
    return None


MENSAJE_CONFLICTO = """⚠️ Tu PR tiene **conflictos** con la rama principal: alguien (otra persona o el propio bot) ha cambiado tu misma celda o la de al lado.

Resuélvelo en tu repositorio local, igual que en la UP05:

```bash
git switch main
git pull upstream main          # traes lo que han pintado los demás
git switch NOMBRE-DE-TU-RAMA
git merge main                  # aquí aparece el conflicto
# Abre index.html y resuélvelo (mira abajo)
git add index.html
git commit
git push origin NOMBRE-DE-TU-RAMA
```

Al resolverlo, **tu celda** tiene que quedar con tu cambio y **todas las demás** exactamente como están en `main`, aunque alguna tenga un color incorrecto (arreglarla sería otra PR). Borra también las marcas `<<<<<<<`, `=======` y `>>>>>>>`.

Si `upstream` no existe, créalo antes: `git remote add upstream https://github.com/{repo}.git`

Si la celda que querías ya la ha arreglado otra persona, elige otra. Cuando hagas `push`, volveré a revisar tu PR."""


# --- Proceso ---------------------------------------------------------------

class Contexto:
    def __init__(self):
        self.rama = get(f"/repos/{REPO}")["default_branch"]
        self.ahora = datetime.now(TZ)
        self.fusionadas = 0
        self.sabotajes_dirigidos = 0
        texto, _ = api("GET", f"/repos/{REPO}/contents/{ARCHIVO}?ref={self.rama}", raw=True)
        self.lienzo = {}
        for linea in texto.splitlines():
            m = CELDA_RE.match(linea)
            if m:
                self.lienzo[(int(m["fila"]), int(m["col"]))] = m["color"]
        self.historial = {}
        for pr in paginar(f"/repos/{REPO}/pulls?state=closed&per_page=100"):
            if pr.get("merged_at") and any(e["name"] == "pintado" for e in pr["labels"]):
                self.historial.setdefault(pr["user"]["login"].lower(), []).append(fecha(pr["merged_at"]))

    def incorrectas(self):
        return sum(1 for (f, c), color in self.lienzo.items() if color and color != objetivo_de(f, c))

    def progreso(self):
        correctas = sum(1 for (f, c), color in self.lienzo.items() if color and color == objetivo_de(f, c))
        texto = f"{correctas}/{TOTAL} celdas ({round(100 * correctas / TOTAL)} %)"
        malas = self.incorrectas()
        return texto + (f" · {malas} celdas con color incorrecto por arreglar" if malas else "")


def procesar(pr, ctx):
    numero, sha = pr["number"], pr["head"]["sha"]
    autor = pr["user"]["login"].lower()
    if pr.get("draft") or pr["base"]["ref"] != ctx.rama:
        return
    if pr["author_association"] in ("OWNER", "MEMBER", "COLLABORATOR"):
        return  # PR del profesorado: se revisan a mano
    if ALUMNOS and autor not in ALUMNOS:
        poner_estado(pr, "no-valida")
        comentar_una_vez(numero, f"lista:{sha}",
                         f"@{pr['user']['login']}, tu usuario no está en la lista de la clase. Si es un error, avisa al profesor.")
        return

    try:
        fila, col, color = validar_cambio(paginar(f"/repos/{REPO}/pulls/{numero}/files?per_page=100"))
        if ctx.lienzo.get((fila, col)) == color:
            ya = f"ya está pintada de `{color}`" if color else "ya está limpia"
            raise NoValida(f"La celda ({fila}, {col}) {ya}: alguien se te ha adelantado. "
                           "Actualiza tu rama con `upstream/main` y elige otra celda.")
    except NoValida as error:
        poner_estado(pr, "no-valida")
        comentar_una_vez(numero, f"no-valida:{sha}",
                         f"❌ Esta PR no se puede fusionar:\n\n{error}\n\nCorrígelo y haz `push` a la misma rama: volveré a revisarla.")
        return

    espera = motivo_de_espera(autor, ctx.historial, ctx.ahora)
    if espera:
        poner_estado(pr, "en-espera")
        comentar_una_vez(numero, f"en-espera:{sha}:{ctx.ahora.date()}", f"⏳ {espera}")
        return

    # Conflicto didáctico: en la PR número CONFLICTO_DESDE_PR (y siguientes) de cada alumno, el bot
    # toca antes la misma celda en main, así que la PR tiene conflicto y hay que resolverlo.
    if (CONFLICTO_DESDE_PR and len(ctx.historial.get(autor, [])) + 1 >= CONFLICTO_DESDE_PR
            and not comentado(numero, f"conflicto-forzado:{numero}")):
        malo = random.choice([c for c in COLORES_SABOTAJE if c != color])
        if pintar_mal(ctx, fila, col, malo, PREFIJO_SABOTAJE_DIRIGIDO):
            ctx.sabotajes_dirigidos += 1
            poner_estado(pr, "conflicto")
            comentar_una_vez(numero, f"conflicto-forzado:{numero}", "🤖 ¡Sabotaje! Acabo de pintar tu celda "
                             f"({fila}, {col}) de `{malo}` en `main`, así que tu PR ahora tiene un conflicto.")
            comentar_una_vez(numero, f"conflicto:{sha}", MENSAJE_CONFLICTO.format(repo=REPO))
            print(f"#{numero}: conflicto forzado en ({fila}, {col}) ({autor})")
        return

    detalle = get(f"/repos/{REPO}/pulls/{numero}")
    if detalle["mergeable"] is None:
        return  # GitHub aún está calculando si hay conflictos: se mira en la próxima ejecución
    if not detalle["mergeable"]:
        poner_estado(pr, "conflicto")
        comentar_una_vez(numero, f"conflicto:{sha}", MENSAJE_CONFLICTO.format(repo=REPO))
        return

    accion = f"pinta la celda ({fila}, {col}) de {color}" if color else f"limpia la celda ({fila}, {col})"
    try:
        # "merge" y no "squash": con squash, el commit original del alumno no entra en el
        # repositorio y su siguiente PR (si sale de una rama que aún lo contiene) mostraría
        # dos líneas cambiadas y sería rechazada.
        api("PUT", f"/repos/{REPO}/pulls/{numero}/merge", {
            "merge_method": "merge",
            "sha": sha,
            "commit_title": f"{accion[0].upper()}{accion[1:]} (#{numero})",
        })
    except urllib.error.HTTPError as error:
        print(f"#{numero}: no se pudo fusionar (HTTP {error.code}); se reintentará en la próxima ejecución.")
        return

    ctx.lienzo[(fila, col)] = color
    ctx.historial.setdefault(autor, []).append(datetime.now(timezone.utc))
    ctx.fusionadas += 1
    poner_estado(pr, "pintado")
    hecho = f"pintada de `{color}`" if color else "limpia"
    comentar_una_vez(numero, f"pintado:{numero}",
                     f"✅ ¡Celda ({fila}, {col}) {hecho}! Progreso del dibujo: {ctx.progreso()}.")
    print(f"#{numero}: fusionada, {accion} ({autor})")


def sabotear(ctx):
    """Pinta de un color incorrecto una celda vacía al azar. Devuelve cuántas celdas ha pintado (0 o 1)."""
    if not SABOTAJE_CADA:
        return 0
    if ctx.incorrectas() >= SABOTAJE_MAX:
        print(f"Sin sabotaje: ya hay {ctx.incorrectas()} celdas incorrectas (máximo {SABOTAJE_MAX}).")
        return 0
    desde = (ctx.ahora - SABOTAJE_CADA).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    recientes = paginar(f"/repos/{REPO}/commits?sha={ctx.rama}&path={ARCHIVO}&since={desde}&per_page=100")
    if any(c["commit"]["message"].startswith(PREFIJO_SABOTAJE) for c in recientes):
        return 0  # el último sabotaje fue hace menos de SABOTAJE_CADA_MIN minutos

    vacias = [posicion for posicion, color in ctx.lienzo.items() if not color]
    if not vacias:
        return 0
    fila, col = random.choice(vacias)
    color = random.choice([c for c in COLORES_SABOTAJE if c != objetivo_de(fila, col)])
    if not pintar_mal(ctx, fila, col, color, PREFIJO_SABOTAJE):
        return 0
    print(f"Sabotaje: celda ({fila}, {col}) pintada de {color}")
    return 1


def pintar_mal(ctx, fila, col, color, prefijo):
    """Commit directo en la rama principal que deja la celda con `color`. Devuelve True si lo ha hecho."""
    # Se lee el archivo de nuevo: puede haber cambiado con las fusiones de esta misma ejecución.
    archivo = get(f"/repos/{REPO}/contents/{ARCHIVO}?ref={ctx.rama}")
    texto = base64.b64decode(archivo["content"]).decode("utf-8")
    texto, cambios = re.subn(rf'(<div class="cell)(?: [a-z-]+)?(" id="c-{fila}-{col}")', rf"\1 {color}\2", texto, count=1)
    if cambios != 1:
        return False  # no se ha encontrado la celda; se intentará en la próxima ejecución
    try:
        api("PUT", f"/repos/{REPO}/contents/{ARCHIVO}", {
            "message": f"{prefijo} pinta la celda ({fila}, {col}) de {color}",
            "content": base64.b64encode(texto.encode("utf-8")).decode("ascii"),
            "sha": archivo["sha"],
            "branch": ctx.rama,
        })
    except urllib.error.HTTPError as error:
        print(f"No se pudo sabotear (HTTP {error.code}). ¿Está protegida la rama {ctx.rama}?")
        return False
    ctx.lienzo[(fila, col)] = color
    return True


def main():
    ctx = Contexto()
    es_evento_pr = os.environ.get("GITHUB_EVENT_NAME") == "pull_request_target"
    if es_evento_pr:
        # Evento de una PR concreta: solo se revisa esa (ahorra llamadas a la API).
        evento = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        prs = [get(f"/repos/{REPO}/pulls/{evento['pull_request']['number']}")]
        prs = [pr for pr in prs if pr["state"] == "open"]
    else:
        # Ejecución programada o manual: se revisan todas las PR abiertas, de la más antigua a la más nueva.
        prs = paginar(f"/repos/{REPO}/pulls?state=open&sort=created&direction=asc&per_page=100")
    for pr in prs:
        try:
            procesar(pr, ctx)
        except urllib.error.HTTPError as error:
            print(f"#{pr['number']}: error de la API (HTTP {error.code}); se reintentará.")
    # El sabotaje va al final, sobre el lienzo ya actualizado, y solo en las ejecuciones periódicas.
    saboteadas = 0 if es_evento_pr else sabotear(ctx)
    print(f"PR fusionadas en esta ejecución: {ctx.fusionadas}. Progreso: {ctx.progreso()}")
    salida = os.environ.get("GITHUB_OUTPUT")
    if salida:
        with open(salida, "a", encoding="utf-8") as f:
            f.write(f"fusionadas={ctx.fusionadas}\n")
            f.write(f"cambios={ctx.fusionadas + saboteadas + ctx.sabotajes_dirigidos}\n")


if __name__ == "__main__":
    main()
