from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
import socket, threading, struct, asyncio, json, sqlite3
from datetime import datetime

app = FastAPI()

connections = []
connection_locks = {}
main_loop = None

# MODIFICACION PERSISTENCIA (SQLITE):
# Nombre de la base de datos SQLite que guardara el historial de sensores.
DB_NAME = "iot_data.db"

# MODIFICACION PERSISTENCIA (SQLITE):
# Lock para evitar acceso concurrente a la BD desde el thread multicast y las rutas FastAPI.
db_lock = threading.Lock()

# MODIFICACION ESCALABILIDAD (GRANJA DE SENSORES):
# Se reemplaza la IP unica por un diccionario dinamico ID -> IP.
esp32_devices = {}
ESP32_PORT = 5005
MULTICAST_GROUP = "239.1.1.1"
MULTICAST_PORT = 5006

def inicializar_base_datos():
    # MODIFICACION PERSISTENCIA (SQLITE):
    # Crea la tabla iot_datos si no existe. Se ejecuta al arrancar el gateway.
    # Campos: timestamp (momento del registro), device_id (ID del ESP32), temperatura, humedad.
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS iot_datos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                device_id TEXT NOT NULL,
                temperatura REAL,
                humedad REAL
            )
        """)
        conn.commit()
        conn.close()
        print(f"[BD] Base de datos {DB_NAME} inicializada correctamente.")
    except sqlite3.Error as e:
        print(f"[BD] Error al inicializar BD: {e}")

def guardar_datos_sensor(device_id, temperatura, humedad):
    # MODIFICACION PERSISTENCIA (SQLITE):
    # Inserta un registro de datos en la tabla iot_datos.
    # Se llama cada vez que llega un paquete multicast valido con datos de sensor.
    try:
        with db_lock:
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO iot_datos (device_id, temperatura, humedad)
                VALUES (?, ?, ?)
            """, (device_id, temperatura, humedad))
            conn.commit()
            conn.close()
            print(f"[BD] Registro guardado: {device_id} - Temp: {temperatura}C, Hum: {humedad}%")
    except sqlite3.Error as e:
        print(f"[BD] Error al guardar datos: {e}")

def generar_id_automatico_desde_ip(ip):
    # MODIFICACION ESCALABILIDAD (GRANJA DE SENSORES):
    # ID de compatibilidad para nodos legacy que no incluyen "ID=" en multicast.
    return "AUTO_" + ip.replace(".", "_")

def enviar_unicast(target_id, cmd):
    # MODIFICACION ESCALABILIDAD (GRANJA DE SENSORES):
    # Enrutamiento de comando al ESP32 seleccionado por su ID.
    if target_id not in esp32_devices:
        return f"ERROR:TARGET_NO_REGISTRADO:{target_id}"

    target_ip = esp32_devices[target_id]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    sock.sendto(cmd.encode(), (target_ip, ESP32_PORT))
    data, _ = sock.recvfrom(1024)
    sock.close()
    return data.decode()

async def broadcast(msg):
    # MODIFICACION RENDIMIENTO (LATENCIA WEB):
    # Serializar una sola vez y enviar en paralelo para evitar bloqueo por clientes lentos.
    if not connections:
        return

    serialized = json.dumps(msg)
    current_connections = list(connections)
    results = await asyncio.gather(
        *(send_to_connection(c, serialized) for c in current_connections),
        return_exceptions=True,
    )

    # MODIFICACION RENDIMIENTO (LATENCIA WEB):
    # Limpiar conexiones caidas para que no degraden el broadcast en el tiempo.
    for c, result in zip(current_connections, results):
        if isinstance(result, Exception) and c in connections:
            connections.remove(c)
            connection_locks.pop(c, None)


async def send_to_connection(connection, serialized_message):
    # MODIFICACION RENDIMIENTO/ESTABILIDAD (WEBSOCKET):
    # Evita envios concurrentes sobre el mismo socket cuando coinciden respuesta+multicast.
    lock = connection_locks.get(connection)
    if lock is None:
        lock = asyncio.Lock()
        connection_locks[connection] = lock

    async with lock:
        await connection.send_text(serialized_message)

