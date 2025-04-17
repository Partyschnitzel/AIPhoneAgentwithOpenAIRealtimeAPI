import os
import json
import base64
import asyncio
import websockets
import datetime  # <-- Hinzugefügt für Datum
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketDisconnect
from starlette.websockets import WebSocketState  # <-- NEU für korrekte Statusprüfung
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests         # <-- Hinzugefügt für Wetter
import http.client    # <-- Hinzugefügt für Wetter (PositionStack)
import urllib.parse   # <-- Hinzugefügt für Wetter (PositionStack)
import random         # <-- Hinzugefügt für Wetter (API Key Auswahl)


class ConversationTracker:
    """Verfolgt den Gesprächsverlauf für die Zusammenfassung."""

    def __init__(self):
        self.messages = []
        self.call_start_time = None
        self.call_end_time = None
        self.caller_number = None
        self.tools_used = []

    def start_call(self, caller_number=None):
        """Startet einen neuen Anruf."""
        self.call_start_time = datetime.datetime.now()
        self.caller_number = caller_number or "Unbekannt"
        self.messages = []
        self.tools_used = []
        logger.info(f"Started tracking conversation with caller: {self.caller_number}")

    def add_user_message(self, text):
        """Fügt eine Benutzer-Nachricht hinzu."""
        self.messages.append({"role": "user", "content": text, "timestamp": datetime.datetime.now()})
        logger.info(f"ConversationTracker: Added user message: '{text}' (total messages: {len(self.messages)})")

    def add_assistant_message(self, text):
        """Fügt eine Assistenten-Nachricht hinzu."""
        self.messages.append({"role": "assistant", "content": text, "timestamp": datetime.datetime.now()})
        logger.info(f"ConversationTracker: Added assistant message: '{text}' (total messages: {len(self.messages)})")

    def add_tool_usage(self, tool_name, result):
        """Protokolliert die Verwendung eines Tools."""
        self.tools_used.append({"name": tool_name, "result": result, "timestamp": datetime.datetime.now()})

    def end_call(self):
        """Beendet den Anruf und gibt eine Zusammenfassung zurück."""
        self.call_end_time = datetime.datetime.now()
        call_duration = self.call_end_time - self.call_start_time

        # Zusammenfassung erstellen
        summary = self._generate_summary()

        # Wenn E-Mail aktiviert ist, sende diese
        if EMAIL_ENABLED:
            self._send_email_summary(summary)

        return summary

    def _generate_summary(self):
        """Erstellt eine formatierte Zusammenfassung des Gesprächs."""
        if not self.call_start_time:
            return "Kein Anruf aufgezeichnet."

        call_duration = self.call_end_time - self.call_start_time if self.call_end_time else datetime.datetime.now() - self.call_start_time
        minutes, seconds = divmod(call_duration.seconds, 60)

        summary = []
        summary.append("=== James KI-Butler - Anrufzusammenfassung ===")
        summary.append(f"Datum: {self.call_start_time.strftime('%d.%m.%Y')}")
        summary.append(f"Uhrzeit: {self.call_start_time.strftime('%H:%M:%S')}")
        summary.append(f"Anrufer: {self.caller_number}")
        summary.append(f"Dauer: {minutes} Minuten, {seconds} Sekunden")
        summary.append("")

        if self.messages:
            summary.append("--- Gesprächsverlauf ---")
            for msg in self.messages:
                role = "Anrufer" if msg["role"] == "user" else "James"
                timestamp = msg["timestamp"].strftime("%H:%M:%S")
                summary.append(f"[{timestamp}] {role}: {msg['content']}")
        else:
            summary.append("--- Gesprächsverlauf ---")
            summary.append("Keine Nachrichten erfasst")

        if self.tools_used:
            summary.append("")
            summary.append("--- Verwendete Tools ---")
            for tool in self.tools_used:
                timestamp = tool["timestamp"].strftime("%H:%M:%S")
                summary.append(f"[{timestamp}] {tool['name']}: {tool['result']}")

        summary.append("")
        summary.append("=== Ende der Zusammenfassung ===")

        return "\n".join(summary)

    def _send_email_summary(self, summary):
        """Sendet die Zusammenfassung per E-Mail."""
        try:
            # Debug-Logging der verwendeten SMTP-Konfiguration
            logger.info(f"Sending email using SMTP server: {EMAIL_SMTP_SERVER}:{EMAIL_SMTP_PORT}")
            logger.info(f"From: {EMAIL_SENDER}, To: {EMAIL_RECIPIENT}")

            msg = MIMEMultipart()
            msg['From'] = EMAIL_SENDER
            msg['To'] = EMAIL_RECIPIENT
            msg[
                'Subject'] = f"James KI-Butler: Anrufzusammenfassung vom {self.call_start_time.strftime('%d.%m.%Y, %H:%M')}"

            msg.attach(MIMEText(summary, 'plain'))

            # Verwende die konfigurierte SMTP-Server-Einstellung
            server = smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT)
            server.set_debuglevel(1)  # Aktiviere SMTP-Debug-Ausgabe

            # Prüfe, ob TLS benötigt wird (die meisten Server benötigen es)
            try:
                server.starttls()
                logger.info("STARTTLS successful")
            except Exception as tls_error:
                logger.warning(f"STARTTLS not supported or failed: {tls_error}")

            # Prüfe, ob Authentifizierung konfiguriert ist
            if EMAIL_PASSWORD:
                logger.info(f"Authenticating with username: {EMAIL_SENDER}")
                server.login(EMAIL_SENDER, EMAIL_PASSWORD)
                logger.info("Authentication successful")
            else:
                logger.info("No password provided, skipping authentication")

            server.send_message(msg)
            logger.info("Email message sent")
            server.quit()

            logger.info(f"E-Mail-Zusammenfassung erfolgreich gesendet an {EMAIL_RECIPIENT}")
        except Exception as e:
            logger.error(f"Fehler beim Senden der E-Mail: {e}", exc_info=True)
            # Versuche alternativen E-Mail-Versand als Fallback
            try:
                logger.info("Attempting alternative email sending method...")
                from_addr = EMAIL_SENDER
                to_addr = EMAIL_RECIPIENT
                email_text = f"Subject: James KI-Butler: Anrufzusammenfassung\n\n{summary}"

                with smtplib.SMTP(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
                    server.set_debuglevel(1)
                    # Optional TLS
                    try:
                        server.starttls()
                    except:
                        logger.info("Alternative method: STARTTLS not used")

                    # Optional Authentication
                    if EMAIL_PASSWORD:
                        server.login(EMAIL_SENDER, EMAIL_PASSWORD)

                    # Senden
                    server.sendmail(from_addr, to_addr, email_text)

                logger.info("Alternative email method successful")
            except Exception as alt_e:
                logger.error(f"Alternative email method also failed: {alt_e}", exc_info=True)


print(f"Websockets version: {websockets.__version__}")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

# E-Mail Konfiguration
EMAIL_ENABLED = os.getenv('EMAIL_ENABLED', 'true').lower() == 'true'
EMAIL_SENDER = os.getenv('EMAIL_SENDER', 'your-email@example.com')
EMAIL_RECIPIENT = os.getenv('EMAIL_RECIPIENT', 'your-email@example.com')
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD', '')
EMAIL_SMTP_SERVER = os.getenv('EMAIL_SMTP_SERVER', 'smtp.gmail.com')
EMAIL_SMTP_PORT = int(os.getenv('EMAIL_SMTP_PORT', 587))

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

# Debug für Response-Typen, die wir noch nicht kennen
KNOWN_RESPONSE_TYPES = [
    'session.created', 'session.updated', 'input_audio_buffer.append',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_started',
    'input_audio_buffer.speech_stopped', 'response.audio.delta', 'response.content.delta',
    'response.content.done', 'response.done', 'rate_limits.updated',
    'response.function_call_arguments.delta', 'response.function_call_arguments.done',
    'conversation.item.retrieve.response', 'conversation.item.response', 'error'
]
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
        "properties": {},  # Keine Parameter nötig
        "required": []  # Keine Parameter nötig
    }
}

