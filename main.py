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
import datetime
import http.client
import urllib.parse
import requests
import time
import random
from datetime import date
from datetime import timedelta

print(websockets.__version__)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')  # requires OpenAI Realtime API Access
PORT = int(os.getenv('PORT', 5050))

SYSTEM_MESSAGE = (
    "Du bist James der KI-Wissensbutler und arbeitest bei der Telefonhotline von C&P Apps bzw. Couture & Pixels. Das ist ein Einzelunternehmen von Michael Knochen und erstellt Web-Apps, Webseiten, KI-Integrationen, Apps wie James KI, Imagenator, djAI und Cinematic AI. Anrufer sprechen deutsch und du sollst auch deutsch sprechen."
)
VOICE = 'verse'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'response.create', 'session.created'
]
SHOW_TIMING_MATH = False
app = FastAPI()
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')


def get_current_date():
    return date.today().strftime('%Y-%m-%d')


@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
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
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }

    async with websockets.connect(
            "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01",
            additional_headers=headers
    ) as openai_ws:
        await send_session_update(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    if not message:
                        print("Warnung: Leere Nachricht von Twilio erhalten!")
                        continue  # Leere Nachricht ignorieren
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError as e:
                        print(f"JSON-Fehler: {e} - Nachricht: {message}")
                        continue  # Überspringt fehlerhafte Nachrichten
                    if data['event'] == 'media' and openai_ws.state == websockets.protocol.State.OPEN:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
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
                            await log_websocket_status(openai_ws)
                        return
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def log_websocket_status(ws):
            """Utility function to log the state of the WebSocket connection."""
            if ws.open:
                logger.info("OpenAI WebSocket is still open.")
            else:
                logger.info("OpenAI WebSocket is now closed.")

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # --- NEU: TOOL CALL VERARBEITUNG ---
                    elif response.get('type') == 'tool_calls' and 'tool_calls' in response:
                        logger.info("Received tool_calls event from OpenAI.")
                        tool_results = []
                        for tool_call in response['tool_calls']:
                            if tool_call['type'] == 'function':
                                function_name = tool_call['function']['name']
                                tool_call_id = tool_call['id']  # Wichtig!

                                logger.info(f"AI requests to call function: {function_name}")

                                if function_name == "get_current_date":
                                    try:
                                        # Rufe die lokale Funktion auf
                                        current_date = getDateToday()
                                        logger.info(f"Executed get_current_date(), result: {current_date}")
                                        # Bereite das Ergebnis für OpenAI vor
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_call_id": tool_call_id,
                                            "result": current_date  # Ergebnis als String
                                        })
                                    except Exception as e:
                                        logger.error(f"Error executing tool {function_name}: {e}")
                                        # Sende Fehler zurück (optional, aber hilfreich)
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_call_id": tool_call_id,
                                            "error": f"Fehler beim Abrufen des Datums: {e}"
                                        })
                                else:
                                    logger.warning(f"Received request for unknown tool: {function_name}")
                                    # Optional: Sende einen Fehler für unbekannte Tools
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_call_id": tool_call_id,
                                        "error": f"Unbekanntes Werkzeug angefordert: {function_name}"
                                    })

                        # Sende alle gesammelten Tool-Ergebnisse an OpenAI
                        if tool_results and openai_ws.open:
                            for result_msg in tool_results:
                                logger.info(f"Sending tool_result to OpenAI: {result_msg}")
                                await openai_ws.send(json.dumps(result_msg))
                        elif not openai_ws.open:
                            logger.warning("OpenAI WebSocket closed, cannot send tool results.")
                            break  # Beende die Schleife

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(
                        f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

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
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
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
    """Send session update to OpenAI WebSocket."""

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
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_current_date",
                        "description": "Ruft das aktuelle Datum im Format YYYY-MM-DD ab.",
                        "parameters": {  # Keine Parameter benötigt
                            "type": "object",
                            "properties": {},
                            "required": []
                        }
                    }
                }
            ],
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    await send_initial_conversation_item(openai_ws)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
