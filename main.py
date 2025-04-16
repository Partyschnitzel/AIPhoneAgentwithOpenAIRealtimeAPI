import os
import json
import base64
import asyncio
import websockets
import datetime # <-- Hinzugefügt für Datum
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect # Stream entfernt, da nicht direkt genutzt
from dotenv import load_dotenv
import logging

print(f"Websockets version: {websockets.__version__}")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

# === NEU: Tool-spezifischer Hinweis im System Prompt ===
SYSTEM_MESSAGE = (
    "Du bist James der KI-Wissensbutler und arbeitest bei der Telefonhotline von C&P Apps bzw. Couture & Pixels. Das ist ein Einzelunternehmen von Michael Knochen und erstellt Web-Apps, Webseiten, KI-Integrationen, Apps wie James KI, Imagenator, djAI und Cinematic AI. Anrufer sprechen deutsch und du sollst auch deutsch sprechen. Wenn du das aktuelle Datum benötigst, verwende das bereitgestellte Tool."
)
# =====================================================
VOICE = 'verse'
# === NEU: Erweiterte Log-Typen für Tool-Debugging ===
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'response.create', 'session.created',
    # --- Relevant für Tools & Debugging ---
    'session.updated',
    'error',
    'response.function_call_arguments.delta',
    'response.function_call_arguments.done',
    'conversation.item.create', # Loggen wir auch unsere gesendeten Events
    # --- Ende Hinzufügungen ---
]
# =====================================================
SHOW_TIMING_MATH = False # Beibehalten aus Original
app = FastAPI()
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# === NEU: Tool-Definition und Implementierung ===
GET_CURRENT_DATE_TOOL = {
    "type": "function", # Korrektes flaches Format
    "name": "get_current_date",
    "description": "Gibt das aktuelle Datum zurück. Verwende dies, wenn der Benutzer nach dem heutigen Datum fragt.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

def get_current_date():
    """Gibt das aktuelle Datum als formatierten String zurück."""
    now = datetime.datetime.now()
    return now.strftime("%d. %B %Y")

AVAILABLE_TOOLS = {
    "get_current_date": get_current_date
}
# =============================================

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    logger.info("Received incoming call request from: %s", request.client.host)
    response = VoiceResponse()
    host = request.url.hostname
    # Verwende Hostname aus Request für korrekte WS-URL hinter Proxies/Load Balancern
    connect_host = request.headers.get("host", host)
    ws_scheme = "wss" if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" else "ws"
    stream_url = f'{ws_scheme}://{connect_host}/media-stream'
    logger.info(f"Connecting media stream to: {stream_url}")
    connect = Connect()
    connect.stream(url=stream_url) # Korrekte URL verwenden
    response.append(connect)
    logger.info("Successfully created the TwiML response")
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    logger.info(f"WebSocket client connected from: {websocket.client}") # Angepasst für FastAPI
    await websocket.accept()
    logger.info("WebSocket connection accepted.") # Hinzugefügt

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"

    try: # Füge try/finally um den connect hinzu für sauberes Logging
        async with websockets.connect(
                openai_ws_url,
                additional_headers=headers
        ) as openai_ws:
            logger.info("Connected to OpenAI Realtime API.") # Hinzugefügt
            await send_session_update(openai_ws) # Beinhaltet jetzt Tools

            # Connection specific state
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None
            # === NEU: State für Tool Calling ===
            current_function_calls = {}
            # ===================================

            async def receive_from_twilio():
                """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
                nonlocal stream_sid, latest_media_timestamp, last_assistant_item, response_start_timestamp_twilio # current_function_calls ist hier nicht nötig
                try:
                    # Verwende FastAPIs Iterator
                    async for message in websocket.iter_text():
                        if not message:
                            logger.warning("Warnung: Leere Nachricht von Twilio erhalten!") # Angepasst
                            continue
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON-Fehler: {e} - Nachricht: {message}", exc_info=True) # Verbessert
                            continue

                        event = data.get('event') # Sicherer Zugriff

                        if event == 'media' and openai_ws.state == websockets.protocol.State.OPEN:
                            latest_media_timestamp = int(data['media']['timestamp'])
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": data['media']['payload']
                            }
                            await openai_ws.send(json.dumps(audio_append))
                        elif event == 'start':
                            stream_sid = data['start']['streamSid']
                            logger.info(f"Incoming stream has started {stream_sid}") # Angepasst
                            # Reset state (wie im Original)
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                            current_function_calls.clear() # Reset Tool State
                        elif event == 'mark':
                            if mark_queue:
                                try: # Fange leere Queue ab
                                     mark_queue.pop(0)
                                except IndexError:
                                     logger.warning("Mark queue was empty when trying to pop.")
                        elif event == 'stop':
                            logger.info("Twilio call ended event received. Closing connections.") # Angepasst
                            if openai_ws.state == websockets.protocol.State.OPEN:
                                logger.info("Closing OpenAI WebSocket due to Twilio stop event.")
                                await openai_ws.close(code=1000, reason="Twilio call ended")
                            return # Stop this task
                        # else: # Loggen anderer Events optional
                        #    logger.debug(f"Received unhandled Twilio event: {event}")

                except WebSocketDisconnect as e: # FastAPI Exception
                    logger.info(f"Twilio client disconnected. Code: {e.code}, Reason: {e.reason}")
                except websockets.exceptions.ConnectionClosed as e: # OpenAI disconnect
                     logger.info(f"OpenAI WebSocket connection closed. Code: {e.code}, Reason: {e.reason}")
                except Exception as e: # Catch other errors
                     logger.error(f"Error in receive_from_twilio: {e}", exc_info=True)
                finally:
                     logger.info("receive_from_twilio task finished.")
                     # Sicherstellen, dass OpenAI WS geschlossen wird, wenn Twilio endet
                     if openai_ws.state == websockets.protocol.State.OPEN:
                         logger.info("Closing OpenAI WebSocket from receive_from_twilio finally block.")
                         await openai_ws.close(code=1000, reason="Twilio receive task ended")


            async def log_websocket_status(ws): # Beibehalten aus Original
                """Utility function to log the state of the WebSocket connection."""
                if ws.open:
                    logger.info("OpenAI WebSocket is still open.")
                else:
                    logger.info(f"OpenAI WebSocket is now closed. Close code: {ws.close_code}, Reason: {ws.close_reason}")


            async def send_to_twilio():
                """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, current_function_calls # Hinzugefügt: current_function_calls
                try:
                    async for openai_message in openai_ws:
                        try: # Füge JSON Parsing Fehlerbehandlung hinzu
                             response = json.loads(openai_message)
                        except json.JSONDecodeError as e:
                             logger.error(f"JSON decode error from OpenAI: {e} - Message: {openai_message}", exc_info=True)
                             continue

                        response_type = response.get('type') # Sicherer Zugriff

                        # === NEU: Erweitertes Logging & Fehlerbehandlung ===
                        if response_type in LOG_EVENT_TYPES or response_type.startswith("response.function_call"):
                            logger.info(f"Received from OpenAI: Type={response_type}, Data={response}")
                        if response_type == 'error':
                            logger.error(f"!!! OpenAI API Error: {response.get('error')}")
                            continue # Weitermachen
                        if response_type == 'session.updated':
                             logger.info(f"OpenAI Session Updated. Final config: {response.get('session')}")
                             session_config = response.get('session', {})
                             if session_config.get('input_audio_format') != 'g711_ulaw' or session_config.get('output_audio_format') != 'g711_ulaw':
                                 logger.warning(f"!!! Audio format mismatch after session update! Input: {session_config.get('input_audio_format')}, Output: {session_config.get('output_audio_format')}")
                             if not session_config.get('tools'):
                                 logger.warning("!!! Tools seem to be missing after session update!")
                        # ===============================================

                        # === Audio Senden (wie im Original, aber mit besserer Prüfung) ===
                        if response_type == 'response.audio.delta' and 'delta' in response:
                            # Prüfe beides: stream_sid gesetzt UND websocket offen
                            if stream_sid and websocket.client_state == websockets.protocol.State.OPEN:
                                audio_payload = response['delta'] # Ist bereits base64 von OpenAI
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
                                        logger.info(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms") # Geändert zu logger.info

                                if response.get('item_id'):
                                    last_assistant_item = response['item_id']

                                await send_mark(websocket, stream_sid)
                            else:
                                # Hier könnte das ursprüngliche Problem liegen, wenn stream_sid noch nicht da ist
                                logger.warning(f"Cannot send audio delta. stream_sid={stream_sid}, websocket_state={websocket.client_state}")
                        # =================================================================

                        # === Unterbrechung (wie im Original) ===
                        if response_type == 'input_audio_buffer.speech_started':
                            logger.info("Speech started detected.") # Angepasst
                            if last_assistant_item:
                                logger.info(f"Interrupting response with id: {last_assistant_item}") # Angepasst
                                await handle_speech_started_event() # Ruft Original-Funktion auf
                        # =======================================

                        # === NEU: Tool Calling Logik ===
                        if response_type == 'response.function_call_arguments.delta':
                             call_id = response.get('call_id')
                             delta = response.get('delta')
                             name = response.get('name') # Name kommt hier auch mit
                             if call_id and delta is not None:
                                 if call_id not in current_function_calls:
                                     # Speichere Namen beim ersten Delta
                                     current_function_calls[call_id] = {"name": name, "arguments": ""}
                                     logger.info(f"Starting function call {call_id} for tool '{name}'")
                                 current_function_calls[call_id]["arguments"] += delta

                        elif response_type == 'response.function_call_arguments.done':
                            call_id = response.get('call_id')
                            if call_id in current_function_calls:
                                logger.info(f"Finished receiving arguments for function call {call_id}")
                                function_call_info = current_function_calls.pop(call_id)
                                function_name = function_call_info['name']
                                arguments_str = function_call_info['arguments']
                                logger.info(f"Executing tool: {function_name} with args: {arguments_str}")

                                if function_name in AVAILABLE_TOOLS:
                                    try:
                                        tool_function = AVAILABLE_TOOLS[function_name]
                                        # Hier ggf. Argumente parsen: args = json.loads(arguments_str) if arguments_str else {}
                                        result = tool_function() # Hier ohne Argumente aufrufen
                                        output_str = str(result)
                                        logger.info(f"Tool '{function_name}' executed successfully. Result: {output_str}")

                                        # Ergebnis an OpenAI senden (Forum-Format)
                                        function_output_item = {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "call_id": call_id,
                                                "output": output_str
                                            }
                                        }
                                        logger.info(f"Sending function_call_output for {call_id} to OpenAI.")
                                        await openai_ws.send(json.dumps(function_output_item))

                                        # WICHTIG: Explizit neue Antwort anfordern (Forum-Feedback)
                                        response_create_item = {"type": "response.create"}
                                        logger.info(f"Sending response.create to trigger AI response after tool call {call_id}.")
                                        await openai_ws.send(json.dumps(response_create_item))

                                    except Exception as e:
                                        logger.error(f"Error executing tool '{function_name}' or sending result: {e}", exc_info=True)
                                else:
                                    logger.warning(f"Received request for unknown tool: {function_name}")
                            else:
                                logger.warning(f"Received function_call_arguments.done for unknown call_id: {call_id}")
                        # ============================

                except websockets.exceptions.ConnectionClosed as e: # OpenAI disconnect
                     logger.info(f"OpenAI WebSocket connection closed during receive loop. Code: {e.code}, Reason: {e.reason}")
                     await log_websocket_status(openai_ws) # Log Status aus Original
                except Exception as e: # Catch other errors
                     logger.error(f"Error in send_to_twilio: {e}", exc_info=True)
                finally:
                    logger.info("send_to_twilio task finished.")
                    # Sicherstellen, dass Twilio WS geschlossen wird, wenn OpenAI endet
                    if websocket.client_state == websockets.protocol.State.OPEN:
                         logger.info("Closing Twilio WebSocket from send_to_twilio finally block.")
                         # Verwende close() von FastAPI WebSocket
                         await websocket.close(code=1000, reason="OpenAI connection task ended")


            async def handle_speech_started_event(): # Aus Original übernommen
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                logger.info("Handling speech started event for interruption.") # Log hinzugefügt
                if websocket.client_state != websockets.protocol.State.OPEN:
                     logger.warning("Cannot handle speech started event, Twilio WebSocket is closed.")
                     return

                if mark_queue and response_start_timestamp_twilio is not None and last_assistant_item:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    if SHOW_TIMING_MATH:
                        logger.info(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms") # Log hinzugefügt
                    logger.info(f"Truncating item {last_assistant_item} at {elapsed_time}ms") # Log hinzugefügt

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    if openai_ws.state == websockets.protocol.State.OPEN:
                         logger.info(f"Sending truncate event to OpenAI: {truncate_event}") # Log hinzugefügt
                         await openai_ws.send(json.dumps(truncate_event))
                    else:
                         logger.warning("Cannot send truncate event, OpenAI WebSocket is closed.")

                    logger.info(f"Sending clear event to Twilio for stream {stream_sid}") # Log hinzugefügt
                    await websocket.send_json({
                        "event": "clear",
                        "streamSid": stream_sid
                    })

                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None
                else:
                    logger.info("Skipping interruption handling: Conditions not met.") # Log hinzugefügt

            async def send_mark(connection, stream_sid_local): # Aus Original übernommen (stream_sid in stream_sid_local umbenannt)
                 """Sends a mark event to Twilio to track audio buffering."""
                 if stream_sid_local and connection.client_state == websockets.protocol.State.OPEN:
                     mark_name = f"response_part_{len(mark_queue)}"
                     mark_event = {
                         "event": "mark",
                         "streamSid": stream_sid_local,
                         "mark": {"name": mark_name}
                     }
                     await connection.send_json(mark_event)
                     mark_queue.append(mark_name)
                 else:
                     logger.warning(f"Cannot send mark: stream_sid={stream_sid_local}, connection_state={connection.client_state}") # Log hinzugefügt

            # === Task Management (wie im Original, aber mit try/except/finally um gather) ===
            logger.info("Starting receive_from_twilio and send_to_twilio tasks.")
            receive_task = asyncio.create_task(receive_from_twilio())
            send_task = asyncio.create_task(send_to_twilio())

            done, pending = await asyncio.wait(
                [receive_task, send_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            logger.info(f"One task finished: {done}. Pending tasks: {pending}")
            for task in pending:
                logger.info(f"Cancelling pending task: {task.get_name()}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.info(f"Task {task.get_name()} was cancelled successfully.")
                except Exception as e:
                    logger.error(f"Error during cancellation of task {task.get_name()}: {e}", exc_info=True)
            logger.info("All tasks finished or cancelled.")
            # =============================================================================

    except websockets.exceptions.WebSocketException as e: # Fange OpenAI Verbindungsfehler ab
         logger.error(f"Failed to connect to OpenAI WebSocket: {e}", exc_info=True)
    except Exception as e: # Fange andere Fehler im Hauptblock ab
        logger.error(f"An unexpected error occurred in handle_media_stream: {e}", exc_info=True)
    finally:
        logger.info("Closing WebSocket connection handler for Twilio.")
        if websocket.client_state != websockets.protocol.State.CLOSED:
             logger.info("Closing Twilio WebSocket in handle_media_stream finally block.")
             # Verwende close() von FastAPI WebSocket
             await websocket.close(code=1000, reason="Handler finished")


async def send_initial_conversation_item(openai_ws): # Aus Original, aber mit response.create
    """Send initial conversation item and trigger response."""
    logger.info("Sending initial conversation item to start the conversation.") # Log hinzugefügt
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Stell dich als James von Couture & Pixels vor und frage, wie du helfen kannst." # Geändert
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    # WICHTIG: Fordere explizit eine Antwort an
    logger.info("Sending response.create for initial greeting.") # Log hinzugefügt
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def send_session_update(openai_ws): # Aus Original, aber mit Tools
    """Send session update to OpenAI WebSocket including tools."""
    logger.info("Preparing session update with tools.") # Log hinzugefügt
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
            "tools": [GET_CURRENT_DATE_TOOL] # <-- Tool hinzugefügt
        }
    }
    logger.info(f'Sending session update to OpenAI: {json.dumps(session_update)}') # Verbessertes Log
    await openai_ws.send(json.dumps(session_update))
    logger.info("Session update sent. Waiting for session.updated confirmation...") # Log hinzugefügt

    # Initiale Begrüßung NACH dem Update senden
    await send_initial_conversation_item(openai_ws)


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting FastAPI server on 0.0.0.0:{PORT}") # Log hinzugefügt
    uvicorn.run(app, host="0.0.0.0", port=PORT)