GET_WEATHER_TOOL = {
    "type": "function",
    "name": "getWeather",
    "description": "Aktuelle Temperatur oder Temperatur-Vorhersage für einen Ort",
    "parameters": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "Der Ort von dem der Benutzer das Wetter wissen möchte, z.Bsp. Gera. Die Funktion sollte nur mit dieser Information aufgerufen werden.",
            },
            "country": {
                "type": "string",
                "description": "Der Ländercode für das Land aus dem das Wetter abgefragt werden soll im zweistelligen Länderformat, z.Bsp. 'DE' für Deutschland oder 'IT' für Italien. Standard ist 'DE', wenn nicht anders angegeben.", # <-- Standard hinzugefügt
            },
            "forecast": {
                "type": "string",
                "description": "'no' = aktuelle Temperatur, 'yes' = Vorhersage für 7 Tage. Muss 'no' oder 'yes' sein.",
            }
        },
        "required": ["city", "forecast"],
    }
}

conversation_tracker = ConversationTracker()


def getPositionForcity(city: str, country: str): # <-- Typ-Hint für country hinzugefügt
    randomInt = random.randint(1, 3)
    if randomInt == 1:
        apiKey = "e0673530c180dc994fba1c54b9462d05"
    elif randomInt == 2:
        apiKey = "be66adb3b8df305b7cd9f36ad0ddbfa1"
    else:
        apiKey = "1e65b7c071d1d33f6c76ae35235c572a"
    logger.info(f"getPositionForcity called with city='{city}', country='{country}', using API Key ending in ...{apiKey[-4:]}") # <-- Verbessertes Logging
    print(city + " " + country)
    conn = http.client.HTTPConnection('api.positionstack.com')
    params = urllib.parse.urlencode({
        'access_key': apiKey,
        'query': city,
        'country': country,
        'limit': 1,
    })

    try:
        conn.request('GET', '/v1/forward?{}'.format(params))
        res = conn.getresponse()
        data = res.read()
        logger.info(f"PositionStack API Response Status: {res.status}, Reason: {res.reason}")
        dataJson = json.loads(data.decode('utf-8'))
        logger.debug(f"PositionStack API Response Data: {dataJson}")  # <-- Debug Log für Response

        if "data" in dataJson and dataJson["data"]:
            logger.info(
                f"Position found for {city}, {country}: {dataJson['data'][0]['latitude']}, {dataJson['data'][0]['longitude']}")
            return dataJson["data"][0]
        else:
            logger.warning(f"No position data found for {city}, {country} in PositionStack response: {dataJson}")
            return None  # <-- Rückgabe None, wenn nichts gefunden wurde
    except Exception as e:
        logger.error(f"Error calling PositionStack API for {city}, {country}: {e}", exc_info=True)
        return None  # <-- Rückgabe None bei Fehler
    finally:
        conn.close()  # <-- Verbindung schließen

