"""sqldb.py · acceso a Amazon RDS (MySQL) con UN ESQUEMA POR TENANT ("Pattern 2" del curso).

Una sola instancia RDS; cada institución tiene su propio esquema por microservicio:
    cursos_utec · cursos_norte · cursos_andino        (ms-cursos)
    evaluaciones_utec · evaluaciones_norte · ...      (ms-evaluaciones)

Reglas de seguridad:
  - El nombre del esquema SOLO se deriva del tenant_id del token (nunca del cliente) y se sanea.
  - Todos los valores viajan como parámetros (%s): no se concatena nada que venga del usuario.
  - Un id de otro tenant "no existe" en tu esquema -> 404, igual que con DynamoDB.

Este archivo se COPIA idéntico en ms-cursos, ms-evaluaciones y datos-seed (como common.py).
El driver pymysql (Python puro) va empaquetado en ./vendor  ->  pip install pymysql -t vendor
Las tablas se crean solas la primera vez que un tenant usa el servicio (CREATE ... IF NOT EXISTS).
"""
import base64
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import pymysql  # noqa: E402
from pymysql.cursors import DictCursor  # noqa: E402

try:
    from common import ApiError, dumps, now_iso  # noqa: E402
except ImportError:  # datos-seed no tiene common.py: solo usa DDL, esquema() y conexion()
    from datetime import datetime, timezone

    class ApiError(Exception):
        def __init__(self, status, codigo, mensaje):
            super().__init__(f"{status} {codigo}: {mensaje}")

    def dumps(o):
        return json.dumps(o, default=str, ensure_ascii=False)

    def now_iso():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

