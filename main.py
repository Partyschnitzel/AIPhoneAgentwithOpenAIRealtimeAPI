import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream # Removed Say as it wasn't used
from dotenv import load_dotenv
import logging
# Removed unused imports: http.client, urllib.parse, requests, time, imghdr, random, openai, replicate, timedelta
from datetime import date # Keep this for getDateToday

print(f"Websockets library version: {websockets.__version__}") # Added f-string for clarity

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY') # requires OpenAI Realtime API Access
PORT = int(os.getenv('PORT', 5050))

# --- Tool Definition ---
DATE_TOOL_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "get_current_date",
            "description": "Ruft das aktuelle Datum im Format YYYY-MM-DD ab.",
            "parameters": { # Keine Parameter benötigt
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

# --- Updated System Message ---
SYSTEM_MESSAGE = (
  "Du bist James der KI-Wissensbutler und arbeitest bei der Telefonhotline von C&P Apps bzw. Couture & Pixels. "
  "Das ist ein Einzelunternehmen von Michael Knochen und erstellt Web-Apps, Webseiten, KI-Integrationen, Apps wie James KI, Imagenator, djAI und Cinematic AI. "
  "Anrufer sprechen deutsch und du sollst auch deutsch sprechen. "
  "Wenn du das aktuelle Datum benötigst, verwende das verfügbare Werkzeug 'get_current_date'." # Hinweis auf das Tool hinzugefügt
)
VOICE = 'nova' # Changed to a standard voice, 'verse' seems specific, adjust if needed
LOG_EVENT_TYPES = [
  'error', # Added error logging
  'tool_calls', # Added tool call logging
  'tool_result', # Added tool result logging
  'response.content.done', 'rate_limits.updated', 'response.done',
  'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
  'input_audio_buffer.speech_started', 'response.create', 'session.created'
]
SHOW_TIMING_MATH = False
app = FastAPI()
if not OPENAI_API_KEY:
  raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

# --- Local Function to be called by the Tool ---
def getDateToday():
    """Gibt das aktuelle Datum im Format YYYY-MM-DD zurück."""
    logger.info("Executing getDateToday function.")
    today_date = date.today().strftime('%Y-%m-%d')
    logger.info(f"getDateToday result: {today_date}")
    return today_date

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>Twilio Media Stream Server with Tool Calling is running!</h1></body></html>"

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    logger.info(f"Received incoming call request from: {request.client.host}")
    response = VoiceResponse()
    host = request.url.hostname
    # Ensure the URL scheme is correct (wss for secure websocket)
    # Use the request's scheme if available, otherwise default to wss
    scheme = request.url.scheme if request.url.scheme in ['ws', 'wss'] else 'wss'

    stream_url = f'{scheme}://{host}/media-stream'
    logger.info(f"Connecting to WebSocket URL: {stream_url}")

    connect = Connect()
    # Add statusCallback to potentially catch connection issues
    connect.stream(url=stream_url) # statusCallback=f"https://{host}/twilio-status" removed unless you have the endpoint
    response.append(connect)

    logger.info("Successfully created the TwiML response")
    # Return as XML, not HTMLResponse with XML content
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI, including Tool Calls."""
    session_id = None # To identify logs related to this specific call session
    try:
        await websocket.accept()
        session_id = websocket.scope.get('client')[0] + ":" + str(websocket.scope.get('client')[1]) # Basic session ID from client IP/Port
        logger.info(f"[{session_id}] WebSocket client connected.")

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }

        # Updated model name if needed, ensure it supports tool use
        # Check OpenAI documentation for the latest Realtime models supporting tool use
        openai_model = "gpt-4o-mini-realtime-preview-2024-12-17" # Example, check latest model
        openai_ws_url = f"wss://api.openai.com/v1/realtime?model={openai_model}"
        logger.info(f"[{session_id}] Connecting to OpenAI WebSocket: {openai_ws_url}")

        async with websockets.connect(
            openai_ws_url,
            additional_headers=headers
        ) as openai_ws:
            logger.info(f"[{session_id}] Successfully connected to OpenAI WebSocket.")
            await send_session_update(openai_ws, session_id)

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
                            logger.warning(f"[{session_id}] Empty message received from Twilio!")
                            continue

                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"[{session_id}] JSON Decode Error from Twilio: {e} - Message: {message}")
                            continue

                        event_type = data.get('event')
                        # Log received Twilio events
                        # logger.debug(f"[{session_id}] Received from Twilio: {event_type}")

                        if event_type == 'media' and openai_ws.open:
                            # Directly access nested keys, add checks if structure might vary
                            media_data = data.get('media', {})
                            timestamp = media_data.get('timestamp')
                            payload = media_data.get('payload')

                            if timestamp is None or payload is None:
                                logger.warning(f"[{session_id}] Received media event with missing timestamp or payload: {data}")
                                continue

                            latest_media_timestamp = int(timestamp)

                            # No need to decode/re-encode if sending directly
                            audio_append = {
                                "type": "input_audio_buffer.append",
                                "audio": payload # Send the original base64 payload
                            }
                            # logger.debug(f"[{session_id}] Sending audio chunk to OpenAI.")
                            await openai_ws.send(json.dumps(audio_append))

                        elif event_type == 'start':
                            start_data = data.get('start', {})
                            stream_sid = start_data.get('streamSid')
                            call_sid = start_data.get('callSid') # Get callSid too if needed for logging/tracking
                            logger.info(f"[{session_id}] Twilio stream started. StreamSid: {stream_sid}, CallSid: {call_sid}")
                            # Reset state for the new stream
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                            mark_queue.clear()

                        elif event_type == 'mark':
                            mark_name = data.get('mark', {}).get('name')
                            # logger.debug(f"[{session_id}] Received mark event from Twilio: {mark_name}")
                            if mark_queue:
                                mark_queue.pop(0) # Process the mark queue

                        elif event_type == 'stop':
                            stop_data = data.get('stop', {})
                            call_sid = stop_data.get('callSid')
                            logger.info(f"[{session_id}] Twilio call stopped. CallSid: {call_sid}. Closing connections.")
                            # No need to explicitly close OpenAI WS here, the 'finally' block handles it
                            return # Exit the receive loop

                        elif event_type == 'error': # Handle Twilio errors
                             error_data = data.get('error')
                             logger.error(f"[{session_id}] Received error event from Twilio: {error_data}")


                except WebSocketDisconnect:
                    logger.info(f"[{session_id}] Twilio WebSocket client disconnected.")
                    # Let the main handler close OpenAI WS

                except websockets.exceptions.ConnectionClosedOK:
                     logger.info(f"[{session_id}] Twilio WebSocket connection closed normally.")

                except websockets.exceptions.ConnectionClosedError as e:
                     logger.error(f"[{session_id}] Twilio WebSocket connection closed with error: {e}")

                except Exception as e:
                    logger.error(f"[{session_id}] Error in receive_from_twilio: {e}", exc_info=True)
                    # Attempt to close OpenAI WS cleanly on error
                    if openai_ws.open:
                        await openai_ws.close(code=1011, reason="Error in Twilio receiver")


            async def log_websocket_status(ws, name="WebSocket"):
                """Utility function to log the state of the WebSocket connection."""
                state_map = {
                    websockets.protocol.State.CONNECTING: "CONNECTING",
                    websockets.protocol.State.OPEN: "OPEN",
                    websockets.protocol.State.CLOSING: "CLOSING",
                    websockets.protocol.State.CLOSED: "CLOSED",
                }
                state = state_map.get(ws.state, "UNKNOWN")
                logger.info(f"[{session_id}] {name} state: {state} (Open: {ws.open}, Closed: {ws.closed})")


            async def send_to_twilio():
                """Receive events from OpenAI, handle tool calls, send audio to Twilio."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
                try:
                    async for openai_message in openai_ws:
                        try:
                            response = json.loads(openai_message)
                        except json.JSONDecodeError:
                            logger.error(f"[{session_id}] Failed to decode JSON from OpenAI: {openai_message}")
                            continue # Ignore invalid messages

                        response_type = response.get('type')

                        if response_type in LOG_EVENT_TYPES:
                            # Use logger.debug for potentially verbose logs
                            log_level = logging.INFO if response_type not in ['response.audio.delta'] else logging.DEBUG
                            # Avoid logging potentially large audio deltas unless debug is enabled
                            log_payload = response if response_type != 'response.audio.delta' else {k: v for k, v in response.items() if k != 'delta'}
                            logger.log(log_level, f"[{session_id}] Received from OpenAI: Type={response_type}, Payload={json.dumps(log_payload)}")

                        # --- AUDIO DELTA HANDLING ---
                        if response_type == 'response.audio.delta' and 'delta' in response:
                            audio_payload = response['delta'] # Directly use the base64 payload from OpenAI
                            audio_delta_msg = {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {
                                    "payload": audio_payload
                                }
                            }
                            # Only send if Twilio WebSocket is still open
                            if websocket.client_state == websockets.protocol.State.OPEN:
                                await websocket.send_json(audio_delta_msg)
                            else:
                                logger.warning(f"[{session_id}] Twilio WebSocket closed, cannot send audio delta.")
                                break # Stop sending if Twilio is gone

                            # Timing logic (unchanged)
                            if response_start_timestamp_twilio is None:
                                response_start_timestamp_twilio = latest_media_timestamp
                                if SHOW_TIMING_MATH:
                                    logger.info(f"[{session_id}] Setting start timestamp for new response: {response_start_timestamp_twilio}ms")
                            if response.get('item_id'):
                                last_assistant_item = response['item_id']

                            # Send mark after sending audio
                            await send_mark(websocket, stream_sid)

                        # --- SPEECH STARTED (INTERRUPTION) HANDLING ---
                        elif response_type == 'input_audio_buffer.speech_started':
                            logger.info(f"[{session_id}] Speech started detected.")
                            if last_assistant_item:
                                logger.info(f"[{session_id}] Interrupting response with id: {last_assistant_item}")
                                # Pass openai_ws to the handler function
                                await handle_speech_started_event(openai_ws)

                        # --- TOOL CALL HANDLING (NEW) ---
                        elif response_type == 'tool_calls' and 'tool_calls' in response:
                            logger.info(f"[{session_id}] Received tool_calls event from OpenAI.")
                            tool_results_to_send = [] # Collect results

                            for tool_call in response.get('tool_calls', []):
                                if tool_call.get('type') == 'function':
                                    function_name = tool_call.get('function', {}).get('name')
                                    tool_call_id = tool_call.get('id')

                                    if not function_name or not tool_call_id:
                                        logger.warning(f"[{session_id}] Invalid tool call structure received: {tool_call}")
                                        continue

                                    logger.info(f"[{session_id}] AI requests to call function: {function_name} with ID: {tool_call_id}")

                                    if function_name == "get_current_date":
                                        try:
                                            # Call the local function
                                            current_date = getDateToday()
                                            tool_results_to_send.append({
                                                "type": "tool_result",
                                                "tool_call_id": tool_call_id,
                                                "result": current_date # Send result as string
                                            })
                                        except Exception as e:
                                            logger.error(f"[{session_id}] Error executing tool {function_name}: {e}", exc_info=True)
                                            tool_results_to_send.append({
                                                "type": "tool_result",
                                                "tool_call_id": tool_call_id,
                                                "error": f"Fehler bei der Ausführung von get_current_date: {e}"
                                            })
                                    else:
                                        logger.warning(f"[{session_id}] Received request for unknown tool: {function_name}")
                                        tool_results_to_send.append({
                                            "type": "tool_result",
                                            "tool_call_id": tool_call_id,
                                            "error": f"Unbekanntes Werkzeug angefordert: {function_name}"
                                        })

                            # Send all collected results back to OpenAI
                            if tool_results_to_send:
                                if openai_ws.open:
                                    for result_msg in tool_results_to_send:
                                        logger.info(f"[{session_id}] Sending tool_result to OpenAI: {result_msg}")
                                        await openai_ws.send(json.dumps(result_msg))
                                else:
                                    logger.warning(f"[{session_id}] OpenAI WebSocket closed, cannot send tool results.")
                                    break # Stop processing if OpenAI WS is closed

                        # --- ERROR HANDLING ---
                        elif response_type == 'error':
                             error_details = response.get('error')
                             logger.error(f"[{session_id}] Received error from OpenAI: {error_details}")
                             # Depending on the error, you might want to close the connection or take other actions
                             # Example: Close connection on fatal errors
                             if error_details and "fatal" in error_details.get("message", "").lower():
                                 logger.error(f"[{session_id}] Fatal error received from OpenAI. Closing connection.")
                                 await websocket.close(code=1011, reason="Fatal OpenAI Error")
                                 await openai_ws.close(code=1011, reason="Fatal OpenAI Error")
                                 return # Exit loop


                except websockets.exceptions.ConnectionClosedOK:
                    logger.info(f"[{session_id}] OpenAI WebSocket connection closed normally.")
                except websockets.exceptions.ConnectionClosedError as e:
                    logger.error(f"[{session_id}] OpenAI WebSocket connection closed with error: {e}")
                except Exception as e:
                    logger.error(f"[{session_id}] Error in send_to_twilio: {e}", exc_info=True)
                finally:
                    logger.info(f"[{session_id}] Exiting send_to_twilio loop.")
                    # Ensure Twilio WS is closed if this loop exits unexpectedly
                    if websocket.client_state == websockets.protocol.State.OPEN:
                        logger.warning(f"[{session_id}] Closing Twilio WebSocket from send_to_twilio finally block.")
                        await websocket.close(code=1011, reason="Send loop ended")


            async def handle_speech_started_event(current_openai_ws): # Pass openai_ws explicitly
                """Handle interruption when the caller's speech starts."""
                nonlocal response_start_timestamp_twilio, last_assistant_item, stream_sid, mark_queue, latest_media_timestamp
                logger.info(f"[{session_id}] Handling speech started event.")
                # Ensure there's an active item and timestamp to calculate truncation
                if mark_queue and response_start_timestamp_twilio is not None and last_assistant_item:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    if elapsed_time < 0: # Sanity check
                         logger.warning(f"[{session_id}] Calculated negative elapsed time for truncation ({elapsed_time}ms). Resetting to 0.")
                         elapsed_time = 0

                    if SHOW_TIMING_MATH:
                        logger.info(f"[{session_id}] Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")
                        logger.info(f"[{session_id}] Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0, # Usually 0 for audio/text
                        "audio_end_ms": elapsed_time
                    }
                    # Check if OpenAI WS is open before sending
                    if current_openai_ws.open:
                        await current_openai_ws.send(json.dumps(truncate_event))
                        logger.info(f"[{session_id}] Sent truncate event to OpenAI for item {last_assistant_item}.")
                    else:
                        logger.warning(f"[{session_id}] OpenAI WebSocket closed, cannot send truncate event.")

                    # Send clear event to Twilio
                    if websocket.client_state == websockets.protocol.State.OPEN:
                        await websocket.send_json({
                            "event": "clear",
                            "streamSid": stream_sid
                        })
                        logger.info(f"[{session_id}] Sent clear event to Twilio.")
                    else:
                        logger.warning(f"[{session_id}] Twilio WebSocket closed, cannot send clear event.")

                    # Reset state after interruption
                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None
                else:
                    logger.info(f"[{session_id}] Skipping interruption handling: No active response item, mark queue, or timestamp.")

            async def send_mark(connection, current_stream_sid):
                """Send a mark message to Twilio to synchronize audio playback."""
                if current_stream_sid and connection.client_state == websockets.protocol.State.OPEN:
                    mark_event = {
                        "event": "mark",
                        "streamSid": current_stream_sid,
                        "mark": {"name": f"response_part_{len(mark_queue)}"} # Unique mark name
                    }
                    try:
                        await connection.send_json(mark_event)
                        mark_queue.append(mark_event["mark"]["name"])
                        # logger.debug(f"[{session_id}] Sent mark event to Twilio: {mark_event['mark']['name']}")
                    except Exception as e:
                         logger.error(f"[{session_id}] Failed to send mark event to Twilio: {e}")
                elif not current_stream_sid:
                     logger.warning(f"[{session_id}] Cannot send mark, stream_sid is not set.")
                # No warning if connection is closed, as sending would fail anyway


            # Start the receive/send loops
            logger.info(f"[{session_id}] Starting receive/send loops.")
            await asyncio.gather(
                receive_from_twilio(),
                send_to_twilio()
            )

            logger.info(f"[{session_id}] Receive/send loops finished.")

    except websockets.exceptions.ConnectionClosedOK:
        logger.info(f"[{session_id or 'Unknown Session'}] WebSocket connection closed normally (Outer).")
    except websockets.exceptions.ConnectionClosedError as e:
        logger.error(f"[{session_id or 'Unknown Session'}] WebSocket connection closed with error (Outer): {e}")
    except WebSocketDisconnect:
         logger.info(f"[{session_id or 'Unknown Session'}] WebSocket client disconnected (Outer).")
    except Exception as e:
        logger.error(f"[{session_id or 'Unknown Session'}] Error in handle_media_stream: {e}", exc_info=True)
    finally:
        logger.info(f"[{session_id or 'Unknown Session'}] Cleaning up WebSocket connection.")
        # Ensure both websockets are closed if they are still open
        # No need to check openai_ws here as the 'async with' handles it
        if websocket.client_state != websockets.protocol.State.CLOSED:
             await websocket.close(code=1000, reason="Session ending")
             logger.info(f"[{session_id or 'Unknown Session'}] Closed Twilio WebSocket.")
        # Log final status if possible
        # await log_websocket_status(websocket, "Twilio WebSocket (Final)")
        # await log_websocket_status(openai_ws, "OpenAI WebSocket (Final)") # openai_ws might be undefined if connect fails

async def send_initial_conversation_item(openai_ws, session_id):
    """Send initial greeting message to start the conversation."""
    try:
        initial_greeting = "Hi, ich bin James von Couture & Pixels. Was kann ich für Sie tun?"
        logger.info(f"[{session_id}] Sending initial greeting: '{initial_greeting}'")
        initial_conversation_item = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                # Role 'user' tells the assistant WHAT to say initially in this context
                # If you want the assistant to generate its own greeting based on instructions,
                # you might skip this or adjust the logic. For a fixed greeting, this is okay.
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": f"Begrüße den Anrufer mit '{initial_greeting}'"
                    }
                ]
            }
        }
        await openai_ws.send(json.dumps(initial_conversation_item))
        # Tell OpenAI to generate the response (audio) for the item just created
        await openai_ws.send(json.dumps({"type": "response.create"}))
        logger.info(f"[{session_id}] Initial greeting sent and response requested.")
    except Exception as e:
         logger.error(f"[{session_id}] Failed to send initial conversation item: {e}", exc_info=True)