def getWeather(city: str, country: str = "DE", forecast: str = "no"): # <-- Default für country hier
    logger.info(f"getWeather called with city='{city}', country='{country}', forecast='{forecast}'")
    posData = getPositionForcity(city, country)
    if not posData:
        logger.error(f"Could not get position for city '{city}', country '{country}'. Aborting weather fetch.")
        return f"Ich konnte die Position für {city} nicht finden, um das Wetter abzurufen."

    lat = posData.get("latitude")
    lon = posData.get("longitude")

    if lat is None or lon is None:
         logger.error(f"Latitude or Longitude missing in position data for {city}: {posData}")
         return f"Ich konnte die Koordinaten für {city} nicht finden."

    logger.info(f"Fetching weather for coordinates: Lat={lat}, Lon={lon}")

    try:
        if forecast == "no":
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m&current_weather=true" # <-- Zeitzone hinzugefügt, current statt hourly
            response = requests.get(url, timeout=10) # <-- Timeout hinzugefügt
            response.raise_for_status() # <-- Prüft auf HTTP-Fehler
            data = response.json()
            logger.info(f"Open-Meteo Current Weather Response: {data}")
            if "current_weather" in data and "temperature" in data["current_weather"]:
                temp = data["current_weather"]["temperature"]
                return f"Die aktuelle Temperatur in {city} beträgt {temp} Grad Celsius."
            else:
                 logger.warning(f"Unexpected response format from Open-Meteo (current): {data}")
                 return f"Ich konnte die aktuelle Temperatur für {city} nicht abrufen."

        else: # forecast == "yes"
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,rain_sum&forecast_days=7&timezone=Europe/Berlin" # <-- Angepasste URL, Zeitzone
            response = requests.get(url, timeout=10) # <-- Timeout hinzugefügt
            response.raise_for_status() # <-- Prüft auf HTTP-Fehler
            data = response.json()
            logger.info(f"Open-Meteo Forecast Response: {data}")
            # Hier könntest du die Vorhersage lesbarer formatieren, statt nur JSON zurückzugeben
            # Beispiel für eine einfache Formatierung:
            if "daily" in data and "time" in data["daily"] and "temperature_2m_max" in data["daily"]:
                forecast_str = f"Wettervorhersage für {city} für die nächsten Tage: "
                for i, date_str in enumerate(data["daily"]["time"]):
                     max_temp = data["daily"]["temperature_2m_max"][i]
                     min_temp = data["daily"]["temperature_2m_min"][i]
                     rain = data["daily"]["rain_sum"][i]
                     # Datum lesbarer formatieren (optional)
                     try:
                         dt_obj = datetime.datetime.fromisoformat(date_str)
                         formatted_date = dt_obj.strftime("%A, %d.%m.") # z.B. Donnerstag, 17.04.
                     except ValueError:
                         formatted_date = date_str
                     forecast_str += f"{formatted_date}: Max {max_temp}°C, Min {min_temp}°C, Regen {rain}mm. "
                return forecast_str.strip()
            else:
                 logger.warning(f"Unexpected response format from Open-Meteo (forecast): {data}")
                 return f"Ich konnte die Wettervorhersage für {city} nicht abrufen."

    except requests.exceptions.RequestException as e:
        logger.error(f"Error calling Open-Meteo API for {city} (Lat={lat}, Lon={lon}): {e}", exc_info=True)
        return f"Entschuldigung, es gab ein Problem beim Abrufen der Wetterdaten für {city}."
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON response from Open-Meteo for {city}: {e}", exc_info=True)
        return f"Entschuldigung, die Wetterdaten für {city} konnten nicht verarbeitet werden."
    except Exception as e:
        logger.error(f"Unexpected error in getWeather for {city}: {e}", exc_info=True)
        return f"Ein unerwarteter Fehler ist beim Abrufen des Wetters für {city} aufgetreten."


def get_current_date(*args, **kwargs):
    """Gibt das aktuelle Datum als formatierten String zurück.
    Ignoriert alle übergebenen Parameter."""
    now = datetime.datetime.now()
    date_str = now.strftime("%d. %B %Y") # Format z.B. "16. April 2025"
    return date_str


AVAILABLE_TOOLS = {
    "get_current_date": get_current_date,
    "getWeather": getWeather
}


# =============================================

