import os
import json
import base64
import asyncio
import websockets
import datetime # <-- Hinzugefügt für Datum
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from starlette.websockets import WebSocketState # <-- NEU für korrekte Statusprüfung
from twilio.twiml.voice_response import VoiceResponse, Connect
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

SYSTEM_MESSAGE = (
    "Du bist James der KI-Wissensbutler und arbeitest bei der Telefonhotline von C&P Apps bzw. Couture & Pixels. Das ist ein Einzelunternehmen von Michael Knochen und erstellt Web-Apps, Webseiten, KI-Integrationen, Apps wie James KI, Imagenator, djAI und Cinematic AI. Anrufer sprechen deutsch und du sollst auch deutsch sprechen. Wenn du das aktuelle Datum benötigst, verwende das bereitgestellte Tool."
)
VOICE = 'verse'
LOG_EVENT_TYPES = [
    'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'response.create', 'session.created',
    # --- Relevant für Tools & Debugging ---
    'session.updated',
    'error',
    'response.function_call_arguments.delta',
    'response.function_call_arguments.done',
    'conversation.item.create',
    # --- Ende Hinzufügungen ---
]
SHOW_TIMING_MATH = False
app = FastAPI()
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# === Tool-Definition und Implementierung ===
GET_CURRENT_DATE_TOOL = {
    "type": "function",
    "name": "get_current_date",
    "description": "Gibt das aktuelle Datum zurück. Verwende dies, wenn der Benutzer nach dem heutigen Datum fragt.",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": []
    }
}

