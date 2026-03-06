from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
import os, json, qrcode, io
from PIL import Image
from database import init_db, get_db
from spotify import buscar_canciones
from typing import Optional
import aiosqlite

load_dotenv()

app = FastAPI(title="DJ Song Request")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
DJ_PASSWORD = os.getenv("DJ_PASSWORD", "dj1234")

# ─── WebSocket Manager ────────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.dj_connections: list[WebSocket] = []
        self.user_connections: dict[int, list[WebSocket]] = {}
        self.display_connections: list[WebSocket] = []

    async def connect_dj(self, ws: WebSocket):
        await ws.accept()
        self.dj_connections.append(ws)

    def disconnect_dj(self, ws: WebSocket):
        if ws in self.dj_connections:
            self.dj_connections.remove(ws)

    async def broadcast_to_dj(self, message: dict):
        dead = []
        for ws in self.dj_connections:
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.dj_connections.remove(ws)
        # Tambien notificar a displays
        await self.broadcast_to_display(message)

    async def connect_user(self, ws: WebSocket, solicitud_id: int):
        await ws.accept()
        if solicitud_id not in self.user_connections:
            self.user_connections[solicitud_id] = []
        self.user_connections[solicitud_id].append(ws)

    def disconnect_user(self, ws: WebSocket, solicitud_id: int):
        if solicitud_id in self.user_connections:
            try: self.user_connections[solicitud_id].remove(ws)
            except: pass

    async def notify_user(self, solicitud_id: int, estado: str, cancion: str):
        mensajes = {
            "aprobada": f"✅ El DJ tiene tu canción. '{cancion}' viene pronto 🎶",
            "rechazada": f"😔 El DJ no tiene '{cancion}' disponible ahora",
            "reproducida": f"🎉 ¡Suena tu canción! '{cancion}' está en el aire",
            "next_song": f"⚡ ¡Prepárate! '{cancion}' es la siguiente canción 🔥"
        }
        conns = self.user_connections.get(solicitud_id, [])
        dead = []
        for ws in conns:
            try:
                await ws.send_json({"tipo": estado, "mensaje": mensajes.get(estado, "")})
            except:
                dead.append(ws)
        for ws in dead:
            try: conns.remove(ws)
            except: pass

    async def connect_display(self, ws: WebSocket):
        await ws.accept()
        self.display_connections.append(ws)

    def disconnect_display(self, ws: WebSocket):
        if ws in self.display_connections:
            self.display_connections.remove(ws)

    async def broadcast_to_display(self, message: dict):
        dead = []
        for ws in self.display_connections:
            try:
                await ws.send_json(message)
            except:
                dead.append(ws)
        for ws in dead:
            self.display_connections.remove(ws)

manager = ConnectionManager()

# ─── Startup ──────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await init_db()
    # Migration: agregar columnas de redes sociales si no existen
    async with aiosqlite.connect("dj_request.db") as db:
        for col in ['instagram','tiktok','facebook','spotify_dj','website','tipo']:
            try:
                default = "'cancion'" if col == 'tipo' else "''"
                tbl = 'solicitudes' if col == 'tipo' else 'configuracion'
                await db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT {default}")
                await db.commit()
            except:
                pass
    async with aiosqlite.connect("dj_request.db") as db:
        cursor = await db.execute("SELECT COUNT(*) FROM eventos")
        count = (await cursor.fetchone())[0]
        if count == 0:
            await db.execute("INSERT INTO eventos (nombre) VALUES ('Mi Evento')")
            await db.commit()

# ─── Landing Page (móvil) ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    async with aiosqlite.connect("dj_request.db") as db:
        cursor = await db.execute("SELECT id, nombre FROM eventos WHERE activo=1 LIMIT 1")
        evento = await cursor.fetchone()
    return templates.TemplateResponse("request.html", {
        "request": request,
        "evento": {"id": evento[0], "nombre": evento[1]} if evento else None
    })

# ─── Buscar canciones ─────────────────────────────────────────────────
@app.get("/api/buscar")
async def buscar(q: str):
    return await buscar_canciones(q)