@app.get("/", response_class=HTMLResponse)
async def index_page():
    return "<html><body><h1>Twilio Media Stream Server is running!</h1></body></html>"


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    try:
        # Für POST-Anfragen
        if request.method == "POST":
            form_data = await request.form()
            caller = form_data.get("From", "Unbekannt")
        # Für GET-Anfragen
        else:
            params = request.query_params
            caller = params.get("From", "Unbekannt")

        logger.info(f"Received incoming call from: {caller}")
        conversation_tracker.start_call(caller)
    except Exception as e:
        logger.error(f"Error getting caller information: {e}")
        logger.info("Starting call with unknown caller")
        conversation_tracker.start_call("Unbekannt")

    response = VoiceResponse()
    host = request.url.hostname
    # Verwende Hostname aus Request für korrekte WS-URL hinter Proxies/Load Balancern
    connect_host = request.headers.get("host", host)
    # Prüfe x-forwarded-proto oder request scheme für https
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
    """Handle WebSocket connections between Twilio and OpenAI."""
    # Verwende websocket.client für FastAPI/Starlette
    logger.info(f"WebSocket client connected from: {websocket.client}")
    await websocket.accept()
    logger.info("WebSocket connection accepted.")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1"
    }
    # NEUER ENDPUNKT (Stand April 2024 - überprüfe ggf. die aktuelle Doku)
    # openai_ws_url = "wss://api.openai.com/v1/audio/realtime/websocket?model=gpt-4o-mini&encoding=ulaw&sample_rate=8000"
    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"

    openai_ws = None  # Definiere außerhalb des try für finally-Block
    try:
        async with websockets.connect(
                openai_ws_url,
                additional_headers=headers
        ) as openai_ws_conn:
            openai_ws = openai_ws_conn  # Weise der äußeren Variable zu
            logger.info("Connected to OpenAI Realtime API.")
            await send_session_update(openai_ws)

            # Connection specific state
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None
            mark_queue = []
            response_start_timestamp_twilio = None
            current_function_calls = {}
            # Event für die Synchronisation von stream_sid
            stream_sid_ready = asyncio.Event()

            # --- Nested async functions ---
            # In der receive_from_twilio Funktion
            async def receive_from_twilio():
                """Empfängt Nachrichten von Twilio, verarbeitet Start/Connected, leitet Media weiter."""
                nonlocal stream_sid, latest_media_timestamp, last_assistant_item, response_start_timestamp_twilio, current_function_calls
                nonlocal stream_sid_ready

                start_received = False
                try:
                    # Warte auf initiale Nachrichten (Connected, dann Start)
                    logger.info("receive_from_twilio: Waiting for initial 'connected' and 'start' messages...")
                    for _ in range(2):  # Erwarte maximal 2 initiale Nachrichten
                        try:
                            message_text = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)  # Etwas längerer Timeout
                            data = json.loads(message_text)
                            event = data.get('event')
                            logger.info(f"receive_from_twilio: Received initial message: {event}")

                            if event == 'connected':
                                logger.info("receive_from_twilio: 'connected' event confirmed.")
                            elif event == 'start':
                                stream_sid = data['start']['streamSid']
                                logger.info(f"receive_from_twilio: 'start' event processed. stream_sid: {stream_sid}")
                                start_received = True
                                # Reset state
                                response_start_timestamp_twilio = None
                                latest_media_timestamp = 0
                                last_assistant_item = None
                                current_function_calls.clear()
                                logger.info("receive_from_twilio: State variables reset.")
                                # Signalisiere Bereitschaft
                                logger.info("receive_from_twilio: Setting stream_sid_ready event.")
                                stream_sid_ready.set()
                                break  # Wichtig: Breche die Schleife ab, nachdem 'start' empfangen wurde
                            else:
                                logger.warning(f"receive_from_twilio: Received unexpected initial message: {data}")
                        except asyncio.TimeoutError:
                            logger.error("receive_from_twilio: Timed out waiting for a message")
                            break
                        except json.JSONDecodeError as e:
                            logger.error(f"receive_from_twilio: JSON decode error in initial message: {e}")
                            break

                    # Überprüfe, ob 'start' empfangen wurde
                    if not start_received:
                        logger.error("receive_from_twilio: Did not receive 'start' message after initial messages.")
                        if not stream_sid_ready.is_set(): stream_sid_ready.set()  # Deadlock verhindern
                        return  # Task beenden

                except asyncio.TimeoutError:
                    logger.error("receive_from_twilio: Timed out waiting for initial messages from Twilio.")
                    if not stream_sid_ready.is_set(): stream_sid_ready.set()
                    return
                except WebSocketDisconnect as e:
                    logger.error(
                        f"receive_from_twilio: WebSocket disconnected during initial message handling: Code {e.code}",
                        exc_info=True)
                    if not stream_sid_ready.is_set(): stream_sid_ready.set()
                    return
                except (json.JSONDecodeError, Exception) as e:
                    logger.error(f"receive_from_twilio: Error processing initial messages: {e}", exc_info=True)
                    if not stream_sid_ready.is_set(): stream_sid_ready.set()
                    return

                # Hauptschleife für Media, Mark, Stop
                try:
                    logger.info("receive_from_twilio: Entering main loop for media/mark/stop events...")
                    async for message in websocket.iter_text():
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from Twilio (in loop): {e} - Message: {message}",
                                         exc_info=True)
                            continue
                        event = data.get('event')

                        if event == 'media' and openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
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
                            logger.info("Twilio call stopped event received in loop.")
                            # Anruf beenden und E-Mail senden
                            summary = conversation_tracker.end_call()
                            logger.info(f"Call ended. Summary:\n{summary}")
                            return  # Stop this task
                        else:
                            logger.debug(f"Unhandled Twilio event in main loop: {event}")

                    # === NEU: Log wenn die Schleife normal endet ===
                    logger.info("receive_from_twilio: iter_text loop finished WITHOUT stop event or exception.")
                    # ===============================================

                except WebSocketDisconnect as e:
                    logger.info(f"Twilio WebSocket disconnected during main loop. Code: {e.code}")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio main loop: {e}", exc_info=True)
                finally:
                    # === Angepasstes Finally ===
                    logger.info("receive_from_twilio task finished (exiting main loop section).")
                    # Event setzen, falls es noch nicht geschehen ist (Sicherheitsnetz)
                    if not stream_sid_ready.is_set():
                        logger.warning("receive_from_twilio: Setting stream_sid_ready event in finally block.")
                        stream_sid_ready.set()

                # Hauptschleife für Media, Mark, Stop
                try:
                    logger.info("receive_from_twilio: Entering main loop for media/mark/stop events...")
                    async for message in websocket.iter_text():
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from Twilio (in loop): {e} - Message: {message}",
                                         exc_info=True)
                            continue
                        event = data.get('event')

                        if event == 'media' and openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
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
                            if event == 'stop':
                                logger.info("Twilio call stopped event received in loop.")  # Nur loggen
                            if openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
                                logger.info("Closing OpenAI WebSocket due to Twilio stop event.")
                                # await openai_ws.close(code=1000, reason="Twilio call ended")
                            # === NEU: Log, wenn die Schleife normal endet ===
                            logger.info("receive_from_twilio: iter_text loop finished WITHOUT stop event or exception.")
                            # ===============================================
                            return  # Stop this task
                            # else: logger.debug(f"Unhandled Twilio event in loop: {event}")

                except WebSocketDisconnect as e:
                    logger.info(f"Twilio WebSocket disconnected during main loop. Code: {e.code}")
                except Exception as e:
                    logger.error(f"Error in receive_from_twilio main loop: {e}", exc_info=True)
                finally:
                    # === Angepasstes Finally ===
                    logger.info("receive_from_twilio task finished (exiting main loop section).")
                    # Event setzen, falls es noch nicht geschehen ist (Sicherheitsnetz)
                    if not stream_sid_ready.is_set():
                        logger.warning("receive_from_twilio: Setting stream_sid_ready event in finally block.")
                        stream_sid_ready.set()
                    # OpenAI WS NICHT hier schließen. Wird im äußeren Handler gemacht.
                    # =========================

            async def send_to_twilio():
                """Empfängt von OpenAI, sendet Audio, verarbeitet Tool Calls."""
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, current_function_calls
                try:
                    # Warte, bis stream_sid von receive_from_twilio gesetzt wurde
                    logger.info("send_to_twilio: Waiting for stream_sid to be set...")
                    try:
                        await asyncio.wait_for(stream_sid_ready.wait(), timeout=10.0)
                        # Prüfe, ob stream_sid nach dem Warten tatsächlich gesetzt wurde
                        if not stream_sid:
                            logger.error(
                                "send_to_twilio: stream_sid_ready event was set, but stream_sid is still None!")
                            return  # Beende den Task, da Senden unmöglich ist
                        logger.info(f"send_to_twilio: stream_sid is ready (value: {stream_sid}). Proceeding.")
                    except asyncio.TimeoutError:
                        logger.error("send_to_twilio: Timed out waiting for stream_sid! Cannot proceed.")
                        return

                    # Hauptschleife für OpenAI-Nachrichten
                    async for openai_message in openai_ws:
                        try:
                            response = json.loads(openai_message)
                        except json.JSONDecodeError as e:
                            logger.error(f"JSON decode error from OpenAI: {e} - Message: {openai_message}",
                                         exc_info=True)
                            continue

                        response_type = response.get('type')

                        # ---- BENUTZER-TRANSKRIPTE ERFASSEN ----

                        # 1. Direktes Transkript im Response
                        if 'transcript' in response and 'role' in response and response.get('role') == 'user':
                            transcript = response.get('transcript')
                            logger.info(f"USER TRANSCRIPT found in {response_type}: {transcript}")
                            conversation_tracker.add_user_message(transcript)

                        # 2. Transkript bei committed buffer
                        if response_type == 'input_audio_buffer.committed':
                            try:
                                transcript = None

                                # Direkt im Event
                                if 'transcript' in response:
                                    transcript = response.get('transcript')

                                # In Metadata
                                elif 'metadata' in response and response.get(
                                        'metadata') and 'transcript' in response.get('metadata', {}):
                                    transcript = response.get('metadata', {}).get('transcript')

                                # API-Anfrage für Transkript
                                elif 'item_id' in response:
                                    item_id = response.get('item_id')
                                    transcript_query = {
                                        "type": "conversation.item.retrieve",
                                        "item_id": item_id
                                    }
                                    logger.info(f"Requesting transcript for user item: {item_id}")
                                    await openai_ws.send(json.dumps(transcript_query))

                                # Speichern wenn vorhanden
                                if transcript:
                                    logger.info(f"Adding user transcript (direct): {transcript}")
                                    conversation_tracker.add_user_message(transcript)

                            except Exception as e:
                                logger.error(f"Error processing user input: {e}", exc_info=True)

                        # 3. Transkript aus speech_stopped
                        if response_type == 'input_audio_buffer.speech_stopped' and 'transcript' in response:
                            transcript = response.get('transcript')
                            if transcript:
                                logger.info(f"Adding user transcript from speech_stopped: {transcript}")
                                conversation_tracker.add_user_message(transcript)

                        # 4. Transkript aus item.retrieve response
                        if response_type in ['conversation.item.retrieve.response', 'conversation.item.response']:
                            try:
                                item = response.get('item', {})
                                if item.get('role') == 'user':
                                    # String content
                                    if isinstance(item.get('content'), str):
                                        transcript = item.get('content')
                                        if transcript:
                                            logger.info(f"Adding user transcript (string content): {transcript}")
                                            conversation_tracker.add_user_message(transcript)

                                    # List content
                                    elif isinstance(item.get('content'), list):
                                        user_text = ""
                                        for content in item.get('content', []):
                                            if isinstance(content, str):
                                                user_text += content + " "
                                            elif isinstance(content, dict):
                                                if content.get('type') in ['text', 'transcription',
                                                                           'audio'] and content.get('text'):
                                                    user_text += content.get('text') + " "
                                                elif 'text' in content:
                                                    user_text += content.get('text') + " "
                                                elif 'transcript' in content:
                                                    user_text += content.get('transcript') + " "

                                        if user_text.strip():
                                            logger.info(f"Adding user transcript (object content): {user_text.strip()}")
                                            conversation_tracker.add_user_message(user_text.strip())

                                    # Direct properties
                                    elif item.get('transcript'):
                                        transcript = item.get('transcript')
                                        logger.info(f"Adding user transcript (item transcript): {transcript}")
                                        conversation_tracker.add_user_message(transcript)
                                    elif item.get('text'):
                                        text = item.get('text')
                                        logger.info(f"Adding user transcript (item text): {text}")
                                        conversation_tracker.add_user_message(text)

                            except Exception as e:
                                logger.error(f"Error processing item response: {e}", exc_info=True)

                        # ---- STANDARD EVENT HANDLING ----

                        # Logging und Fehlerbehandlung
                        if response_type in LOG_EVENT_TYPES or response_type.startswith("response.function_call"):
                            logger.info(f"Received from OpenAI: Type={response_type}, Data={response}")

                        # Fehlerbehandlung
                        if response_type == 'error':
                            logger.error(f"!!! OpenAI API Error: {response.get('error')}")
                            continue

                        # Session Update
                        if response_type == 'session.updated':
                            logger.info(f"OpenAI Session Updated. Final config: {response.get('session')}")

                        # ---- ASSISTANT-TRANSKRIPTE ERFASSEN ----

                        # Bei response.done Assistant-Nachricht erfassen
                        if response_type == 'response.done':
                            try:
                                output_items = response.get('response', {}).get('output', [])
                                for item in output_items:
                                    if item.get('type') == 'message' and item.get('role') == 'assistant':
                                        content_list = item.get('content', [])
                                        for content in content_list:
                                            if content.get('type') == 'audio' and 'transcript' in content:
                                                transcript = content.get('transcript')
                                                logger.info(f"Adding assistant transcript: {transcript}")
                                                conversation_tracker.add_assistant_message(transcript)
                            except Exception as transcript_error:
                                logger.error(f"Error processing assistant transcript: {transcript_error}")

                        # ---- AUDIO STREAMING ----

                        # Audio an Twilio senden
                        if response_type == 'response.audio.delta' and 'delta' in response:
                            is_sid_set = bool(stream_sid)  # Sollte jetzt immer True sein
                            is_ws_connected = websocket.client_state == WebSocketState.CONNECTED

                            logger.debug(
                                f"Audio Delta Check: stream_sid set? {is_sid_set}, websocket connected? {is_ws_connected}")

                            if is_sid_set and is_ws_connected:
                                audio_payload = response['delta']
                                audio_delta = {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": audio_payload}
                                }
                                await websocket.send_json(audio_delta)

                                # Timestamps und Mark setzen
                                if response_start_timestamp_twilio is None:
                                    response_start_timestamp_twilio = latest_media_timestamp
                                if response.get('item_id'):
                                    last_assistant_item = response['item_id']
                                await send_mark(websocket, stream_sid)
                            else:
                                logger.warning(
                                    f"Cannot send audio delta. stream_sid={stream_sid}, state={websocket.client_state}")

                            # Audio-Transkript erfassen wenn vorhanden
                            if response.get('transcript'):
                                transcript = response.get('transcript')
                                if transcript:
                                    conversation_tracker.add_assistant_message(transcript)

                        # ---- UNTERBRECHUNG DURCH BENUTZER ----

                        # Unterbrechung durch Benutzer
                        if response_type == 'input_audio_buffer.speech_started':
                            logger.info("User speech started detected.")
                            if last_assistant_item:
                                logger.info(f"Interrupting response with id: {last_assistant_item}")
                                await handle_speech_started_event()

                        # ---- TOOL CALLING ----

                        # Tool Calling - Argumente sammeln
                        if response_type == 'response.function_call_arguments.delta':
                            call_id = response.get('call_id')
                            delta = response.get('delta')
                            if call_id and delta is not None:
                                if call_id not in current_function_calls:
                                    current_function_calls[call_id] = {"name": None, "arguments": ""}
                                    logger.info(f"Receiving arguments for function call {call_id}...")
                                current_function_calls[call_id]["arguments"] += delta

                        # Tool Calling - Ausführung
                        elif response_type == 'response.function_call_arguments.done':
                            call_id = response.get('call_id')
                            function_name_from_event = response.get('name')

                            if call_id in current_function_calls:
                                current_function_calls[call_id]["name"] = function_name_from_event
                                logger.info(
                                    f"Finished receiving arguments for function call {call_id}, Name: '{function_name_from_event}'")

                                function_call_info = current_function_calls.pop(call_id)
                                function_name = function_call_info['name']
                                arguments_str = function_call_info['arguments']
                                logger.info(f"Executing tool: {function_name} with args: {arguments_str}")

                                if function_name and function_name in AVAILABLE_TOOLS:
                                    try:
                                        # Tool ausführen
                                        tool_function = AVAILABLE_TOOLS[function_name]

                                        # Mit oder ohne Argumente aufrufen je nach Struktur
                                        if arguments_str.strip() in ['{}', '']:
                                            result = tool_function()  # Ohne Argumente
                                        else:
                                            try:
                                                arguments = json.loads(arguments_str)
                                                result = tool_function(**arguments)  # Mit Argumenten
                                            except (json.JSONDecodeError, TypeError):
                                                # Fallback ohne Argumente
                                                result = tool_function()

                                        output_str = str(result)
                                        logger.info(
                                            f"Tool '{function_name}' executed successfully. Result: {output_str}")

                                        # Für die Zusammenfassung protokollieren
                                        conversation_tracker.add_tool_usage(function_name, output_str)

                                        # Erzeuge die Antwort
                                        function_output = {
                                            "type": "conversation.item.create",
                                            "item": {
                                                "type": "function_call_output",
                                                "call_id": str(call_id),
                                                "output": output_str
                                            }
                                        }

                                        # Sende das Ergebnis
                                        logger.info(f"Sending function_call_output for {call_id} to OpenAI.")
                                        await openai_ws.send(json.dumps(function_output))

                                        # Neue Antwort anfordern
                                        logger.info("Requesting new response after tool execution.")
                                        await openai_ws.send(json.dumps({"type": "response.create"}))

                                    except Exception as e:
                                        logger.error(f"Error executing/sending tool '{function_name}': {e}",
                                                     exc_info=True)
                                else:
                                    logger.warning(f"Received request for unknown tool: {function_name}")
                            else:
                                logger.warning(f"Received function_call_arguments.done for unknown call_id: {call_id}")

                        # ---- UNBEKANNTE EVENT-TYPEN ----

                        if response_type not in KNOWN_RESPONSE_TYPES:
                            logger.info(f"NEW RESPONSE TYPE: {response_type}")
                            logger.info(f"UNKNOWN RESPONSE DATA: {response}")

                            # Versuch, Transkripte zu extrahieren
                            if 'transcript' in response:
                                transcript = response.get('transcript')
                                logger.info(f"Found transcript in unknown response: {transcript}")
                                # Nach Rolle zuordnen
                                if 'role' in response and response.get('role') == 'user':
                                    conversation_tracker.add_user_message(transcript)
                                else:
                                    conversation_tracker.add_assistant_message(transcript)

                except websockets.exceptions.ConnectionClosed as e:
                    logger.info(f"OpenAI WebSocket connection closed. Code: {e.code}, Reason: {e.reason}")
                except Exception as e:
                    logger.error(f"Error in send_to_twilio: {e}", exc_info=True)
                finally:
                    # Anruf als beendet markieren
                    if conversation_tracker.call_start_time and not conversation_tracker.call_end_time:
                        summary = conversation_tracker.end_call()
                        logger.info(f"Call force-ended in finally block. Summary:\n{summary}")
                    logger.info("send_to_twilio task finished.")

            async def handle_speech_started_event():
                """Behandelt Unterbrechung durch Benutzer."""
                nonlocal response_start_timestamp_twilio, last_assistant_item
                logger.info("Handling speech started event for interruption.")
                # Verwende korrekten State für FastAPI/Starlette
                if websocket.client_state != WebSocketState.CONNECTED:
                    logger.warning("Cannot handle speech started event, Twilio WebSocket is not connected.")
                    return

                if mark_queue and response_start_timestamp_twilio is not None and last_assistant_item:
                    elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                    logger.info(f"Truncating item {last_assistant_item} at {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    if openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
                        logger.info(f"Sending truncate event to OpenAI: {truncate_event}")
                        await openai_ws.send(json.dumps(truncate_event))
                    else:
                        logger.warning("Cannot send truncate event, OpenAI WebSocket is closed or unavailable.")

                    logger.info(f"Sending clear event to Twilio for stream {stream_sid}")
                    await websocket.send_json({"event": "clear", "streamSid": stream_sid})

                    # Reset state
                    mark_queue.clear()
                    last_assistant_item = None
                    response_start_timestamp_twilio = None
                else:
                    logger.info("Skipping interruption handling: Conditions not met.")

            async def send_mark(connection, stream_sid_local):
                """Sendet Mark-Events an Twilio."""
                # Verwende korrekten State für FastAPI/Starlette
                if stream_sid_local and connection.client_state == WebSocketState.CONNECTED:
                    mark_name = f"response_part_{len(mark_queue)}"
                    mark_event = {
                        "event": "mark",
                        "streamSid": stream_sid_local,
                        "mark": {"name": mark_name}
                    }
                    await connection.send_json(mark_event)
                    mark_queue.append(mark_name)
                else:
                    logger.warning(
                        f"Cannot send mark: stream_sid={stream_sid_local}, connection_state={connection.client_state}")

            # --- Ende der nested functions ---

            # Starte die beiden Haupt-Tasks
            logger.info("Starting receive_from_twilio and send_to_twilio tasks.")
            receive_task = asyncio.create_task(receive_from_twilio())
            send_task = asyncio.create_task(send_to_twilio())

            # Warte, bis einer der Tasks beendet ist
            done, pending = await asyncio.wait(
                [receive_task, send_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            logger.info(f"One task finished: {done}. Pending tasks: {pending}")
            # Beende verbleibende Tasks sauber
            for task in pending:
                logger.info(f"Cancelling pending task: {task.get_name()}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.info(f"Task {task.get_name()} was cancelled successfully.")
                except Exception as e:  # Fange Fehler beim Canceln ab
                    logger.error(f"Error during cancellation of task {task.get_name()}: {e}", exc_info=True)
            logger.info("All tasks finished or cancelled.")

    except websockets.exceptions.WebSocketException as e:  # OpenAI Verbindungsfehler
        logger.error(f"Failed to connect to OpenAI WebSocket: {e}", exc_info=True)
        # Stelle sicher, dass Twilio geschlossen wird
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=1011, reason="OpenAI connection failed")
            except RuntimeError:
                pass
    except Exception as e:  # Andere unerwartete Fehler
        logger.error(f"An unexpected error occurred in handle_media_stream: {e}", exc_info=True)
        # Stelle sicher, dass Twilio geschlossen wird
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close(code=1011, reason="Unexpected handler error")
            except RuntimeError:
                pass
    finally:
        logger.info("Final cleanup in handle_media_stream.")
        # Schließe OpenAI WS, falls noch offen
        if openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
            logger.info("Closing OpenAI WebSocket in final finally block.")
            await openai_ws.close(code=1000, reason="Handler finished")
        # Schließe Twilio WS, falls noch offen (letzte Instanz)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            logger.info("Closing Twilio WebSocket in final finally block.")
            try:
                await websocket.close(code=1000, reason="Handler finished normally")
            except RuntimeError:
                logger.warning("Tried to close Twilio WebSocket in final finally block, but it was already closed.")
        else:
            logger.info("Twilio WebSocket already closed before final finally block.")


async def send_initial_conversation_item(openai_ws):
    """Sendet die initiale Nachricht und fordert eine Antwort an."""
    logger.info("Sending initial conversation item to start the conversation.")
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Stell dich als James von Couture & Pixels vor und frage, wie du helfen kannst."
                }
            ]
        }
    }
    # --- Debugging für TypeError ---
    logger.info(f"DEBUG: Type of initial_conversation_item: {type(initial_conversation_item)}")
    logger.info(f"DEBUG: Value of initial_conversation_item before dump:\n{initial_conversation_item}")
    # --- Ende Debugging ---
    try:
        # Sende die initiale Nachricht
        await openai_ws.send(json.dumps(initial_conversation_item))
        logger.info("Initial conversation item sent successfully.")

        # Fordere explizit Antwort an
        logger.info("Sending response.create for initial greeting.")
        await openai_ws.send(json.dumps({"type": "response.create"}))

    except TypeError as e:
        # Logge den Fehler und die Struktur, die Probleme machte
        logger.error(f"!!! JSON dump failed in send_initial_conversation_item: {e}", exc_info=True)
        logger.error(f"Problematic structure was: {initial_conversation_item}")
    except Exception as e:
        logger.error(f"Error sending initial item or response.create: {e}", exc_info=True)