def multicast():
    print("Thread multicast iniciado, esperando event loop...")
    # Esperar a que main_loop sea inicializado
    while main_loop is None:
        threading.Event().wait(0.1)
    
    print("Event loop listo, configurando socket multicast...")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", MULTICAST_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    print(f"Escuchando multicast en {MULTICAST_GROUP}:{MULTICAST_PORT}")

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            msg = data.decode()
            source_ip = addr[0]
            print(f"Datos multicast recibidos: {msg} desde {addr}")
            # MODIFICACION ESCALABILIDAD (GRANJA DE SENSORES):
            # Auto-registro de nuevos dispositivos con ID explicito o ID automatico por IP.
            if msg.startswith("ID="):
                partes = msg.split(";", 1)
                device_id = partes[0].replace("ID=", "", 1).strip()
                if not device_id:
                    device_id = generar_id_automatico_desde_ip(source_ip)
            else:
                device_id = generar_id_automatico_desde_ip(source_ip)

            previous_ip = esp32_devices.get(device_id)
            esp32_devices[device_id] = source_ip
            if previous_ip != source_ip:
                print(f"Dispositivo registrado/actualizado: {device_id} -> {source_ip}")

            # MODIFICACION PERSISTENCIA (SQLITE):
            # Parsear el mensaje multicast para extraer temperatura y humedad y guardarlos.
            # Formato esperado: ID=ESP32_01;EVENT:TEMP=25.5;HUM=60.2
            temperatura = None
            humedad = None
            try:
                # Busca TEMP= en el mensaje
                if "TEMP=" in msg:
                    temp_start = msg.find("TEMP=") + 5
                    temp_end = msg.find(";", temp_start)
                    if temp_end == -1:
                        temp_end = len(msg)
                    temperatura = float(msg[temp_start:temp_end])
                
                # Busca HUM= en el mensaje
                if "HUM=" in msg:
                    hum_start = msg.find("HUM=") + 4
                    hum_end = msg.find(";", hum_start)
                    if hum_end == -1:
                        hum_end = len(msg)
                    humedad = float(msg[hum_start:hum_end])
                
                # Si encontro datos validos, guardarlos en la BD
                if temperatura is not None and humedad is not None:
                    guardar_datos_sensor(device_id, temperatura, humedad)
                else:
                    print(f"[BD] No se encontraron datos de sensor en el mensaje")
            except ValueError as ve:
                print(f"[BD] Error al parsear datos del sensor: {ve}")

            event_payload = {
                "type": "event",
                "id": device_id,
                "msg": msg,
                "devices": dict(esp32_devices)
            }

            if main_loop:
                asyncio.run_coroutine_threadsafe(broadcast(event_payload), main_loop)
        except Exception as e:
            print(f"Error en multicast: {e}")

threading.Thread(target=multicast, daemon=True).start()


@app.get("/")
async def root():
    return FileResponse("index.html")


@app.get("/history/{device_id}")
async def obtener_historial(device_id: str):
    # MODIFICACION PERSISTENCIA (SQLITE):
    # Endpoint para consultar los ultimos 50 registros de un dispositivo en particular.
    # Se accede como: GET /history/ESP32_01
    # Devuelve una lista JSON con timestamp, temperatura y humedad.
    try:
        with db_lock:
            conn = sqlite3.connect(DB_NAME)
            # MODIFICACION PERSISTENCIA (SQLITE):
            # row_factory permite obtener filas como diccionarios en lugar de tuplas.
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # MODIFICACION PERSISTENCIA (SQLITE):
            # Consulta los ultimos 50 registros ordenados por timestamp descendente (mas reciente primero).
            # MODIFICACION VISUALIZACION (WEB): Se agrega el campo 'id' autoincrement para referencia exacta en BD desde frontend.
            cursor.execute("""
                SELECT id, timestamp, device_id, temperatura, humedad
                FROM iot_datos
                WHERE device_id = ?
                ORDER BY timestamp DESC
                LIMIT 50
            """, (device_id,))
            
            registros = cursor.fetchall()
            conn.close()
            
            # MODIFICACION PERSISTENCIA (SQLITE):
            # Convertir filas a diccionarios para devolver JSON limpio.
            # MODIFICACION VISUALIZACION (WEB): Se incluye el campo 'id' en la respuesta JSON.
            historial = [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "device_id": row["device_id"],
                    "temperatura": row["temperatura"],
                    "humedad": row["humedad"]
                }
                for row in registros
            ]
            
            return {"device_id": device_id, "registros": historial, "total": len(historial)}
    except sqlite3.Error as e:
        return {"error": f"Error al consultar historial: {e}"}


@app.on_event("startup")
async def startup():
    global main_loop
    main_loop = asyncio.get_event_loop()
    print("Event loop capturado para multicast")
    
    # MODIFICACION PERSISTENCIA (SQLITE):
    # Al arrancar el gateway, se inicializa la base de datos.
    inicializar_base_datos()




@app.websocket("/ws")
async def ws(ws: WebSocket):
    await ws.accept()
    connections.append(ws)
    connection_locks[ws] = asyncio.Lock()
    print(f"Cliente conectado. Total de clientes: {len(connections)}")

    try:
        # MODIFICACION ESCALABILIDAD (GRANJA DE SENSORES):
        # Se comparte al cliente la lista inicial de dispositivos conocidos.
        await ws.send_text(json.dumps({"type": "devices", "devices": dict(esp32_devices)}))

        while True:
            data = await ws.receive_text()
            payload = json.loads(data)

            # MODIFICACION ESCALABILIDAD (GRANJA DE SENSORES):
            # Nueva estructura de comando desde web: {"target":"ID","command":"CMD"}.
            target_id = payload.get("target")
            cmd = payload.get("command", "")

            if not target_id:
                await send_to_connection(ws, json.dumps({"type": "response", "resp": "ERROR:FALTA_TARGET"}))
                continue

            resp = enviar_unicast(target_id, cmd)
            await broadcast({"type": "response", "target": target_id, "resp": resp, "devices": dict(esp32_devices)})
    except Exception as e:
        print(f"Error WebSocket: {e}")
    finally:
        if ws in connections:
            connections.remove(ws)
        connection_locks.pop(ws, None)
        print(f"Cliente desconectado. Total de clientes: {len(connections)}")