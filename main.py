import os
import json
import base64
import asyncio
import websockets
import datetime # <-- Hinzugefügt für die Datumsfunktion
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
import logging
import asyncio # Sicherstellen, dass asyncio importiert ist


print(f"Websockets version: {websockets.__version__}") # <-- Verbessertes Logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

SYSTEM_MESSAGE = (
    "Du bist James der KI-Wissensbutler und arbeitest bei der Telefonhotline von C&P Apps bzw. Couture & Pixels. Das ist ein Einzelunternehmen von Michael Knochen und erstellt Web-Apps, Webseiten, KI-Integrationen, Apps wie James KI, Imagenator, djAI und Cinematic AI. Anrufer sprechen deutsch und du sollst auch deutsch sprechen. Wenn du das aktuelle Datum benötigst, verwende das bereitgestellte Tool." # <-- Hinweis auf Tool hinzugefügt
)
VOICE = 'verse'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'response.create', 'session.created',
    # --- Hinzugefügt für Tool Calling Debugging ---
    'session.updated',
    'error',
    'response.function_call_arguments.delta',
    'response.function_call_arguments.done',
    'conversation.item.create', # Loggen wir auch unsere gesendeten Events
    # --- Ende Hinzufügungen ---
]
SHOW_TIMING_MATH = False
app = FastAPI()
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# --- Definition des Tools für OpenAI ---
# Beachte das Format: 'type: "function"' auf oberster Ebene, keine verschachtelte "function": {} Struktur
GET_CURRENT_DATE_TOOL = {
    "type": "function",
    "name": "get_current_date",
    "description": "Gibt das aktuelle Datum zurück. Verwende dies, wenn der Benutzer nach dem heutigen Datum fragt.",
    "parameters": {
        "type": "object",
        "properties": {}, # Keine Parameter für diese Funktion
        "required": []
    }
}
# --- Ende Tool-Definition ---

# --- Implementierung der Python-Funktion ---
def get_current_date():
    """Gibt das aktuelle Datum als formatierten String zurück."""
    now = datetime.datetime.now()
    # Du kannst das Format nach Bedarf anpassen
    return now.strftime("%d. %B %Y")
# --- Ende Implementierung ---

# --- Mapping von Tool-Namen zu Python-Funktionen ---
AVAILABLE_TOOLS = {
    "get_current_date": get_current_date
}
# --- Ende Mapping ---


