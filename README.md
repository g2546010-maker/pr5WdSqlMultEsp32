# Practica 5 - IoT distribuido con ESP32, Gateway y App Web

## Descripcion general
Este proyecto implementa una arquitectura IoT con tres entidades separadas:

- Servidor embebido (ESP32 con MicroPython)
- Gateway (FastAPI + WebSocket + SQLite)
- App web responsiva (interfaz para monitoreo y envio de comandos)

El sistema permite:

- Escalabilidad para multiples nodos ESP32
- Persistencia historica de datos en SQLite
- Tolerancia a bloqueos en hardware usando Watchdog Timer (WDT)

## Estructura del repositorio

- servidor_embebido/
  - main.py
  - boot.py
- gateway/
  - gateway.py
  - __init__.py
- app_web/
  - index.html
- .gitignore

## Requisitos

- Python 3.10 o superior
- ESP32 con MicroPython
- Conexion de red local entre ESP32 y Gateway

## Instalacion (Gateway)
Desde la raiz del proyecto, instala dependencias:

```bash
pip install fastapi uvicorn
```

Nota: SQLite ya viene integrado con Python (modulo sqlite3), no requiere instalacion adicional.

## Ejecucion del Gateway

### Opcion recomendada (desde la raiz del proyecto)
```bash
uvicorn gateway.gateway:app --reload
```

### Opcion alternativa (si entras a la carpeta gateway)
```bash
uvicorn gateway:app --reload
```

Una vez iniciado, la app queda disponible por defecto en:

- http://127.0.0.1:8000/

## Entidad 1: Servidor embebido (ESP32)
Archivo principal: servidor_embebido/main.py

### Funcionalidad implementada
- Envio de mensajes multicast con identificador unico de dispositivo.
- Formato esperado de eventos:
  - ID=ESP32_01;EVENT:TEMP=25.5;HUM=60.2
- Recepcion de comandos por unicast desde el gateway.
- Watchdog Timer para reinicio automatico ante bloqueo mayor al umbral configurado.

### WDT (Manejo de bloqueos)
- Se configura el temporizador de hardware.
- En el bucle principal se ejecuta alimentacion periodica del WDT.
- Si el firmware se congela y no alimenta el WDT a tiempo, el ESP32 reinicia automaticamente.

## Entidad 2: Gateway
Archivo principal: gateway/gateway.py

### Funcionalidad implementada
- Descubrimiento/registro automatico de nodos ESP32 por multicast.
- Mapa dinamico de dispositivos: Device_ID -> IP.
- Envio de comandos por unicast al dispositivo objetivo.
- Difusion de eventos y respuestas a clientes web via WebSocket.
- Persistencia de mediciones en base de datos SQLite.

### Persistencia de datos (SQLite)
Base de datos: iot_data.db

Tabla utilizada:
- id (autoincrement)
- timestamp
- device_id
- temperatura
- humedad

Cada paquete multicast valido se almacena automaticamente.

### API de historial
Endpoint:
- GET /history/{device_id}

Comportamiento:
- Devuelve los ultimos 50 registros del dispositivo solicitado.

Ejemplo:
- GET /history/ESP32_01

## Entidad 3: App web responsiva
Archivo principal: app_web/index.html

### Funcionalidad implementada
- Conexion WebSocket con el gateway.
- Visualizacion de eventos en tiempo real.
- Seleccion del dispositivo destino para comandos.
- Envio de comandos con estructura JSON:

```json
{"target": "ESP32_01", "command": "LED_ON"}
```

- Consulta de historial por dispositivo mediante el endpoint del gateway.

## Flujo de operacion resumido
1. El ESP32 publica eventos por multicast con su ID.
2. El gateway registra/actualiza ID -> IP automaticamente.
3. El gateway guarda temperatura y humedad en SQLite.
4. La app web recibe eventos por WebSocket.
5. El usuario selecciona un dispositivo y envia comandos.
6. El gateway enruta el comando por unicast al ESP32 objetivo.


