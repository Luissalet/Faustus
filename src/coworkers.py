"""Persistent owner-scoped responsibilities over the existing agent definitions.

No second runner or permissions system: missions use delegate_agents. Notes are
explicit user-maintained context; receipts never become facts automatically.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from src.constants import DATA_DIR


class Coworker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=100)
    agent: str = Field("implementer", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    responsibility: str = Field(..., min_length=1, max_length=8000)
    notes: str = Field("", max_length=16000)
    hoards: list[str] = Field(default_factory=list, max_length=40)
    state: Literal["active", "paused", "archived"] = "active"


class Store:
    def __init__(self, root=None):
        self.path = Path(root or DATA_DIR) / "coworkers.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS coworkers (owner TEXT,id TEXT,revision INTEGER,body TEXT,PRIMARY KEY(owner,id))")
            db.execute("CREATE TABLE IF NOT EXISTS coworker_runs (owner TEXT,request_id TEXT,coworker TEXT,session TEXT,mission TEXT,state TEXT,result TEXT,PRIMARY KEY(owner,request_id))")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS coworker_active ON coworker_runs(owner,coworker) WHERE state='running'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS coworker_unresolved ON coworker_runs(owner,coworker) WHERE state IN ('running','unknown')")

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    @staticmethod
    def owner(owner):
        return str(owner or "")

    def get(self, owner, id):
        with self.connect() as db:
            row = db.execute("SELECT revision,body FROM coworkers WHERE owner=? AND id=?", (self.owner(owner),id)).fetchone()
        if not row:
            raise LookupError("Coworker not found")
        return {"id":id,"revision":row[0],**json.loads(row[1])}

    def list(self, owner):
        with self.connect() as db:
            rows=db.execute("SELECT id FROM coworkers WHERE owner=? ORDER BY rowid DESC LIMIT 200",(self.owner(owner),)).fetchall()
        return [self.get(owner,row[0]) for row in rows]

    def save(self, owner, coworker, id=None, expected_revision=None):
        id=id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current=db.execute("SELECT revision FROM coworkers WHERE owner=? AND id=?",(self.owner(owner),id)).fetchone()
            if (current and current[0]!=expected_revision) or (not current and expected_revision is not None):
                raise ValueError("Coworker changed or no longer exists; reload before saving")
            revision=(current[0] if current else 0)+1
            db.execute("INSERT OR REPLACE INTO coworkers VALUES (?,?,?,?)",(self.owner(owner),id,revision,coworker.model_dump_json()))
        return self.get(owner,id)

    def begin(self, owner, id, request_id, session, mission):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous=db.execute("SELECT coworker,session,mission,state,result FROM coworker_runs WHERE owner=? AND request_id=?",(self.owner(owner),request_id)).fetchone()
            if previous:
                if previous[:3]!=(id,session,mission):
                    raise ValueError("Request id already belongs to another mission")
                return {"request_id":request_id,"state":previous[3],"receipt":json.loads(previous[4]) if previous[4] else None}
            row=db.execute("SELECT body FROM coworkers WHERE owner=? AND id=?",(self.owner(owner),id)).fetchone()
            if not row:
                raise LookupError("Coworker not found")
            if json.loads(row[0])["state"] != "active":
                raise ValueError("Activate the coworker before starting a mission")
            try:
                db.execute("INSERT INTO coworker_runs VALUES (?,?,?,?,?,'running',NULL)",(self.owner(owner),request_id,id,session,mission))
            except sqlite3.IntegrityError as exc:
                raise ValueError("This coworker already has a running mission; inspect its receipt before retrying") from exc
        return None

    def finish(self, owner, request_id, receipt):
        state="failed" if receipt.get("error") or receipt.get("exit_code",0) else "finished"
        with self.connect() as db:
            db.execute("UPDATE coworker_runs SET state=?,result=? WHERE owner=? AND request_id=? AND state='running'",(state,json.dumps(receipt,ensure_ascii=False,default=str),self.owner(owner),request_id))

    def history(self, owner, id):
        self.get(owner,id)
        with self.connect() as db:
            rows=db.execute("SELECT request_id,session,mission,state,result FROM coworker_runs WHERE owner=? AND coworker=? ORDER BY rowid DESC LIMIT 20",(self.owner(owner),id)).fetchall()
        return [{"request_id":r[0],"session":r[1],"mission":r[2],"state":r[3],"receipt":json.loads(r[4]) if r[4] else None} for r in rows]

    def interrupted(self, owner, request_id):
        with self.connect() as db:
            db.execute("UPDATE coworker_runs SET state='unknown',result=? WHERE owner=? AND request_id=? AND state='running'",(json.dumps({'error':'Caller interrupted. The worker may still be running; inspect the child session before closing this mission.'}),self.owner(owner),request_id))

    def resolve(self, owner, id, request_id):
        """Explicit UI acknowledgement after inspecting the worker, never automatic recovery."""
        self.get(owner,id)
        with self.connect() as db:
            changed=db.execute("UPDATE coworker_runs SET state='closed',result=? WHERE owner=? AND coworker=? AND request_id=? AND state IN ('running','unknown')",(json.dumps({'note':'Closed explicitly by the owner after inspecting the worker; no success claim.'}),self.owner(owner),id,request_id)).rowcount
        if not changed:
            raise ValueError('No unresolved mission to close')
        return {'request_id':request_id,'state':'closed'}


def templates():
    groups = [
        {"name":"Director de diseño","agent":"implementer","responsibility":"Diseñar interfaces y movimiento, revisar capturas reales y mejorar hasta satisfacer el brief. Usar Vitruvius para criterio, tokens, crítica y composiciones; Prospero para generar recursos y Lumiere para el montaje.","hoards":["vitruvius","prospero","lumiere","cicero"]},
        {"name":"Ingeniero de IA","agent":"implementer","responsibility":"Preparar datasets con procedencia, medir modelos contra una base, detectar regresiones y conservar recetas reproducibles. Usar Nightingale, Pygmalion y Galton; comprobar disponibilidad con Cassandra y el Hub antes de reservar GPU. Cargar y medir modelos grandes en las DGX Spark con Prometheus.","hoards":["nightingale","pygmalion","galton","cassandra","atlas","prometheus"]},
        {"name":"Taller 3D","agent":"implementer","responsibility":"Conservar modelos, poses, texturas y colores originales. Crear derivados editables; verificar geometría, exportaciones STL y renders con evidencia visual. Usar Heron para sólidos con medidas y exportación STEP, Gepetto para modelado artístico. Catalogar y preparar fichas sin inventar medidas ni publicar por defecto.","hoards":["heron","gepetto","platos","vulcan","daguerre","mercator","atlas"]},
        {"name":"Centro de mando","agent":"reviewer","responsibility":"Revisar salud de servicios, trabajo pendiente, espacio en disco y agenda. Informar de cambios accionables con evidencia; respetar pausas manuales y procesos ajenos. No borrar, detener servicios ni enviar mensajes sin una petición concreta.","hoards":["cassandra","diskhoard","hoardhub","funes","echo","atlas"]},
        {"name":"Investigador","agent":"reviewer","responsibility":"Buscar fuentes primarias, extraer y citar evidencia, distinguir hechos de inferencias y guardar material con procedencia. Consultar Reach, Borges, Kafka y Links; organizar estudio y análisis con Hypatia y Nightingale.","hoards":["borges","kafka","links","hypatia","nightingale"]},
        {"name":"Ingeniero de software","agent":"implementer","responsibility":"Consultar contratos y documentación vigentes con Babel; comprobar unidades y cálculos con Laplace. Conservar revisiones en Atlas, verificar servicios con Cassandra y evaluar cambios en el código real con pruebas apropiadas.","hoards":["babel","laplace","atlas","cassandra","galton"]},
        {"name":"Memoria personal","agent":"reviewer","responsibility":"Recuperar contexto desde actividad, pantalla y portapapeles sólo dentro de los permisos y exclusiones existentes. Citar la evidencia y proponer actualizaciones revisables en Dorian; nunca convertir inferencias en hechos personales guardados sin revisión.","hoards":["argus","echo","funes","dorian","people"]},
        {"name":"Analista de finanzas","agent":"reviewer","responsibility":"Relacionar gastos, documentos, ventas y estadísticas reproducibles con Ledger, Kafka, Mercator, Nightingale y Laplace. Usar Midas para hipótesis y backtests con procedencia. No ejecutar operaciones financieras ni presentar resultados históricos como rentabilidad garantizada.","hoards":["ledger","kafka","mercator","nightingale","laplace","midas"]},
        {"name":"Casa y compras","agent":"reviewer","responsibility":"Relacionar inventario, garantías, mantenimiento, compras, envíos y menú con HomeHoard, Kafka, Tantalus, Phileas y CookHoard. Comprobar presupuesto con Ledger y contexto de regalos con People. Mostrar propuestas y fuentes; comprar o enviar mensajes sólo ante una petición concreta.","hoards":["homehoard","cookhoard","kafka","tantalus","phileas","ledger","people"]},
        {"name":"Empleo y proyectos","agent":"implementer","responsibility":"Preparar candidaturas con Jobhunter, documentación con Plato y referencias con Borges. Relacionar contactos con People y viajes con Phileas. Conservar borradores y evidencia; enviar candidaturas o contactar a terceros sólo cuando esté solicitado.","hoards":["jobhunter","platos","borges","people","phileas"]},
        {"name":"Narrativa y edición","agent":"implementer","responsibility":"Conservar canon y continuidad con Scheherazade; redactar y revisar manuscritos con Writer. Convertirlos en presentaciones con Cicero o producciones de voz y vídeo con Prospero y Lumiere, manteniendo referencias y versiones.","hoards":["scheherazade","writer","cicero","prospero","lumiere"]},
        {"name":"Biblioteca de juegos","agent":"reviewer","responsibility":"Consultar biblioteca, partidas y backlog con GamerHoard por MCP; relacionar ofertas y lanzamientos con Tantalus y presupuestos con Ledger. No asumir que un juego está instalado ni iniciar compras a partir de su ficha.","hoards":["gamerhoard","tantalus","ledger"]},
    ]
    return groups + specialist_templates()


def specialist_templates():
    """Curated offline Agency adaptations, resolved through the normal agent loader."""
    path = Path(__file__).resolve().parents[1] / 'config' / 'agents' / 'agency' / 'specialists.json'
    catalog = json.loads(path.read_text(encoding='utf-8'))
    return [{"name": "Especialista de " + row['name'], "agent": row['agent'],
             "responsibility": row['mission'] + ' Entrega: ' + row['deliverable'],
             "hoards": [row['hoard'], *row['related']]}
            for row in catalog['specialists']]
