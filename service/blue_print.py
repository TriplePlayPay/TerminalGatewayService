import asyncio
import json as OG_JSON
from typing import Dict, Any, Optional
from dataclasses import dataclass

from sanic import json, Request, SanicException, Websocket, Blueprint
from sanic.response import JSONResponse

from service.logger import get_logger
from service.authorization import get_api_key_from_http_request, get_authorization_from_api_key
from service.json_util import ignore_properties

logger = get_logger()

bp = Blueprint("TerminalGateway", url_prefix="/api/terminal-gateway")


# Timeouts & retry knobs
WS_CHARGE_TIMEOUT_S = 600  # generous for tip flows
TERMINAL_LOOKUP_MAX_RETRIES = 3
TERMINAL_LOOKUP_BACKOFF_BASE = 3  # seconds, exponential
SEND_MAX_ATTEMPTS = 3



def _evaluate_pax_details(details: Dict[str, Any]) -> bool:
    """
    PAX success criteria: DeviceResponseCode == "000000"
    """
    device_response_code = details.get("DeviceResponseCode")
    if not device_response_code:
        logger.error(f"No device response code present in: {details}")
        raise ValueError("No Device Response Code Available In Response")

    if device_response_code != "000000":
        logger.error(f"Non Zero Response Code In: {details}")
        return False

    return True


def _evaluate_newpos_details(details: Dict[str, Any]) -> bool:
    """
    NEWPOS success criteria: AUTH_RESP == "00"
    """
    device_response_code = details.get("AUTH_RESP")
    if not device_response_code:
        logger.error(f"No device response code present in: {details}")
        raise ValueError("No Device Response Code Available In Response")

    if device_response_code != "00":
        logger.error(f"Non Zero Response Code In: {details}")
        return False

    return True


def _coerce_text(msg: Any) -> Optional[str]:
    """
    Convert incoming ws frame payload to str, if possible.
    """
    if msg is None:
        return None
    if isinstance(msg, (bytes, bytearray)):
        try:
            return msg.decode("utf-8", "ignore")
        except Exception:  # pragma: no cover
            return None
    if isinstance(msg, str):
        return msg
    # Anything else (e.g. dict) is unexpected here
    return None


# A global in-memory store of connected terminals:
# {
#   "someTerminalId": {
#       "websocket": <WebSocket connection object>,
#       "pending_requests": { "someRequestId": <asyncio.Future>, ... }
#   },
#   ...
# }
TERMINAL_CONNECTIONS: Dict[str, Dict[str, Any]] = {}


@dataclass
class BaseRequestInput:
    reference: str


@dataclass
class ChargeRequestInput(BaseRequestInput):
    amount: str
    laneId: str
    terminal_payment_type: str
    terminal_type: str
    mid_override: Optional[str] = "",
    order_identifier: Optional[str] = ""


@bp.websocket("/")
async def pax_terminal_ws(request: Request, ws: Websocket):
    """
    A WebSocket endpoint that your Desktop Application can connect to.
    It must supply:
      - An Authorization header that matches SECRET_KEY.
      - A 'terminal_id' query param or some unique identifier for the terminal.

    Once connected, the server can push "charge", "refund", etc. requests
    and wait for the Desktop App to respond.
    """
    try:
        # Listen for messages from the Desktop App in an infinite loop
        async for msg in ws:
            logger.info(f"Raw msg received at pax ws: {msg}")
            data = OG_JSON.loads(msg)
            logger.info(f"JSON msg received at pax ws: {data}")
            request_id = data.get("request_id")
            apikey = (
                data.pop("publishable_key", "")
                or data.pop("public_key", "")
                or data.pop("apikey", "")
                or data.pop("api_key", "")
                or data.pop("ApiKey", "")
                or data.pop("iframekey", "")
            )
            logger.info(f"Api key from pax message: {apikey}")

            # Authorize the request
            is_authorized = await get_authorization_from_api_key(apikey)
            if not is_authorized:
                logger.error(f"Unauthorized request made with apikey: {apikey}")
                await ws.send(OG_JSON.dumps({"error": "Invalid auth token"}))
                await ws.close()
                return

            terminal_id = data.get("lane_id") or data.get("LaneId")
            if not terminal_id:
                logger.error(f"No lane_id provided in the request, closing socket: {data}")
                await ws.send(OG_JSON.dumps({"error": "No lane_id or LaneId provided"}))
                await ws.close()
                return

            if terminal_id in TERMINAL_CONNECTIONS.keys():

                # Preserve any pending requests in a variable
                existing_pending = TERMINAL_CONNECTIONS[terminal_id].get(
                    "pending_requests", {}
                )

                # Now replace the old entry with the new WebSocket,
                # and keep the existing pending requests if desired
                TERMINAL_CONNECTIONS[terminal_id] = {
                    "websocket": ws,
                    "pending_requests": existing_pending,
                }

                logger.info(
                    f"Replaced old connection for terminal_id={terminal_id} with new one"
                )

            else:
                # Register this connection in our global dictionary
                logger.info(
                    f"Registering terminal_id in global dictionary: {terminal_id}"
                )
                TERMINAL_CONNECTIONS[terminal_id] = {"websocket": ws, "pending_requests": {}}

            # If it's a response to one of our requests, fulfill the future
            if (
                request_id
                and request_id in TERMINAL_CONNECTIONS[terminal_id]["pending_requests"]
            ):
                logger.info(
                    f"Attempting to resolve: {request_id} request at terminal: {terminal_id}"
                )
                fut = TERMINAL_CONNECTIONS[terminal_id]["pending_requests"].pop(request_id)
                logger.info(
                    f"setting future response: {request_id} with data: {data}"
                )
                fut.set_result(data)
            else:
                # Otherwise handle any new/unsolicited messages from the Desktop
                # e.g. status updates, device info, etc.
                logger.info(
                    f"Received msg from {terminal_id}: {data}"
                )
    except Exception:
        logger.exception("Exception in websocket operations")


