"""ms-evaluaciones · tareas, entregas y calificaciones.  PRODUCTOR PRINCIPAL de eventos.

Base de datos: Amazon RDS MySQL, un ESQUEMA POR TENANT (evaluaciones_utec, evaluaciones_norte, ...).
  tareas(tarea_id PK, curso_id, fecha_limite, puntaje_max ...)
  entregas(tarea_id, alumno_id PK, FK tarea, idx alumno_id)   -> ON DELETE CASCADE
Notas, entregas y tareas son relacionales y requieren integridad: por eso SQL aquí.

Eventos que publica (payload con el "resumen" del alumno -> el consumidor es una función pura
del evento, por eso es idempotente aunque SQS lo entregue dos veces):
  EntregaRegistrada · NotaPublicada
"""
import uuid
from decimal import Decimal, InvalidOperation

from common import (ApiError, body, exigir_rol, now_iso, path_params, protegido, publicar_evento,
                    query_params, requerido, verificar_tenant)
from sqldb import Tenant, actualizar, crear_cursor, fila, leer_cursor

SERVICIO = "evaluaciones"


def _db(ctx):
    return Tenant(SERVICIO, ctx["tenant_id"])


def _tarea(f, tenant_id):
    t = fila(f, tenant_id, ("publicada",))
    t["curso_tarea"] = f"{t['curso_id']}#{t['tarea_id']}"
    return t


def _entrega(f, tenant_id):
    e = fila(f, tenant_id, ("es_tardia",))
    e["tarea_alumno"], e["alumno_tarea"] = f"{e['tarea_id']}#{e['alumno_id']}", f"{e['alumno_id']}#{e['tarea_id']}"
    return e


def _tarea_o_404(t, curso_id, tarea_id):
    f = t.uno("SELECT * FROM {s}.tareas WHERE curso_id = %s AND tarea_id = %s", (curso_id, tarea_id))
    if not f:
        raise ApiError(404, "TAREA_NO_ENCONTRADA", "La tarea no existe en tu tenant")
    return _tarea(f, t.tenant_id)


def resumen_alumno(t, alumno_id):
    """Indicadores del alumno calculados desde SUS entregas (índice idx_alumno)."""
    items = t.todos("SELECT estado, nota, calificado_en, es_tardia FROM {s}.entregas WHERE alumno_id = %s",
                    (alumno_id,))
    cal = sorted((i for i in items if i["estado"] == "CALIFICADA" and i["nota"] is not None),
                 key=lambda i: i["calificado_en"] or "")
    n = len(cal)
    promedio = round(float(sum(float(i["nota"]) for i in cal)) / n, 2) if n else None
    caida = 0.0
    if n >= 2:
        previo = float(sum(float(i["nota"]) for i in cal[:-1])) / (n - 1)
        caida = round(max(0.0, previo - float(cal[-1]["nota"])), 2)
    return {"n_entregas": len(items), "n_notas": n, "promedio": promedio,
            "n_tardias": sum(1 for i in items if i["es_tardia"]), "caida_ultima": caida}


# ---------------------------------------------------------------- tareas
@protegido
def crear_tarea(event, ctx):
    exigir_rol(ctx, "docente", "admin")
    d = body(event)
    verificar_tenant(ctx, d)
    t = _db(ctx)
    cid = path_params(event)["curso_id"]
    tarea_id = "T" + uuid.uuid4().hex[:6].upper()  # único aunque se creen en paralelo
    item = {
        "tarea_id": tarea_id, "curso_id": cid, "titulo": requerido(d, "titulo", 160),
        "descripcion": str(d.get("descripcion", ""))[:1000],
        "fecha_limite": requerido(d, "fecha_limite", 20),  # ISO UTC: 2026-11-30T23:59:00Z
        "puntaje_max": Decimal(str(d.get("puntaje_max", 20))), "docente_id": ctx["user_id"],
        "docente_nombre": ctx["nombre"], "publicada": 1, "creado_en": now_iso(),
    }
    cols = list(item)
    t.ejecutar(f"INSERT INTO {{s}}.tareas ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
               [item[c] for c in cols])
    return 201, _tarea(item, ctx["tenant_id"])


@protegido
def listar_tareas(event, ctx):
    cid = path_params(event)["curso_id"]
    t = _db(ctx)
    filas = t.todos("SELECT * FROM {s}.tareas WHERE curso_id = %s ORDER BY fecha_limite", (cid,))
    return {"curso_id": cid, "tareas": [_tarea(f, ctx["tenant_id"]) for f in filas]}