def get_current_date():
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
    logger.info("Received incoming call request from: %s", request.client.host)
    response = VoiceResponse()
    host = request.url.hostname
    connect_host = request.headers.get("host", host)
    ws_scheme = "wss" if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https" else "ws"
    stream_url = f'{ws_scheme}://{connect_host}/media-stream'
    logger.info(f"Connecting media stream to: {stream_url}")
    connect = Connect()
    connect.stream(url=stream_url)
    response.append(connect)
    logger.info("Successfully created the TwiML response")
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    logger.info(f"WebSocket client connected from: {websocket.client}")
    await websocket.accept()
    logger.info("WebSocket connection accepted.")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"

    try:
        async with websockets.connect(
                openai_ws_url,
                additional_headers=headers
        ) as openai_ws:
            logger.info("Connected to OpenAI Realtime API.")
            await send_session_update(openai_ws)

            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None
            current_function_calls = {}
            stream_sid_ready = asyncio.Event()

            async def receive_from_twilio():
                nonlocal stream_sid, latest_media_timestamp, last_assistant_item, response_start_timestamp_twilio, current_function_calls
                nonlocal stream_sid_ready

                connected_received = False
                start_received = False

                try:
                    logger.info("receive_from_twilio: Waiting for initial 'connected' and 'start' messages...")
                    for _ in range(2):
                        message_text = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
                        data = json.loads(message_text)
                        event = data.get('event')
                        logger.info(f"receive_from_twilio: Received initial message: {event}")

                        if event == 'connected':
                            logger.info("receive_from_twilio: 'connected' event confirmed.")
                            connected_received = True
                        elif event == 'start':
                            stream_sid = data['start']['streamSid']
                            logger.info(f"receive_from_twilio: 'start' event processed. stream_sid: {stream_sid}")
                            start_received = True

                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                            current_function_calls.clear()
                            logger.info("receive_from_twilio: State variables reset.")

                            logger.info("receive_from_twilio: Setting stream_sid_ready event.")
                            stream_sid_ready.set()
                            break
                        else:
                            logger.warning(f"receive_from_twilio: Received unexpected initial message: {data}")

                    if not start_received:
                         logger.error("receive_from_twilio: Did not receive 'start' message after initial messages.")
                         if not stream_sid_ready.is_set(): stream_sid_ready.set()
                         return

                except asyncio.TimeoutError:
                    logger.error("receive_from_twilio: Timed out waiting for initial messages from Twilio.")
                    if not stream_sid_ready.is_set(): stream_sid_ready.set()
                    return
                except WebSocketDisconnect as e:
                     logger.error(f"receive_from_twilio: WebSocket disconnected during initial message handling: {e}", exc_info=True)
                     if not stream_sid_ready.is_set(): stream_sid_ready.set()
                     return
                except (json.JSONDecodeError, Exception) as e:
                    logger.error(f"receive_from_twilio: Error processing initial messages: {e}", exc_info=True)
                    if not stream_sid_ready.is_set(): stream_sid_ready.set()
                    return

                try:
                    logger.info("receive_from_twilio: Entering main loop for media/mark/stop events...")
                    async for message in websocket.iter_text():
                         try:
                            data = json.loads(message)
                         except json.JSONDecodeError as e:
                             logger.error(f"JSON decode error from Twilio (in loop): {e} - Message: {message}", exc_info=True)
                             continue
                         event = data.get('event')

                         if event == 'media' and openai_ws.state == websockets.protocol.State.OPEN:
                             latest_media_timestamp = int(data['media']['timestamp'])
                             audio_append = {
                                 "type": "input_audio_buffer.append",
                                 "audio": data['media']['payload']
                             }
                             await openai_ws.send(json.dumps(audio_append))
                         elif event == 'mark':
                             mark_name = data.get('mark', {}).get('name')
                             logger.debug(f"Received mark event: {mark_name}")
                             if mark_queue:
                                 try:
                                      mark_queue.pop(0)
                                 except IndexError:
                                      logger.warning("Mark queue was empty when trying to pop.")
                         elif event == 'stop':
                             logger.info("Twilio call stopped event received in loop. Closing connections.")
                             if openai_ws.state == websockets.protocol.State.OPEN:
                                 logger.info("Closing OpenAI WebSocket due to Twilio stop event.")
                                 await openai_ws.close(code=1000, reason="Twilio call ended")
                             return
                         else:
                            logger.debug(f"Received unhandled Twilio event in loop: {event}")

                except WebSocketDisconnect as e:
                    logger.info(f"Twilio WebSocket disconnected during main loop. Code: {e.code}, Reason: {e.reason}")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio main loop: {e}", exc_info=True)
                finally:
                    logger.info("receive_from_twilio task finished (main loop section).")
                    if not stream_sid_ready.is_set():
                         logger.warning("receive_from_twilio: Setting stream_sid_ready event in main loop finally block (should not happen).")
                         stream_sid_ready.set()
                    if openai_ws.state == websockets.protocol.State.OPEN:
                        logger.info("Closing OpenAI WebSocket from receive_from_twilio finally block.")
                        await openai_ws.close(code=1000, reason="Twilio receive task ended")

            async def send_to_twilio():
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, current_function_calls
                try:
                    logger.info("send_to_twilio: Waiting for stream_sid to be set...")
                    try:
                        await asyncio.wait_for(stream_sid_ready.wait(), timeout=10.0)
                        logger.info(f"send_to_twilio: stream_sid is ready (value: {stream_sid}). Proceeding.")
                    except asyncio.TimeoutError:
                        logger.error("send_to_twilio: Timed out waiting for stream_sid! Cannot proceed.")
                        return

                    async for openai_message in openai_ws:
                        try:
                            response = json.loads(openai_message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from OpenAI: {e} - Message: {openai_message}", exc_info=True)
                            continue

                        response_type = response.get('type')

                        if response_type in LOG_EVENT_TYPES or response_type.startswith("response.function_call"):
                            logger.info(f"Received from OpenAI: Type={response_type}, Data={response}")
                        if response_type == 'error':
                            logger.error(f"!!! OpenAI API Error: {response.get('error')}")
                            continue
                        if response_type == 'session.updated':
                             logger.info(f"OpenAI Session Updated. Final config: {response.get('session')}")
                             session_config = response.get('session', {})
                             if session_config.get('input_audio_format') != 'g711_ulaw' or session_config.get('output_audio_format') != 'g711_ulaw':
                                 logger.warning(f"!!! Audio format mismatch after session update!")
                             if not session_config.get('tools'):
                                 logger.warning("!!! Tools seem to be missing after session update!")

                        if response_type == 'response.audio.delta' and 'delta' in response:
                            is_sid_set = bool(stream_sid)
                            is_ws_connected = websocket.client_state == WebSocketState.CONNECTED

                            logger.debug(f"Audio Delta Check: stream_sid set? {is_sid_set} (value='{stream_sid}'), websocket connected? {is_ws_connected} (state='{websocket.client_state}')")

                            if is_sid_set and is_ws_connected:
                                audio_payload = response['delta']
                                audio_delta = { # Definition war hier korrekt
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
                                        logger.info(f"Setting start timestamp: {response_start_timestamp_twilio}ms")
                                if response.get('item_id'):
                                    last_assistant_item = response['item_id']
                                await send_mark(websocket, stream_sid)
                            else:
                                logger.warning(f"Cannot send audio delta. [Debug Detail] is_sid_set={is_sid_set}, is_ws_connected={is_ws_connected}, stream_sid='{stream_sid}', state='{websocket.client_state}'")

                        if response_type == 'input_audio_buffer.speech_started':
                            logger.info("User speech started detected.")
                            if last_assistant_item:
                                logger.info(f"Interrupting response with id: {last_assistant_item}")
                                await handle_speech_started_event()

                        if response_type == 'response.function_call_arguments.delta':
                             call_id = response.get('call_id')
                             delta = response.get('delta')
                             if call_id and delta is not None:
                                 if call_id not in current_function_calls:
                                     current_function_calls[call_id] = {"name": None, "arguments": ""}
                                     logger.info(f"Receiving arguments for function call {call_id}...")
                                 current_function_calls[call_id]["arguments"] += delta

                        elif response_type == 'response.function_call_arguments.done':
                            call_id = response.get('call_id')
                            function_name_from_event = response.get('name')

                            if call_id in current_function_calls:
                                current_function_calls[call_id]["name"] = function_name_from_event
                                logger.info(f"Finished receiving arguments for function call {call_id}, Name: '{function_name_from_event}'")

                                function_call_info = current_function_calls.pop(call_id)
                                function_name = function_call_info['name']
                                arguments_str = function_call_info['arguments']
                                logger.info(f"Executing tool: {function_name} with args: {arguments_str}")

                                if function_name and function_name in AVAILABLE_TOOLS:
                                    try:
                                        tool_function = AVAILABLE_TOOLS[function_name]
                                        result = tool_function()
                                        output_str = str(result)
                                        logger.info(f"Tool '{function_name}' executed successfully. Result: {output_str}")

                                        function_output_item = {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "call_id": call_id,
                                                "output": output_str
                                            }
                                        }
                                        logger.info(f"Sending function_call_output for {call_id} to OpenAI.")
                                        # === FEHLERBEHEBUNG ===
                                        # JSON kann keine Sets serialisieren, was ein Problem verursachen KÖNNTE,
                                        # obwohl unwahrscheinlich bei DIESEM Tool. Wir serialisieren explizit den output_str.
                                        await openai_ws.send(json.dumps(function_output_item))
                                        # =====================

                                        response_create_item = {"type": "response.create"}
                                        logger.info(f"Sending response.create after tool call {call_id}.")
                                        await openai_ws.send(json.dumps(response_create_item))

                                    except TypeError as e: # Fange spezifischen TypeError ab
                                        logger.error(f"JSON Serialization Error for tool '{function_name}': {e}", exc_info=True)
                                        # Optional: Sende Fehlermeldung an OpenAI?
                                    except Exception as e:
                                        logger.error(f"Error executing/sending tool '{function_name}': {e}", exc_info=True)
                                else:
                                    logger.warning(f"Received request for unknown or unnamed tool: {function_name}")
                            else:
                                logger.warning(f"Received function_call_arguments.done for unknown call_id: {call_id}")

                except websockets.exceptions.ConnectionClosed as e:
                     logger.info(f"OpenAI WebSocket connection closed. Code: {e.code}, Reason: {e.reason}")
                except Exception as e:
                     logger.error(f"Error in send_to_twilio: {e}", exc_info=True)
                finally:
                    logger.info("send_to_twilio task finished.")
                    if websocket.client_state == WebSocketState.CONNECTED:
                         logger.info("Closing Twilio WebSocket from send_to_twilio finally block.")
                         await websocket.close(code=1000, reason="OpenAI connection task ended")

            async def handle_speech_started_event():
                nonlocal response_start_timestamp_twilio, last_assistant_item
                logger.info("Handling speech started event for interruption.")
                if websocket.client_state != WebSocketState.CONNECTED: # Korrigiert
                     logger.warning("Cannot handle speech started event, Twilio WebSocket is not connected.")
                     return

                if mark_queue and response_start_timestamp_twilio is not None and last_assistant_item:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    if SHOW_TIMING_MATH:
                        logger.info(f"Truncating elapsed time: {elapsed_time}ms")
                    logger.info(f"Truncating item {last_assistant_item} at {elapsed_time}ms")

                    truncate_event = { ... } # Wie gehabt
                    if openai_ws.state == websockets.protocol.State.OPEN:
                         logger.info(f"Sending truncate event to OpenAI: {truncate_event}")
                         await openai_ws.send(json.dumps(truncate_event))
                    else:
                         logger.warning("Cannot send truncate event, OpenAI WebSocket is closed.")

                    logger.info(f"Sending clear event to Twilio for stream {stream_sid}")
                    await websocket.send_json({ ... }) # Wie gehabt

                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None
                else:
                    logger.info("Skipping interruption handling: Conditions not met.")

            async def send_mark(connection, stream_sid_local):
                 if stream_sid_local and connection.client_state == WebSocketState.CONNECTED: # Korrigiert
                     mark_name = f"response_part_{len(mark_queue)}"
                     mark_event = { ... } # Wie gehabt
                     await connection.send_json(mark_event)
                     mark_queue.append(mark_name)
                 else:
                     logger.warning(f"Cannot send mark: stream_sid={stream_sid_local}, connection_state={connection.client_state}")

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

    except websockets.exceptions.WebSocketException as e:
         logger.error(f"Failed to connect to OpenAI WebSocket: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred in handle_media_stream: {e}", exc_info=True)
    finally:
        logger.info("Closing WebSocket connection handler for Twilio.")
        if websocket.client_state != WebSocketState.DISCONNECTED: # Sicherer Check
             logger.info("Closing Twilio WebSocket in handle_media_stream finally block.")
             await websocket.close(code=1000, reason="Handler finished")

async def send_initial_conversation_item(openai_ws):
    logger.info("Sending initial conversation item to start the conversation.")
    initial_conversation_item = { ... } # Wie gehabt
    await openai_ws.send(json.dumps(initial_conversation_item))
    logger.info("Sending response.create for initial greeting.")
    await openai_ws.send(json.dumps({"type": "response.create"}))

async def send_session_update(openai_ws):
    logger.info("Preparing session update with tools.")
    session_update = { ... } # Wie gehabt
    logger.info(f'Sending session update to OpenAI: {json.dumps(session_update)}')
    await openai_ws.send(json.dumps(session_update))
    logger.info("Session update sent. Waiting for session.updated confirmation...")
    await send_initial_conversation_item(openai_ws)

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting FastAPI server on 0.0.0.0:{PORT}")
    # === WICHTIG: Passe ggf. die Anzahl der Uvicorn Worker an ===
    # Wenn uvicorn mit mehreren Workern läuft (z.B. durch --workers 4),
    # können asyncio Events / shared state problematisch werden.
    # Teste explizit mit einem Worker:
    uvicorn.run(app, host="0.0.0.0", port=PORT, workers=1)
    # ==========================================================