# ─── Solicitar canción ────────────────────────────────────────────────
@app.post("/api/solicitar")
async def solicitar(data: dict):
    evento_id = data.get("evento_id", 1)
    async with aiosqlite.connect("dj_request.db") as db:
        cursor = await db.execute(
            "INSERT INTO solicitudes (evento_id, cancion, artista, spotify_id, portada_url, dedicatoria) VALUES (?,?,?,?,?,?)",
            (evento_id, data["cancion"], data["artista"], data.get("spotify_id",""), data.get("portada_url",""), data.get("dedicatoria",""))
        )
        await db.commit()
        solicitud_id = cursor.lastrowid
    await manager.broadcast_to_dj({
        "tipo": "nueva_solicitud",
        "id": solicitud_id,
        "cancion": data["cancion"],
        "artista": data["artista"],
        "portada_url": data.get("portada_url",""),
        "dedicatoria": data.get("dedicatoria","")
    })
    return {"id": solicitud_id, "ok": True}

@app.post("/api/mensaje-dj")
async def mensaje_dj(data: dict):
    evento_id = data.get("evento_id", 1)
    texto = data.get("texto", "").strip()
    if not texto:
        raise HTTPException(400, "Texto requerido")
    async with aiosqlite.connect("dj_request.db") as db:
        cursor = await db.execute(
            "INSERT INTO solicitudes (evento_id, cancion, artista, spotify_id, portada_url, dedicatoria, tipo) VALUES (?,?,?,?,?,?,?)",
            (evento_id, texto, "✈️ Mensaje Directo", "", "", "", "mensaje")
        )
        await db.commit()
        solicitud_id = cursor.lastrowid
    await manager.broadcast_to_dj({
        "tipo": "nueva_solicitud",
        "id": solicitud_id,
        "cancion": texto,
        "artista": "✈️ Mensaje Directo",
        "portada_url": "",
        "dedicatoria": "",
        "tipo_solicitud": "mensaje"
    })
    return {"id": solicitud_id, "ok": True}

# ─── Cola de solicitudes ──────────────────────────────────────────────
@app.get("/api/cola/{evento_id}")
async def cola(evento_id: int):
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM solicitudes WHERE evento_id=? AND estado!='rechazada' ORDER BY votos DESC, id ASC",
            (evento_id,)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

# ─── Panel DJ ─────────────────────────────────────────────────────────
@app.get("/dj", response_class=HTMLResponse)
async def dj_panel(request: Request):
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM eventos WHERE activo=1 ORDER BY id DESC LIMIT 1")
        evento = await cursor.fetchone()
    return templates.TemplateResponse("dj.html", {"request": request, "evento": evento})

# ─── DJ Solicitudes ───────────────────────────────────────────────────
@app.get("/api/dj/solicitudes")
async def dj_solicitudes(password: str, evento_id: int = 1):
    if password != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM solicitudes WHERE evento_id=? ORDER BY votos DESC, id ASC",
            (evento_id,)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]

# ─── Aprobar / Rechazar ───────────────────────────────────────────────
@app.post("/api/dj/estado/{solicitud_id}")
async def cambiar_estado(solicitud_id: int, data: dict):
    if data.get("password") != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    estado = data["estado"]
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT cancion FROM solicitudes WHERE id=?", (solicitud_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        cancion = row["cancion"]
        await db.execute("UPDATE solicitudes SET estado=? WHERE id=?", (estado, solicitud_id))
        await db.commit()
    await manager.notify_user(solicitud_id, estado, cancion)
    await manager.broadcast_to_dj({"tipo": "estado_actualizado", "id": solicitud_id, "estado": estado})
    return {"ok": True}

# ─── Next Song ────────────────────────────────────────────────────────
@app.post("/api/dj/next/{solicitud_id}")
async def next_song(solicitud_id: int, data: dict):
    if data.get("password") != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT cancion FROM solicitudes WHERE id=?", (solicitud_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Not found")
        cancion = row["cancion"]
    await manager.notify_user(solicitud_id, "next_song", cancion)
    return {"ok": True}

# ─── Votar ────────────────────────────────────────────────────────────
@app.post("/api/votar/{solicitud_id}")
async def votar(solicitud_id: int):
    async with aiosqlite.connect("dj_request.db") as db:
        await db.execute("UPDATE solicitudes SET votos=votos+1 WHERE id=?", (solicitud_id,))
        await db.commit()
        cursor = await db.execute("SELECT votos FROM solicitudes WHERE id=?", (solicitud_id,))
        row = await cursor.fetchone()
    await manager.broadcast_to_dj({"tipo": "voto", "id": solicitud_id, "votos": row[0]})
    return {"votos": row[0]}