# ----------------------------
# NEW: WebSocket charge endpoint
# ----------------------------
@bp.websocket("/charge/ws")
async def charge_ws(request: Request, ws: Websocket):
    """
    WebSocket endpoint for your *server-to-server* caller (the other API).
    Client must:
      - Include Authorization header (Bearer <api_key>) in the WS handshake.
      - Send a JSON message containing ChargeRequestInput fields.
    We keep the socket open, ignore any non-JSON frames, and only close after sending a final 'result'.
    """
    # Authorize the caller using the WS handshake headers
    api_key = get_api_key_from_http_request(request)
    is_authorized = await get_authorization_from_api_key(api_key)
    if not is_authorized:
        await ws.send(OG_JSON.dumps({"type": "result", "status": False, "message": "Unauthorized"}))
        await ws.close()
        return

    # Wait for the first valid JSON charge payload, ignoring any noise frames
    request_input: Optional[ChargeRequestInput] = None
    raw_payload: Optional[Dict[str, Any]] = None
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
        except asyncio.TimeoutError:
            await ws.send(OG_JSON.dumps({"type": "result", "status": False, "message": "No payload received"}))
            await ws.close()
            return
        except Exception:
            logger.exception("Error receiving initial charge payload on WS")
            await ws.send(OG_JSON.dumps({"type": "result", "status": False, "message": "WS receive error"}))
            await ws.close()
            return

        raw_text = _coerce_text(raw)
        if raw_text is None:
            # Ignore non-text/undecodable frames
            continue

        try:
            candidate = OG_JSON.loads(raw_text)
        except OG_JSON.JSONDecodeError:
            # Ignore invalid JSON (heartbeats etc.)
            continue
        except Exception:
            logger.exception("Unexpected error parsing initial WS payload; ignoring frame")
            continue

        try:
            # Coerce into our dataclass (ignore extra keys)
            request_input = ignore_properties(ChargeRequestInput, candidate)
            raw_payload = candidate
            break
        except Exception as e:
            # The frame was JSON but not the expected shape; keep waiting
            logger.warning(f"JSON payload did not match ChargeRequestInput shape: {e}; ignoring frame")
            continue

    assert request_input is not None  # for type checkers
    terminal_id = request_input.laneId
    reference = request_input.reference

    # Informational ack
    await ws.send(OG_JSON.dumps({
        "type": "ack",
        "request_id": reference,
        "laneId": terminal_id,
        "message": "Charge received; searching for terminal"
    }))

    # Try to locate the terminal with exponential backoff (and report progress)
    for attempt in range(1, TERMINAL_LOOKUP_MAX_RETRIES + 1):
        if terminal_id in TERMINAL_CONNECTIONS:
            await ws.send(OG_JSON.dumps({
                "type": "info",
                "request_id": reference,
                "laneId": terminal_id,
                "message": f"Terminal {terminal_id} found (attempt {attempt})"
            }))
            break
        else:
            if attempt < TERMINAL_LOOKUP_MAX_RETRIES:
                backoff_seconds = TERMINAL_LOOKUP_BACKOFF_BASE ** attempt
                logger.error(
                    f"Attempt {attempt}/{TERMINAL_LOOKUP_MAX_RETRIES}: "
                    f"Terminal {terminal_id} not found. Current connected terminals: {list(TERMINAL_CONNECTIONS.keys())}"
                )
                await ws.send(OG_JSON.dumps({
                    "type": "info",
                    "request_id": reference,
                    "laneId": terminal_id,
                    "message": f"Terminal not connected; retrying in {backoff_seconds}s"
                }))
                await asyncio.sleep(backoff_seconds)
            else:
                await ws.send(OG_JSON.dumps({
                    "type": "result",
                    "status": False,
                    "message": (
                        f"Terminal {terminal_id} not connected. Please ensure the terminal is on and online."
                    ),
                    "laneId": terminal_id,
                    "amount": request_input.amount,
                    "request_id": reference,
                }))
                await ws.close()
                return

    # Prepare message to send to the desktop terminal
    device_message = {
        "action": "charge",
        "request_id": reference,
        "laneId": terminal_id,
        "amount": float(request_input.amount),
        "terminal_payment_type": request_input.terminal_payment_type,
        "terminal_type": request_input.terminal_type,
        "orderIdentifier": request_input.order_identifier,
        "merchantKey": api_key,
        "mid_override": request_input.mid_override,
    }

    # Future to await the desktop device response
    fut = asyncio.get_event_loop().create_future()
    TERMINAL_CONNECTIONS[terminal_id]["pending_requests"][reference] = fut

    # Try to send to the terminal with a few retries
    send_success = False
    for attempt in range(1, SEND_MAX_ATTEMPTS + 1):
        try:
            ws_terminal = TERMINAL_CONNECTIONS[terminal_id]["websocket"]
            await ws_terminal.send(OG_JSON.dumps(device_message))
            send_success = True
            break
        except Exception as e:
            logger.exception(f"Attempt {attempt} to send to terminal {terminal_id} failed: {e}")
            await asyncio.sleep(1)

    if not send_success:
        # Clean pending future since nothing was sent
        TERMINAL_CONNECTIONS[terminal_id]["pending_requests"].pop(reference, None)
        await ws.send(OG_JSON.dumps({
            "type": "result",
            "status": False,
            "message": "Intermittent issue communicating with the terminal. Please retry shortly.",
            "laneId": terminal_id,
            "amount": request_input.amount,
            "request_id": reference,
        }))
        await ws.close()
        return

    # Await device response, ignoring any non-JSON noise from *this* WS (we never send noise here)
    try:
        response = await asyncio.wait_for(fut, timeout=WS_CHARGE_TIMEOUT_S)
        response_details = response.get("details") or {}

        if request_input.terminal_type == "PAX":
            is_successful = _evaluate_pax_details(response_details)
        elif request_input.terminal_type == "NEWPOS":
            is_successful = _evaluate_newpos_details(response_details)
        else:
            is_successful = False

        final_payload = {
            "type": "result",
            "status": is_successful,
            "message": response.get("message", "AUTHORIZED" if is_successful else "DECLINED"),
            "laneId": terminal_id,
            "amount": request_input.amount,
            "fee": response.get("fee", "0.00"),
            "tip": response.get("tip", "0.00"),
            "receipt": [],
            "details": response.get("details"),
            "request_id": reference,
        }
        await ws.send(OG_JSON.dumps(final_payload))
    except asyncio.TimeoutError:
        await ws.send(OG_JSON.dumps({
            "type": "result",
            "status": False,
            "message": "Timeout waiting for terminal response",
            "laneId": terminal_id,
            "amount": request_input.amount,
            "request_id": reference,
        }))
    except Exception:
        logger.exception("Unhandled error in charge_ws")
        await ws.send(OG_JSON.dumps({
            "type": "result",
            "status": False,
            "message": "Internal server error",
            "laneId": terminal_id,
            "amount": request_input.amount,
            "request_id": reference,
        }))
    finally:
        # This WS is single-shot: close after final result is sent
        try:
            await ws.close()
        except Exception:
            pass