async def send_session_update(openai_ws, session_id):
    """Send session update to OpenAI WebSocket, including system message and tool definitions."""
    try:
        session_update = {
          "type": "session.update",
          "session": {
              "turn_detection": {"type": "server_vad"}, # Use Voice Activity Detection
              "input_audio_format": "g711_ulaw", # Matching Twilio's default
              "output_audio_format": "g711_ulaw", # Matching Twilio's requirement
              "voice": VOICE,
              "instructions": SYSTEM_MESSAGE,
              "modalities": ["text", "audio"],
              "temperature": 0.7, # Adjusted temperature
              "tools": DATE_TOOL_DEFINITION # Include the tool definition here
          }
        }
        logger.info(f'[{session_id}] Sending session update to OpenAI: {json.dumps(session_update)}')
        await openai_ws.send(json.dumps(session_update))
        logger.info(f"[{session_id}] Session update sent.")

        # Send the initial greeting *after* the session is configured
        await send_initial_conversation_item(openai_ws, session_id)

    except Exception as e:
        logger.error(f"[{session_id}] Failed to send session update: {e}", exc_info=True)


if __name__ == "__main__":
  import uvicorn
  logger.info(f"Starting Uvicorn server on host 0.0.0.0 port {PORT}")
  uvicorn.run(app, host="0.0.0.0", port=PORT)