DDL = {
    "cursos": [
        """CREATE TABLE IF NOT EXISTS {s}.cursos (
             curso_id VARCHAR(12) NOT NULL, codigo VARCHAR(20) NOT NULL, nombre VARCHAR(160) NOT NULL,
             descripcion TEXT, docente_id VARCHAR(20) NOT NULL, docente_nombre VARCHAR(80),
             ciclo VARCHAR(10), creditos INT, estado VARCHAR(12), color VARCHAR(9),
             creado_en VARCHAR(30), actualizado_en VARCHAR(30) NULL,
             PRIMARY KEY (curso_id), KEY idx_docente (docente_id)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS {s}.modulos (
             curso_id VARCHAR(12) NOT NULL, modulo_id VARCHAR(12) NOT NULL, numero INT, titulo VARCHAR(160),
             descripcion TEXT, publicado TINYINT(1) DEFAULT 1, materiales TEXT, fecha_publicacion VARCHAR(30),
             actualizado_en VARCHAR(30) NULL,
             PRIMARY KEY (curso_id, modulo_id),
             FOREIGN KEY (curso_id) REFERENCES {s}.cursos (curso_id) ON DELETE CASCADE
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS {s}.matriculas (
             curso_id VARCHAR(12) NOT NULL, alumno_id VARCHAR(20) NOT NULL, alumno_nombre VARCHAR(80),
             estado VARCHAR(12), avance_pct INT DEFAULT 0, matriculado_en VARCHAR(30),
             PRIMARY KEY (curso_id, alumno_id), KEY idx_alumno (alumno_id),
             FOREIGN KEY (curso_id) REFERENCES {s}.cursos (curso_id) ON DELETE CASCADE
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ],
    "evaluaciones": [
        """CREATE TABLE IF NOT EXISTS {s}.tareas (
             tarea_id VARCHAR(12) NOT NULL, curso_id VARCHAR(12) NOT NULL, titulo VARCHAR(160) NOT NULL,
             descripcion TEXT, fecha_limite VARCHAR(20) NOT NULL, puntaje_max DECIMAL(6,2) NOT NULL,
             docente_id VARCHAR(20), docente_nombre VARCHAR(80), publicada TINYINT(1) DEFAULT 1,
             creado_en VARCHAR(30),
             PRIMARY KEY (tarea_id), KEY idx_curso (curso_id)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS {s}.entregas (
             tarea_id VARCHAR(12) NOT NULL, alumno_id VARCHAR(20) NOT NULL, curso_id VARCHAR(12),
             alumno_nombre VARCHAR(80), titulo_tarea VARCHAR(160), docente_id VARCHAR(20),
             estado VARCHAR(12), texto TEXT, archivo_nombre VARCHAR(120), entregado_en VARCHAR(30),
             es_tardia TINYINT(1) DEFAULT 0, nota DECIMAL(6,2) NULL, retroalimentacion TEXT NULL,
             calificado_por VARCHAR(20) NULL, calificado_en VARCHAR(30) NULL, actualizado_en VARCHAR(30) NULL,
             PRIMARY KEY (tarea_id, alumno_id), KEY idx_alumno (alumno_id),
             FOREIGN KEY (tarea_id) REFERENCES {s}.tareas (tarea_id) ON DELETE CASCADE
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    ],
}

_con = None
_listos = set()


def esquema(servicio, tenant_id):
    """cursos_utec: prefijo del servicio + tenant saneado (solo a-z, 0-9 y _)."""
    slug = re.sub(r"[^a-z0-9_]", "", str(tenant_id).lower())[:40]
    if not slug:
        raise ApiError(403, "TENANT_INVALIDO", "Tenant inválido")
    return f"{servicio}_{slug}"


def conexion():
    """Conexión reutilizable entre invocaciones del mismo contenedor Lambda."""
    global _con
    if _con is None:
        _con = pymysql.connect(
            host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"], charset="utf8mb4",
            autocommit=True, connect_timeout=8, cursorclass=DictCursor)
    else:
        _con.ping(reconnect=True)
    return _con


def asegurar(servicio, tenant_id):
    """Crea el esquema del tenant y sus tablas si no existen (idempotente). Devuelve el nombre."""
    s = esquema(servicio, tenant_id)
    if s not in _listos:
        cur = conexion().cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {s} CHARACTER SET utf8mb4")
        for ddl in DDL[servicio]:
            cur.execute(ddl.format(s=s))
        _listos.add(s)
    return s


class Tenant:
    """Atajo para ejecutar SQL dentro del esquema de UN tenant. Usa {s} como marcador del esquema."""

    def __init__(self, servicio, tenant_id):
        self.tenant_id = tenant_id
        self.s = asegurar(servicio, tenant_id)

    def todos(self, sql, params=()):
        cur = conexion().cursor()
        cur.execute(sql.format(s=self.s), params)
        return list(cur.fetchall())

    def uno(self, sql, params=()):
        filas = self.todos(sql, params)
        return filas[0] if filas else None

    def ejecutar(self, sql, params=()):
        cur = conexion().cursor()
        cur.execute(sql.format(s=self.s), params)
        return cur.rowcount


def actualizar(t, tabla, claves, datos, permitidos):
    """UPDATE con lista blanca de campos. 404 si la fila no existe (nunca crea por accidente)."""
    campos = [c for c in permitidos if c in datos]
    if not campos:
        raise ApiError(400, "SIN_CAMPOS", "No enviaste ningún campo editable")
    sets = ", ".join(f"{c} = %s" for c in campos) + ", actualizado_en = %s"
    donde = " AND ".join(f"{k} = %s" for k in claves)
    n = t.ejecutar(f"UPDATE {{s}}.{tabla} SET {sets} WHERE {donde}",
                   [datos[c] for c in campos] + [now_iso()] + list(claves.values()))
    if n == 0 and not t.uno(f"SELECT 1 AS x FROM {{s}}.{tabla} WHERE {donde}", list(claves.values())):
        raise ApiError(404, "NO_ENCONTRADO", "El recurso no existe en tu tenant")


# ---------------------------------------------------------------- paginación por cursor (keyset)
def leer_cursor(params, tenant_id, limite_max=100):
    """Devuelve (limite, ultima_clave|None). El cursor lleva el tenant: uno ajeno -> 403."""
    try:
        limite = max(1, min(int(params.get("limit", 25)), limite_max))
    except ValueError:
        raise ApiError(400, "LIMIT_INVALIDO", "limit debe ser un entero")
    c = params.get("cursor")
    if not c:
        return limite, None
    try:
        d = json.loads(base64.urlsafe_b64decode(c.encode()))
        clave = d["k"]
    except Exception:  # noqa: BLE001
        raise ApiError(400, "CURSOR_INVALIDO", "cursor inválido")
    if d.get("tenant_id") != tenant_id:
        raise ApiError(403, "CURSOR_AJENO", "El cursor no pertenece a tu tenant")
    return limite, clave


def crear_cursor(tenant_id, clave):
    return base64.urlsafe_b64encode(dumps({"tenant_id": tenant_id, "k": clave}).encode()).decode()


def booleanos(fila, *campos):
    for c in campos:
        if c in fila and fila[c] is not None:
            fila[c] = bool(fila[c])
    return fila


def fila(f, tenant_id, bools=()):
    """Fila SQL -> dict de la API: sin NULL, con tenant_id y booleanos reales (igual que antes con DynamoDB)."""
    d = {k: v for k, v in f.items() if v is not None}
    d["tenant_id"] = tenant_id
    return booleanos(d, *bools)