# ─── WebSocket DJ ─────────────────────────────────────────────────────
@app.websocket("/ws/dj")
async def ws_dj(websocket: WebSocket):
    await manager.connect_dj(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        manager.disconnect_dj(websocket)

# ─── WebSocket Usuario ────────────────────────────────────────────────
@app.websocket("/ws/usuario/{solicitud_id}")
async def ws_usuario(websocket: WebSocket, solicitud_id: int):
    await manager.connect_user(websocket, solicitud_id)
    try:
        while True:
            await websocket.receive_text()
    except:
        manager.disconnect_user(websocket, solicitud_id)

# ─── Display / Proyeccion ─────────────────────────────────────────────
@app.get("/display", response_class=HTMLResponse)
async def display_page(request: Request):
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM eventos WHERE activo=1 ORDER BY id DESC LIMIT 1")
        evento = await cursor.fetchone()
    return templates.TemplateResponse("display.html", {"request": request, "evento": evento})

@app.websocket("/ws/display")
async def ws_display(websocket: WebSocket):
    await manager.connect_display(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        manager.disconnect_display(websocket)

@app.post("/api/dj/message")
async def dj_message(data: dict):
    if data.get("password") != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    await manager.broadcast_to_display({
        "tipo": "dj_message",
        "texto": data.get("texto", ""),
        "color": data.get("color", "white")
    })
    return {"ok": True}

# ─── Configuracion DJ ─────────────────────────────────────────────────
@app.get("/api/dj/config")
async def get_config(password: str, evento_id: int = 1):
    if password != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM configuracion WHERE evento_id=?", (evento_id,))
        row = await cursor.fetchone()
    if row:
        return dict(row)
    return {"evento_id": evento_id, "event_name": "Mi Evento", "subtitle": "DJ Request System", "logo_url": "", "cashapp": "", "venmo": "", "applepay": "", "love_text": "Show Your Love 💛", "instagram": "", "tiktok": "", "facebook": "", "spotify_dj": "", "website": ""}

@app.post("/api/dj/config")
async def save_config(data: dict):
    if data.get("password") != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    evento_id = data.get("evento_id", 1)
    async with aiosqlite.connect("dj_request.db") as db:
        await db.execute("""
            INSERT INTO configuracion (evento_id, event_name, subtitle, logo_url, cashapp, venmo, applepay, love_text, instagram, tiktok, facebook, spotify_dj, website)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(evento_id) DO UPDATE SET
                event_name=excluded.event_name, subtitle=excluded.subtitle,
                logo_url=excluded.logo_url, cashapp=excluded.cashapp,
                venmo=excluded.venmo, applepay=excluded.applepay, love_text=excluded.love_text,
                instagram=excluded.instagram, tiktok=excluded.tiktok,
                facebook=excluded.facebook, spotify_dj=excluded.spotify_dj, website=excluded.website
        """, (evento_id, data.get("event_name","Mi Evento"), data.get("subtitle",""),
              data.get("logo_url",""), data.get("cashapp",""), data.get("venmo",""),
              data.get("applepay",""), data.get("love_text","Show Your Love 💛"),
              data.get("instagram",""), data.get("tiktok",""),
              data.get("facebook",""), data.get("spotify_dj",""), data.get("website","")))
        await db.commit()
    await manager.broadcast_to_dj({"tipo": "config_actualizada"})
    return {"ok": True}

@app.get("/api/config/publica")
async def config_publica():
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM configuracion LIMIT 1")
        row = await cursor.fetchone()
    if row:
        return dict(row)
    return {"event_name": "DJ Request", "subtitle": "groovtek.com — DJ Request System", "logo_url": "", "love_text": "Show Your Love 💛"}

# ─── QR Code ──────────────────────────────────────────────────────────
@app.get("/api/dj/backup-db")
async def backup_db(password: str):
    if password != DJ_PASSWORD:
        raise HTTPException(403, "Forbidden")
    from fastapi.responses import PlainTextResponse
    import datetime
    async with aiosqlite.connect("dj_request.db") as db:
        db.row_factory = aiosqlite.Row
        # Export all tables as JSON
        data = {}
        for table in ["eventos","solicitudes","configuracion","votos"]:
            cursor = await db.execute(f"SELECT * FROM {table}")
            rows = await cursor.fetchall()
            data[table] = [dict(r) for r in rows]
    import json
    filename = f"groovtek_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.json"
    from fastapi.responses import Response
    return Response(
        content=json.dumps(data, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/qr")
async def qr_code():
    target = BASE_URL
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(target)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0a0a0a", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    return StreamingResponse(buf, media_type="image/png")

@app.get("/qr/page", response_class=HTMLResponse)
async def qr_page(request: Request):
    return templates.TemplateResponse("qr.html", {"request": request, "base_url": BASE_URL})