@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    logger.info("Received incoming call request from: %s", request.client.host)
    response = VoiceResponse()
    host = request.url.hostname
    # Sicherstellen, dass die WebSocket-URL korrekt ist (wss:// für HTTPS)
    ws_scheme = "wss" if request.url.scheme == "https" else "ws"
    # Verwende den Host aus der Anfrage, um sicherzustellen, dass er von außen erreichbar ist
    connect_host = request.headers.get("host", host) # Fallback auf URL-Host
    stream_url = f'{ws_scheme}://{connect_host}/media-stream'
    logger.info(f"Connecting media stream to: {stream_url}")
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    logger.info("Successfully created the TwiML response")
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    logger.info("WebSocket client connected from: %s", websocket.client.host)
    await websocket.accept()
    logger.info("WebSocket connection accepted.")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01" # <-- Ggf. Modell anpassen falls nötig

    try:
        async with websockets.connect(
                openai_ws_url,
                additional_headers=headers
        ) as openai_ws:
            logger.info("Connected to OpenAI Realtime API.")
            await send_session_update(openai_ws) # Send session config including tools

            # Connection specific state
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None
            # --- State für Tool Calling ---
            current_function_calls = {} # Speichert Infos zu laufenden Funktionsaufrufen {call_id: {"name": "...", "arguments": "..."}}
            # --- Ende State für Tool Calling ---
            stream_sid_ready = asyncio.Event()

            async def receive_from_twilio():
                """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                nonlocal stream_sid, latest_media_timestamp
                try:
                    async for message in websocket.iter_text():
                        if not message:
                            logger.warning("Received empty message from Twilio, skipping.")
                            continue
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from Twilio: {e} - Message: {message}", exc_info=True)
                            continue

                        event = data.get('event')
                        # logger.debug(f"Received from Twilio: {event}") # Optional: sehr detailliertes Logging

                        if event == 'media' and openai_ws.state == websockets.protocol.State.OPEN:
                            # logger.debug("Forwarding media payload to OpenAI.") # Optional
                            latest_media_timestamp = int(data['media']['timestamp'])
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            pass
                        elif event == 'start':
                            stream_sid = data['start']['streamSid']
                            logger.info(f"Twilio media stream started: {stream_sid}")
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                            current_function_calls.clear() # Reset state on new stream
                        elif event == 'mark':
                            mark_name = data.get('mark', {}).get('name')
                            logger.debug(f"Received mark event: {mark_name}")
                            if mark_queue:
                                try:
                                     mark_queue.pop(0)
                                except IndexError:
                                     logger.warning("Mark queue was empty when trying to pop.")
                        elif event == 'stop':
                            logger.info("Twilio call stopped event received. Closing connections.")
                            if openai_ws.state == websockets.protocol.State.OPEN:
                                logger.info("Closing OpenAI WebSocket due to Twilio stop event.")
                                await openai_ws.close(code=1000, reason="Twilio call ended")
                            return # Stop this task
                        else:
                            logger.debug(f"Received unhandled Twilio event: {event}")

                except WebSocketDisconnect:
                    logger.info("Twilio WebSocket disconnected.")
                except websockets.exceptions.ConnectionClosedOK:
                     logger.info("Twilio WebSocket connection closed normally.")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio: {e}", exc_info=True)
                finally:
                    logger.info("receive_from_twilio task finished.")
                    if not stream_sid_ready.is_set():
                        logger.info("receive_from_twilio: Setting stream_sid_ready event in finally block (in case of early exit).")
                        stream_sid_ready.set() # Sicherstellen, dass der andere Task nicht ewig wartet
                    if openai_ws.state == websockets.protocol.State.OPEN:
                        logger.info("Closing OpenAI WebSocket from receive_from_twilio finally block.")
                        await openai_ws.close(code=1000, reason="Twilio receive task ended")


            async def send_to_twilio():
                """Receive events from the OpenAI Realtime API, send audio back to Twilio, handle tool calls."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, current_function_calls
                try:
                    # === NEU: Warte auf das stream_sid_ready Event ===
                    logger.info("send_to_twilio: Waiting for stream_sid to be set...")
                    try:
                        # Timeout hinzufügen, falls etwas schiefgeht (z.B. 10 Sekunden)
                        await asyncio.wait_for(stream_sid_ready.wait(), timeout=10.0)
                        logger.info(f"send_to_twilio: stream_sid is ready (value: {stream_sid}). Proceeding.")
                    except asyncio.TimeoutError:
                        logger.error("send_to_twilio: Timed out waiting for stream_sid! Cannot proceed with sending audio.")
                        # Hier könnten wir entscheiden, die Verbindung zu schließen oder anders zu reagieren.
                        # Fürs Erste beenden wir diesen Task.
                        return
                    # =================================================

                    async for openai_message in openai_ws:
                        try:
                            response = json.loads(openai_message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from OpenAI: {e} - Message: {openai_message}", exc_info=True)
                            continue

                        response_type = response.get('type')
                        if response_type in LOG_EVENT_TYPES or response_type.startswith("response.function_call"):
                            # Log relevant events, including tool calls
                            logger.info(f"Received from OpenAI: Type={response_type}, Data={response}")

                        # --- Verbesserte Fehlerbehandlung ---
                        if response_type == 'error':
                            logger.error(f"!!! OpenAI API Error: {response.get('error')}")
                            # Hier könntest du entscheiden, ob du den Anruf beenden willst
                            # assert False, f"Received error from OpenAI: {response.get('error')}" # Zum Debuggen: Programm anhalten
                            continue # erstmal weitermachen

                        # --- Session Update Bestätigung loggen ---
                        if response_type == 'session.updated':
                             logger.info(f"OpenAI Session Updated. Final config: {response.get('session')}")
                             # Hier prüfen, ob audio formats = g711_ulaw und tools vorhanden sind!
                             session_config = response.get('session', {})
                             if session_config.get('input_audio_format') != 'g711_ulaw' or session_config.get('output_audio_format') != 'g711_ulaw':
                                 logger.warning(f"!!! Audio format mismatch after session update! Input: {session_config.get('input_audio_format')}, Output: {session_config.get('output_audio_format')}")
                             if not session_config.get('tools'):
                                 logger.warning("!!! Tools seem to be missing after session update!")


                        # --- Audio an Twilio senden ---
                        if response_type == 'response.audio.delta' and 'delta' in response:
                            # Jetzt können wir sicher sein, dass stream_sid gesetzt ist.
                            # Wir brauchen nur noch den WebSocket-Status zu prüfen.
                            if websocket.client_state == websockets.protocol.State.OPEN:
                                audio_payload = response['delta']
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid, # stream_sid ist garantiert nicht None
                                    "media": {
                                        "payload": audio_payload
                                    }
                                }
                                await websocket.send_json(audio_delta)

                                if response_start_timestamp_twilio is None:
                                    response_start_timestamp_twilio = latest_media_timestamp
                                    if SHOW_TIMING_MATH:
                                        logger.info(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                                if response.get('item_id'):
                                    last_assistant_item = response['item_id']

                                await send_mark(websocket, stream_sid)
                            else:
                                logger.warning("Cannot send audio delta: No stream_sid or Twilio websocket closed.")


                        # --- Unterbrechung durch Benutzer ---
                        if response_type == 'input_audio_buffer.speech_started':
                            logger.info("User speech started detected.")
                            if last_assistant_item:
                                logger.info(f"Attempting to interrupt response with id: {last_assistant_item}")
                                await handle_speech_started_event() # Ruft Truncate/Clear auf

                        # --- Tool Calling Logik ---
                        if response_type == 'response.function_call_arguments.delta':
                             call_id = response.get('call_id')
                             delta = response.get('delta')
                             name = response.get('name')
                             if call_id and delta is not None:
                                 if call_id not in current_function_calls:
                                     current_function_calls[call_id] = {"name": name, "arguments": ""}
                                     logger.info(f"Starting function call {call_id} for tool '{name}'")
                                 current_function_calls[call_id]["arguments"] += delta
                                 # logger.debug(f"Accumulated args for {call_id}: {current_function_calls[call_id]['arguments']}") # Optional

                        elif response_type == 'response.function_call_arguments.done':
                            call_id = response.get('call_id')
                            if call_id in current_function_calls:
                                logger.info(f"Finished receiving arguments for function call {call_id}")
                                function_call_info = current_function_calls.pop(call_id) # Entferne aus dem State
                                function_name = function_call_info['name']
                                arguments_str = function_call_info['arguments']
                                logger.info(f"Executing tool: {function_name} with args: {arguments_str}")

                                # Führe die Funktion aus
                                if function_name in AVAILABLE_TOOLS:
                                    try:
                                        # Versuche Argumente zu parsen (für diese Funktion nicht nötig, aber generell sinnvoll)
                                        # arguments_json = json.loads(arguments_str) if arguments_str else {}
                                        tool_function = AVAILABLE_TOOLS[function_name]
                                        # Führe die Funktion aus (hier ohne Argumente)
                                        result = tool_function()
                                        output_str = str(result) # Sicherstellen, dass es ein String ist
                                        logger.info(f"Tool '{function_name}' executed successfully. Result: {output_str}")

                                        # Sende das Ergebnis an OpenAI
                                        # Format basierend auf Forum-Feedback
                                        function_output_item = {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "call_id": call_id,
                                                "output": output_str # Ergebnis als String senden
                                                # 'status': 'completed' ist laut Doku nicht hier, sondern im function_call_output content, aber andere User haben es so gemacht
                                            }
                                        }
                                        logger.info(f"Sending function_call_output for {call_id} to OpenAI.")
                                        await openai_ws.send(json.dumps(function_output_item))

                                        # !!! WICHTIG: Fordere explizit eine neue Antwort an !!!
                                        # Basierend auf Forum-Feedback (liquidshadowsmk, j0rdan, 914099943)
                                        response_create_item = {
                                            "type": "response.create"
                                            # Optional: "response": {"instructions": "Antworte dem Nutzer mit dem Ergebnis."}
                                            # Aber das einfachste scheint zu funktionieren.
                                        }
                                        logger.info(f"Sending response.create to trigger AI response after tool call {call_id}.")
                                        await openai_ws.send(json.dumps(response_create_item))

                                    except Exception as e:
                                        logger.error(f"Error executing tool '{function_name}' or sending result: {e}", exc_info=True)
                                        # Sende ggf. eine Fehlermeldung zurück an OpenAI? (Komplexer)
                                        # Vorerst loggen wir nur den Fehler.
                                else:
                                    logger.warning(f"Received request for unknown tool: {function_name}")
                                    # Was tun? Evtl. Fehlermeldung an OpenAI senden?

                            else:
                                logger.warning(f"Received function_call_arguments.done for unknown or already processed call_id: {call_id}")
                        # --- Ende Tool Calling Logik ---

                except websockets.exceptions.ConnectionClosedOK:
                    logger.info("OpenAI WebSocket connection closed normally.")
                except websockets.exceptions.ConnectionClosedError as e:
                    logger.error(f"OpenAI WebSocket connection closed with error: {e}", exc_info=True)
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {e}", exc_info=True)
                finally:
                    logger.info("send_to_twilio task finished.")
                    # Sicherstellen, dass auch die Twilio-Verbindung geschlossen wird, wenn OpenAI endet
                    if websocket.client_state == websockets.protocol.State.OPEN:
                         logger.info("Closing Twilio WebSocket from send_to_twilio finally block.")
                         await websocket.close(code=1000, reason="OpenAI connection task ended")


            async def handle_speech_started_event():
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                logger.info("Handling speech started event for interruption.")
                # Prüfen ob Twilio Websocket noch offen ist
                if websocket.client_state != websockets.protocol.State.OPEN:
                    logger.warning("Cannot handle speech started event, Twilio WebSocket is closed.")
                    return

                if mark_queue and response_start_timestamp_twilio is not None and last_assistant_item:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    if SHOW_TIMING_MATH:
                        logger.info(
                            f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")
                    logger.info(f"Truncating item {last_assistant_item} at {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0, # Annahme: nur ein Content-Item
                        "audio_end_ms": elapsed_time
                    }
                    if openai_ws.state == websockets.protocol.State.OPEN:
                        logger.info(f"Sending truncate event to OpenAI: {truncate_event}")
                        await openai_ws.send(json.dumps(truncate_event))
                    else:
                         logger.warning("Cannot send truncate event, OpenAI WebSocket is closed.")

                    logger.info(f"Sending clear event to Twilio for stream {stream_sid}")
                    await websocket.send_json({
                        "event": "clear",
                        "streamSid": stream_sid
                    })

                    mark_queue.clear() # Leere die Mark-Warteschlange, da wir abgebrochen haben
                    last_assistant_item = None
                    response_start_timestamp_twilio = None
                else:
                    logger.info("Skipping interruption handling: No active response item, mark queue, or start timestamp.")


            async def send_mark(connection, stream_sid_local):
                """Sends a mark event to Twilio to track audio buffering."""
                if stream_sid_local and connection.client_state == websockets.protocol.State.OPEN:
                    mark_name = f"response_part_{len(mark_queue)}"
                    mark_event = {
                        "event": "mark",
                        "streamSid": stream_sid_local,
                        "mark": {"name": mark_name}
                    }
                    # logger.debug(f"Sending mark event to Twilio: {mark_name}") # Optional
                    await connection.send_json(mark_event)
                    mark_queue.append(mark_name)
                else:
                    logger.warning(f"Cannot send mark: No stream_sid ({stream_sid_local}) or Twilio websocket closed.")

            # Starte die beiden Coroutinen für Senden und Empfangen
            logger.info("Starting receive_from_twilio and send_to_twilio tasks.")
            receive_task = asyncio.create_task(receive_from_twilio())
            send_task = asyncio.create_task(send_to_twilio())

            # Warte, bis eine der Aufgaben beendet ist
            done, pending = await asyncio.wait(
                [receive_task, send_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            logger.info(f"One task finished: {done}. Pending tasks: {pending}")

            # Beende die verbleibende Aufgabe
            for task in pending:
                logger.info(f"Cancelling pending task: {task.get_name()}")
                task.cancel()
                try:
                    await task # Warte auf die tatsächliche Beendigung nach dem Cancel
                except asyncio.CancelledError:
                    logger.info(f"Task {task.get_name()} was cancelled.")
                except Exception as e:
                    logger.error(f"Error during cancellation of task {task.get_name()}: {e}", exc_info=True)

            logger.info("Both receive and send tasks have finished or been cancelled.")

    except websockets.exceptions.InvalidURI as e:
         logger.error(f"Invalid WebSocket URI: {openai_ws_url} - {e}", exc_info=True)
    except websockets.exceptions.WebSocketException as e:
         logger.error(f"WebSocket connection failed: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred in handle_media_stream: {e}", exc_info=True)
    finally:
        logger.info("Closing WebSocket connection handler.")
        if websocket.client_state != websockets.protocol.State.CLOSED:
             await websocket.close(code=1000, reason="Handler finished")
             logger.info("Twilio WebSocket closed in handle_media_stream finally block.")


async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    logger.info("Sending initial conversation item to start the conversation.")
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user", # Wir tun so, als käme es vom User, damit die AI antwortet
            "content": [
                {
                    "type": "input_text",
                    # "text": "Begrüße den Anrufer mit 'Hi ich bin James von Couture & Pixels. Was kann ich für Sie tun?'"
                     "text": "Stell dich als James von Couture & Pixels vor und frage, wie du helfen kannst." # Etwas natürlicher formuliert
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    # Fordere direkt eine Antwort auf diese initiale Nachricht an
    logger.info("Sending response.create for initial greeting.")
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def send_session_update(openai_ws):
    """Send session update to OpenAI WebSocket, including tool definitions."""
    logger.info("Preparing session update with tools.")
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw", # Wichtig für Twilio
            "output_audio_format": "g711_ulaw", # Wichtig für Twilio
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "tools": [GET_CURRENT_DATE_TOOL] # <-- Tool hier hinzufügen
        }
    }
    logger.info(f'Sending session update to OpenAI: {json.dumps(session_update)}')
    await openai_ws.send(json.dumps(session_update))
    logger.info("Session update sent. Waiting for session.updated confirmation...")

    # Sende die initiale Begrüßung NACH dem Session Update
    # Warten wir kurz, um sicherzustellen, dass das Update verarbeitet wird? (optional)
    # await asyncio.sleep(0.1)
    await send_initial_conversation_item(openai_ws)


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting FastAPI server on 0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
