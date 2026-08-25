"""Small, local web remote powered by pyatv."""

import asyncio
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

import pyatv
from pyatv.storage.file_storage import FileStorage


routes = web.RouteTableDef()
INDEX_FILE = Path(__file__).with_name("index.html")

REMOTE_COMMANDS = {
    "up",
    "down",
    "left",
    "right",
    "select",
    "menu",
    "home",
    "play_pause",
    "previous",
    "next",
    "skip_forward",
    "skip_backward",
}
AUDIO_COMMANDS = {"volume_up", "volume_down"}
POWER_COMMANDS = {"turn_on", "turn_off"}


@web.middleware
async def same_origin_commands(
    request: web.Request, handler: web.RequestHandler
) -> web.StreamResponse:
    """Block cross-site webpages from sending commands to local devices."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        expected_origin = f"{request.scheme}://{request.host}"
        if origin is not None and origin != expected_origin:
            raise web.HTTPForbidden(text="Cross-origin commands are not allowed")
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def playing_to_dict(playing: pyatv.interface.Playing) -> dict[str, Any]:
    """Convert a Playing instance to JSON-safe data."""
    return {
        "title": playing.title,
        "artist": playing.artist,
        "album": playing.album,
        "genre": playing.genre,
        "position": playing.position,
        "total_time": playing.total_time,
        "state": playing.device_state.name.lower(),
        "media_type": playing.media_type.name.lower(),
    }


def device_to_dict(config: pyatv.interface.BaseConfig, connected: bool) -> dict[str, Any]:
    """Convert a discovered device configuration to JSON-safe data."""
    return {
        "id": config.identifier,
        "name": config.name,
        "address": str(config.address),
        "model": config.device_info.model_str,
        "connected": connected,
        "protocols": [service.protocol.name.lower() for service in config.services],
    }


async def send_update(client: web.WebSocketResponse, payload: dict[str, Any]) -> None:
    """Send an update unless the browser has already disconnected."""
    try:
        await client.send_json(payload)
    except (ConnectionError, RuntimeError):
        pass


class DeviceListener(pyatv.interface.DeviceListener, pyatv.interface.PushListener):
    """Forward device and playback updates to browser clients."""

    def __init__(self, app: web.Application, identifier: str):
        self.app = app
        self.identifier = identifier

    def connection_lost(self, exception: Exception) -> None:
        self._remove()

    def connection_closed(self) -> None:
        self._remove()

    def _remove(self) -> None:
        self.app["atv"].pop(self.identifier, None)
        if self in self.app["listeners"]:
            self.app["listeners"].remove(self)

    def playstatus_update(
        self, updater: pyatv.interface.PushUpdater, playstatus: pyatv.interface.Playing
    ) -> None:
        for client in list(self.app["clients"].get(self.identifier, [])):
            asyncio.create_task(send_update(client, playing_to_dict(playstatus)))

    def playstatus_error(
        self, updater: pyatv.interface.PushUpdater, exception: Exception
    ) -> None:
        for client in list(self.app["clients"].get(self.identifier, [])):
            asyncio.create_task(send_update(client, {"error": str(exception)}))


def connected_device(request: web.Request) -> pyatv.interface.AppleTV:
    """Return the connected device from a request or raise a useful HTTP error."""
    device_id = request.match_info["id"]
    atv = request.app["atv"].get(device_id)
    if atv is None:
        raise web.HTTPConflict(
            text=f"Not connected to {device_id}", content_type="text/plain"
        )
    return atv


@routes.get("/")
async def index(_request: web.Request) -> web.FileResponse:
    """Serve the remote control UI."""
    return web.FileResponse(INDEX_FILE)


@routes.get("/api/devices")
async def devices(request: web.Request) -> web.Response:
    """Discover devices on the local network."""
    try:
        results = await pyatv.scan(
            asyncio.get_running_loop(), storage=request.app["storage"]
        )
    except Exception as ex:
        raise web.HTTPServiceUnavailable(text=f"Scan failed: {ex}") from ex

    return web.json_response(
        [
            device_to_dict(config, config.identifier in request.app["atv"])
            for config in results
            if config.identifier is not None
        ]
    )


@routes.post("/api/devices/{id}/connect")
async def connect(request: web.Request) -> web.Response:
    """Connect to a discovered device using credentials from pyatv storage."""
    device_id = request.match_info["id"]
    if device_id in request.app["atv"]:
        return web.json_response({"connected": True, "message": "Already connected"})

    results = await pyatv.scan(
        asyncio.get_running_loop(),
        identifier=device_id,
        storage=request.app["storage"],
    )
    if not results:
        raise web.HTTPNotFound(text="Device not found")

    try:
        atv = await pyatv.connect(
            results[0], asyncio.get_running_loop(), storage=request.app["storage"]
        )
    except Exception as ex:
        raise web.HTTPUnauthorized(
            text=(
                f"Could not connect: {ex}. If this is an authentication error, "
                "run 'atvremote wizard' once and try again."
            )
        ) from ex

    listener = DeviceListener(request.app, device_id)
    atv.listener = listener
    request.app["listeners"].append(listener)
    request.app["atv"][device_id] = atv

    try:
        atv.push_updater.listener = listener
        atv.push_updater.start()
    except (pyatv.exceptions.NotSupportedError, pyatv.exceptions.BlockedStateError):
        pass

    return web.json_response({"connected": True, "name": results[0].name})


@routes.post("/api/devices/{id}/command/{command}")
async def command(request: web.Request) -> web.Response:
    """Run one explicitly supported remote, audio, or power command."""
    atv = connected_device(request)
    command_name = request.match_info["command"]

    try:
        if command_name in REMOTE_COMMANDS:
            await getattr(atv.remote_control, command_name)()
        elif command_name in AUDIO_COMMANDS:
            await getattr(atv.audio, command_name)()
        elif command_name in POWER_COMMANDS:
            await getattr(atv.power, command_name)()
        else:
            raise web.HTTPBadRequest(text=f"Unsupported command: {command_name}")
    except web.HTTPException:
        raise
    except Exception as ex:
        raise web.HTTPBadRequest(text=f"Command failed: {ex}") from ex

    return web.json_response({"ok": True, "command": command_name})


@routes.get("/api/devices/{id}/playing")
async def playing(request: web.Request) -> web.Response:
    """Return current playback metadata."""
    atv = connected_device(request)
    try:
        status = await atv.metadata.playing()
    except Exception as ex:
        raise web.HTTPBadRequest(text=f"Could not fetch playback state: {ex}") from ex
    return web.json_response(playing_to_dict(status))


@routes.delete("/api/devices/{id}/connect")
async def close_connection(request: web.Request) -> web.Response:
    """Close a device connection."""
    atv = connected_device(request)
    atv.close()
    return web.json_response({"connected": False})


@routes.get("/api/devices/{id}/updates")
async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """Stream playback updates to a browser."""
    atv = connected_device(request)
    device_id = request.match_info["id"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    request.app["clients"].setdefault(device_id, []).append(ws)

    try:
        try:
            await ws.send_json(playing_to_dict(await atv.metadata.playing()))
        except Exception as ex:
            await send_update(ws, {"error": f"Could not fetch playback state: {ex}"})
        async for msg in ws:
            if msg.type == WSMsgType.TEXT and msg.data == "close":
                await ws.close()
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        request.app["clients"].get(device_id, []).remove(ws)

    return ws


async def on_startup(app: web.Application) -> None:
    """Load credentials saved by atvremote."""
    storage = FileStorage.default_storage(asyncio.get_running_loop())
    await storage.load()
    app["storage"] = storage


async def on_shutdown(app: web.Application) -> None:
    """Close device connections and save pyatv settings."""
    for atv in list(app["atv"].values()):
        atv.close()
    await app["storage"].save()


def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application(middlewares=[same_origin_commands])
    app["atv"] = {}
    app["listeners"] = []
    app["clients"] = {}
    app.add_routes(routes)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    """Run the local web remote."""
    web.run_app(create_app(), host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
