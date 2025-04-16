import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
import logging
from datetime import date  # Import für getDateToday

# Logging konfigurieren
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
# Konfiguration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

SYSTEM_MESSAGE = (
    "Du bist James der KI-Wissensbutler und arbeitest bei der Telefonhotline von C&P Apps bzw. Couture & Pixels. "
    "Das ist ein Einzelunternehmen von Michael Knochen und erstellt Web-Apps, Webseiten, KI-Integrationen, Apps "
    "wie James KI, Imagenator, djAI und Cinematic AI. Anrufer sprechen deutsch und du sollst auch deutsch sprechen. "
    "Du kannst die Funktion getDateToday() aufrufen, um das aktuelle Datum zu erhalten."
)
VOICE = 'verse'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'response.create', 'session.created',
    'response.content.tool_calls', 'response.content.tool_calls_done'
]
SHOW_TIMING_MATH = False
app = FastAPI()
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# Funktionen definieren
def getDateToday(optional=True):
    """Gibt das aktuelle Datum im Format YYYY-MM-DD zurück."""
    return date.today().strftime('%Y-%m-%d')

# Tool-Definitionen
TOOLS = [
    {
        "name": "getDateToday",
        "description": "Gibt das aktuelle Datum im Format YYYY-MM-DD zurück",
        "parameters": {
            "type": "object",
            "properties": {
                "optional": {
                    "type": "boolean",
                    "description": "Optionaler Parameter, hat keinen Einfluss auf das Ergebnis"
                }
            },
            "required": []
        }
    }
]

# Funktionszuordnung
FUNCTION_MAP = {
    "getDateToday": getDateToday
}

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Eingehenden Anruf behandeln und TwiML-Antwort zurückgeben."""
    logger.info("Received incoming call request from: %s", request.client.host)
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    logger.info("Successfully created the TwiML response")
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """WebSocket-Verbindungen zwischen Twilio und OpenAI verarbeiten."""
    logger.info("Client connected")
    await websocket.accept()

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }

    async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
            additional_headers=headers
    ) as openai_ws:
        # Sitzung initialisieren
        await send_session_update(openai_ws)

        # Verbindungsspezifischer Zustand
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        pending_tool_calls = {}  # Tool-Calls nach ID verfolgen

        async def receive_from_twilio():
            """Audio-Daten von Twilio empfangen und an die OpenAI API senden."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    if not message:
                        logger.warning("Leere Nachricht von Twilio erhalten!")
                        continue
                    
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON-Fehler: {e} - Nachricht: {message}")
                        continue
                        
                    if data['event'] == 'media' and openai_ws.state == websockets.protocol.State.OPEN:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        logger.info(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
                    elif data['event'] == 'stop':
                        logger.info("Twilio call ended. Closing connections.")
                        if openai_ws.state == websockets.protocol.State.OPEN:
                            logger.info("Closing OpenAI WebSocket.")
                            await openai_ws.close()
                        break
                        
            except WebSocketDisconnect:
                logger.info("Client disconnected.")
                if openai_ws.state == websockets.protocol.State.OPEN:
                    await openai_ws.close()
                    
            except Exception as e:
                logger.error(f"Error in receive_from_twilio: {e}")
                if openai_ws.state == websockets.protocol.State.OPEN:
                    await openai_ws.close()

        async def handle_tool_call(tool_call):
            """Tool-Call ausführen und Ergebnis zurückgeben."""
            try:
                function_name = tool_call.get('function', {}).get('name')
                arguments_str = tool_call.get('function', {}).get('arguments', '{}')
                
                logger.info(f"Tool call requested: {function_name} with args {arguments_str}")
                
                if function_name not in FUNCTION_MAP:
                    logger.error(f"Function {function_name} not found in function map")
                    return {"error": f"Function {function_name} not implemented"}
                
                # Argumente parsen und Funktion aufrufen
                try:
                    function_args = json.loads(arguments_str)
                except json.JSONDecodeError:
                    function_args = {}
                
                function = FUNCTION_MAP[function_name]
                result = function(**function_args)
                logger.info(f"Function result: {result}")
                return result
                
            except Exception as e:
                logger.error(f"Error executing tool call: {e}")
                return {"error": str(e)}

        async def send_to_twilio():
            """Ereignisse von der OpenAI API empfangen und Audio an Twilio senden."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, pending_tool_calls
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        logger.info(f"Received event: {response['type']}")
                        
                    # Tool-Calls von der API verarbeiten
                    if response.get('type') == 'response.content.tool_calls':
                        tool_call_id = response.get('tool_call', {}).get('id')
                        if tool_call_id:
                            logger.info(f"Received tool call: {response.get('tool_call')}")
                            pending_tool_calls[tool_call_id] = response.get('tool_call')
                    
                    elif response.get('type') == 'response.content.tool_calls_done':
                        logger.info("Processing completed tool calls")
                        for tool_call_id, tool_call in pending_tool_calls.items():
                            # Funktion ausführen
                            result = await handle_tool_call(tool_call)
                            
                            # Ergebnis zurück an OpenAI senden
                            tool_call_result = {
                                "type": "conversation.item.tool_result.create",
                                "tool_call_id": tool_call_id,
                                "content": json.dumps(result)
                            }
                            logger.info(f"Sending tool call result: {tool_call_result}")
                            await openai_ws.send(json.dumps(tool_call_result))
                        
                        # Ausstehende Tool-Calls löschen
                        pending_tool_calls = {}

                    elif response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        
                        # Audio an Twilio senden
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                logger.debug(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # last_assistant_item sicher aktualisieren
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Unterbrechung auslösen
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        logger.info("Speech started detected.")
                        if last_assistant_item:
                            logger.info(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
                            
            except Exception as e:
                logger.error(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Unterbrechung behandeln, wenn der Anrufer zu sprechen beginnt."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            logger.info("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    logger.debug(f"Elapsed time for truncation: {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        logger.debug(f"Truncating item with ID: {last_assistant_item} at {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            """Mark-Event an Twilio senden."""
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        # Beide Aufgaben gleichzeitig ausführen
        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_initial_conversation_item(openai_ws):
    """Anfänglichen Gesprächseintrag senden, wenn KI zuerst spricht."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Begrüße den Anrufer mit 'Hi ich bin James von Couture & Pixels. Was kann ich für Sie tun?'"
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def send_session_update(openai_ws):
    """Sitzungsaktualisierung an den OpenAI WebSocket senden."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "tools": TOOLS  # Tool-Definitionen hinzufügen
        }
    }
    logger.info('Sending session update')
    await openai_ws.send(json.dumps(session_update))
    await send_initial_conversation_item(openai_ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
