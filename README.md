# ms-evaluaciones

Tareas, entregas y calificaciones. Calcula el resumen del alumno y lo viaja dentro del evento (event-carried state).

- **Base de datos:** Amazon **RDS MySQL**, un **esquema por tenant** (`evaluaciones_utec`, ...) con tablas `tareas` y `entregas` (clave foránea tarea→entrega). Se crean solas al primer uso.
- **Publica:** EntregaRegistrada, NotaPublicada
- **Consume:** —
- **Seguridad:** Lambda Authorizer de `ms-auth` en todas las rutas; el tenant sale del token, nunca del cliente.

## Endpoints
| Método | Ruta | Acceso | Handler |
|---|---|---|---|
| GET | `/evaluaciones/cursos/{curso_id}/tareas` | token | `handler.listar_tareas` |
| POST | `/evaluaciones/cursos/{curso_id}/tareas` | token | `handler.crear_tarea` |
| POST | `/evaluaciones/cursos/{curso_id}/tareas/{tarea_id}/entregas` | token | `handler.registrar_entrega` |
| GET | `/evaluaciones/cursos/{curso_id}/tareas/{tarea_id}/entregas` | token | `handler.listar_entregas` |
| PUT | `/evaluaciones/cursos/{curso_id}/tareas/{tarea_id}/entregas/{alumno_id}/calificacion` | token | `handler.calificar_entrega` |
| GET | `/evaluaciones/mis-notas` | token | `handler.mis_notas` |

## Desplegar
```bash
# con credenciales vigentes, tras infra-base (RDS) y ms-auth
export DB_PASSWORD='la-misma-de-infra-base'
pip install -r requirements.txt -t vendor          # pymysql, Python puro
npx --yes serverless@3 deploy --stage dev
```
Orden completo y atajo: `../docs-ops/deploy-all.sh`.

## Probar sin AWS
```bash
python3 ../pruebas-locales/test_e2e.py     # emulador con los serverless.yml reales
```
> `common.py` es **idéntico** en todos los servicios (la prueba lo verifica). Si lo cambias, cópialo a los demás.