@bp.post("/charge")
async def charge(request: Request) -> JSONResponse:
    """
    Sends a charge command to the relevant terminal.

    :param request: Request
    :return: JSONResponse
    """

    logger.info(f"Charge called with the following parameters: {request.json}")
    request_input = ignore_properties(ChargeRequestInput, request.json)

    # Authorize the request
    api_key = get_api_key_from_http_request(request)
    is_authorized = await get_authorization_from_api_key(api_key)
    if not is_authorized:
        raise SanicException("Not authorized to perform this action.", status_code=401)

    logger.info(
        f"Here are connected terminals: {TERMINAL_CONNECTIONS.keys()}"
    )
    terminal_id = request_input.laneId

    # Attempt to find the terminal in the relevant connections
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        if terminal_id in TERMINAL_CONNECTIONS:
            # Found it! Break out of the loop and proceed
            logger.info(
                f"{terminal_id} found on attempt {attempt}"
            )
            break
        else:
            if attempt < max_retries:
                # Exponential backoff time, e.g. 2^(attempt) seconds
                backoff_seconds = 3 ** attempt

                logger.error(
                    f"Attempt {attempt}/{max_retries}: "
                    f"Terminal {terminal_id} not found. Current connected terminals: {list(TERMINAL_CONNECTIONS.keys())}"
                )
                logger.error(
                    f"Will retry {terminal_id} in {backoff_seconds} seconds..."
                )

                await asyncio.sleep(backoff_seconds)
            else:
                # Final attempt failed — return an error
                return json({
                    "status": False,
                    "message": (
                        f"Terminal {terminal_id} not connected. Please ensure the "
                        "terminal is connected to wifi and is on."
                    ),
                    "amount": "0.00",
                })

    message = {
        "action": "charge",
        "request_id": request_input.reference,
        "laneId": terminal_id,
        "amount": float(request_input.amount),
        "terminal_payment_type": request_input.terminal_payment_type,
        "terminal_type": request_input.terminal_type,
        "orderIdentifier": request_input.order_identifier,
        "merchantKey": api_key,
        "mid_override": request_input.mid_override
    }

    def __evaluate_details(details: Dict[str, Any]) -> bool:
        """
        Basically tells us if a request is successful based on its details
        :param details: Dict[str, Any]
        :return: bool
        """
        device_response_code = details.get("DeviceResponseCode")
        if not device_response_code:
            logger.error(f"No device response code present in: {details}")
            raise ValueError("No Device Response Code Available In Response")

        if device_response_code != "000000":
            logger.error(f"Non Zero Response Code In: {details}")
            return False

        return True

    def __evaluate_new_pos_details(details: Dict[str, Any]) -> bool:
        """
        Basically tells us if a request is successful based on its details
        :param details: Dict[str, Any]
        :return: bool
        """
        device_response_code = details.get("AUTH_RESP")
        if not device_response_code:
            logger.error(f"No device response code present in: {details}")
            raise ValueError("No Device Response Code Available In Response")

        if device_response_code != "00":
            logger.error(f"Non Zero Response Code In: {details}")
            return False

        return True

    # Create a Future so we can wait for the response
    fut = asyncio.get_event_loop().create_future()

    send_success = False
    for attempt in range(1, 4):  # up to 3 attempts
        try:
            # Store the Future in pending_requests
            TERMINAL_CONNECTIONS[terminal_id]["pending_requests"][request_input.reference] = fut

            # Pull the latest WebSocket in case it changed
            ws = TERMINAL_CONNECTIONS[terminal_id]["websocket"]

            # Attempt to send
            await ws.send(OG_JSON.dumps(message))

            # If it works, break out of the loop
            send_success = True
            break
        except Exception as e:
            logger.error(
                f"Attempt {attempt} to send WebSocket message failed for terminal {terminal_id}. "
                f"Error: {e}"
            )
            logger.exception(
                f"Attempt {attempt} to send WebSocket message failed for terminal {terminal_id}."
            )
            # Optionally sleep a short time before retrying
            # (This is up to you)
            await asyncio.sleep(1)

    # If after 3 attempts we still haven't succeeded, handle it
    if not send_success:
        logger.error(f"Unable to submit to lane id: {terminal_id}")
        # We can remove this future from pending_requests since we never sent the request
        # (No device can possibly respond to a message that never went out)
        TERMINAL_CONNECTIONS[terminal_id]["pending_requests"].pop(request_input.reference, None)

        return json({
            "status": False,
            "message": (
                "Intermittent issue communicating with the terminal. "
                "Please wait a few moments and retry."
            ),
            "laneId": terminal_id,
            "amount": request_input.amount,
        })

    try:
        # Await the response from the device or time out
        response = await asyncio.wait_for(fut, timeout=270)
        # You can shape the final return dict to match your format
        """
        {
            "status": True,
            "amount": payload.get("amount", "0.00"),
            "fee": payload.get("fee", "0.00"),
            "tip": payload.get("tip", "0.00"),
            "message": {
                "amount": 0,
            },
            "details": {"authorization_id": self.reference},
            "receipt": [],
        }
        """
        response_details = response.get("details")
        if request_input.terminal_type == "PAX":
            is_successful = __evaluate_details(response_details)
        elif request_input.terminal_type == "NEWPOS":
            is_successful = __evaluate_new_pos_details(response_details)
        else:
            is_successful = False

        return json({
            "status": is_successful,
            "message": response.get("message", ""),
            "laneId": terminal_id,
            "amount": request_input.amount,
            "fee": response.get("fee", "0.00"),
            "tip": response.get("tip", "0.00"),
            "receipt": [],
            "details": response.get("details"),
        })
    except asyncio.TimeoutError:
        return json({
            "status": False,
            "message": "Timeout waiting for terminal response",
            "laneId": terminal_id,
            "amount": request_input.amount,
        })