# --------------------------------------------------------------- entregas
@protegido
def registrar_entrega(event, ctx):
    exigir_rol(ctx, "alumno")
    p = path_params(event)
    d = body(event)
    verificar_tenant(ctx, d)
    t = _db(ctx)
    tarea = _tarea_o_404(t, p["curso_id"], p["tarea_id"])
    texto, archivo = str(d.get("texto", ""))[:4000], str(d.get("archivo_nombre", ""))[:120]
    if not texto.strip() and not archivo:
        raise ApiError(400, "ENTREGA_VACIA", "Envía texto o el nombre de un archivo")
    previa = t.uno("SELECT estado FROM {s}.entregas WHERE tarea_id = %s AND alumno_id = %s",
                   (tarea["tarea_id"], ctx["user_id"]))
    if previa and previa["estado"] == "CALIFICADA":
        raise ApiError(409, "YA_CALIFICADA", "La entrega ya fue calificada; no se puede reemplazar")
    ahora = now_iso()
    tardia = int(ahora > tarea["fecha_limite"])
    if previa:   # reemplaza una entrega aún no calificada
        t.ejecutar("UPDATE {s}.entregas SET texto = %s, archivo_nombre = %s, entregado_en = %s, es_tardia = %s, "
                   "estado = 'ENTREGADA' WHERE tarea_id = %s AND alumno_id = %s",
                   (texto, archivo, ahora, tardia, tarea["tarea_id"], ctx["user_id"]))
    else:
        t.ejecutar("INSERT INTO {s}.entregas (tarea_id, alumno_id, curso_id, alumno_nombre, titulo_tarea, "
                   "docente_id, estado, texto, archivo_nombre, entregado_en, es_tardia) "
                   "VALUES (%s, %s, %s, %s, %s, %s, 'ENTREGADA', %s, %s, %s, %s)",
                   (tarea["tarea_id"], ctx["user_id"], tarea["curso_id"], ctx["nombre"], tarea["titulo"],
                    tarea["docente_id"], texto, archivo, ahora, tardia))
    item = _entrega(t.uno("SELECT * FROM {s}.entregas WHERE tarea_id = %s AND alumno_id = %s",
                          (tarea["tarea_id"], ctx["user_id"])), ctx["tenant_id"])
    publicar_evento("EntregaRegistrada", ctx["tenant_id"], {
        "alumno_id": ctx["user_id"], "alumno_nombre": ctx["nombre"], "curso_id": tarea["curso_id"],
        "tarea_id": tarea["tarea_id"], "titulo": tarea["titulo"], "docente_id": tarea["docente_id"],
        "es_tardia": item["es_tardia"], "resumen": resumen_alumno(t, ctx["user_id"])})
    return 201, item


@protegido
def listar_entregas(event, ctx):
    exigir_rol(ctx, "docente", "admin")
    p = path_params(event)
    t = _db(ctx)
    tarea = _tarea_o_404(t, p["curso_id"], p["tarea_id"])
    limite, ultima = leer_cursor(query_params(event), ctx["tenant_id"])
    filas = t.todos("SELECT * FROM {s}.entregas WHERE tarea_id = %s AND alumno_id > %s ORDER BY alumno_id LIMIT %s",
                    (tarea["tarea_id"], ultima or "", limite + 1))
    hay_mas = len(filas) > limite
    filas = filas[:limite]
    return {"tarea": tarea, "entregas": [_entrega(f, ctx["tenant_id"]) for f in filas],
            "cursor": crear_cursor(ctx["tenant_id"], filas[-1]["alumno_id"]) if hay_mas else None}


@protegido
def calificar_entrega(event, ctx):
    exigir_rol(ctx, "docente", "admin")  # T-ISO-5: un alumno recibe 403 ROL_NO_AUTORIZADO
    p = path_params(event)
    d = body(event)
    verificar_tenant(ctx, d)
    t = _db(ctx)
    tarea = _tarea_o_404(t, p["curso_id"], p["tarea_id"])
    if ctx["rol"] == "docente" and tarea["docente_id"] != ctx["user_id"]:
        raise ApiError(403, "NO_ES_DOCENTE_DEL_CURSO", "Solo el docente de la tarea puede calificar")
    try:
        nota = Decimal(str(d["nota"]))
    except (KeyError, InvalidOperation):
        raise ApiError(400, "NOTA_INVALIDA", "nota debe ser numérica")
    if nota < 0 or nota > Decimal(str(tarea["puntaje_max"])):
        raise ApiError(400, "NOTA_FUERA_DE_RANGO", f"La nota debe estar entre 0 y {tarea['puntaje_max']}")
    actualizar(t, "entregas", {"tarea_id": tarea["tarea_id"], "alumno_id": p["alumno_id"]},
               {"nota": nota, "retroalimentacion": str(d.get("retroalimentacion", ""))[:1000],
                "estado": "CALIFICADA", "calificado_por": ctx["user_id"], "calificado_en": now_iso()},
               ["nota", "retroalimentacion", "estado", "calificado_por", "calificado_en"])
    entrega = _entrega(t.uno("SELECT * FROM {s}.entregas WHERE tarea_id = %s AND alumno_id = %s",
                             (tarea["tarea_id"], p["alumno_id"])), ctx["tenant_id"])
    publicar_evento("NotaPublicada", ctx["tenant_id"], {
        "alumno_id": p["alumno_id"], "alumno_nombre": entrega.get("alumno_nombre", ""),
        "curso_id": tarea["curso_id"], "tarea_id": tarea["tarea_id"], "titulo": tarea["titulo"],
        "nota": nota, "docente_id": ctx["user_id"], "resumen": resumen_alumno(t, p["alumno_id"])})
    return entrega


@protegido
def mis_notas(event, ctx):
    qs = query_params(event)
    verificar_tenant(ctx, qs)
    t = _db(ctx)
    alumno_id = ctx["user_id"] if ctx["rol"] == "alumno" else requerido(qs, "alumno_id", 40)
    filas = t.todos("SELECT * FROM {s}.entregas WHERE alumno_id = %s ORDER BY tarea_id LIMIT 2000", (alumno_id,))
    return {"alumno_id": alumno_id, "entregas": [_entrega(f, ctx["tenant_id"]) for f in filas],
            "resumen": resumen_alumno(t, alumno_id)}