async def send_session_update(openai_ws):
    """Sendet das Session Update mit Tools an OpenAI."""
    logger.info("Preparing session update with tools.")
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
            "tools": [GET_CURRENT_DATE_TOOL, GET_WEATHER_TOOL]
        }
    }

    serialized_data = None
    try:
        # Serialisiere zuerst
        serialized_data = json.dumps(session_update)
        logger.info("DEBUG: JSON serialization of session_update successful.")

        # Logge den serialisierten String
        logger.info(f"Sending session update to OpenAI: {serialized_data}")

        # Sende den serialisierten String
        await openai_ws.send(serialized_data)
        logger.info("Session update sent successfully.")

        # Fahre erst fort, wenn das Senden erfolgreich war
        await send_initial_conversation_item(openai_ws)

    except TypeError as e:
        # Fange den JSON-Fehler ab
        logger.error(f"!!! JSON dump failed in send_session_update: {e}", exc_info=True)
        logger.error(f"Problematic structure was: {session_update}")
        # Logge Typen zur weiteren Diagnose
        logger.info(f"DEBUG: Type of VOICE: {type(VOICE)}")
        logger.info(f"DEBUG: Type of SYSTEM_MESSAGE: {type(SYSTEM_MESSAGE)}")
        # Beende die Verbindung oder handle den Fehler anders
        if openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
            await openai_ws.close(code=1011, reason="JSON serialization error during session update")
        raise e  # Wirft den Fehler weiter, um den Handler zu beenden

    except Exception as e:
        logger.error(f"Error sending session update or calling initial item: {e}", exc_info=True)
        if openai_ws and openai_ws.state == websockets.protocol.State.OPEN:
            await openai_ws.close(code=1011, reason="Error during session update/initial send")
        raise e  # Wirft den Fehler weiter


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting FastAPI server on 0.0.0.0:{PORT}")
    # Explizit mit einem Worker starten, um Probleme mit asyncio Events/State zu vermeiden
    uvicorn.run(app, host="0.0.0.0", port=PORT, workers=1)
