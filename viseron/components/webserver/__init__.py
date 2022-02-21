"""Viseron webserver."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import TYPE_CHECKING

import tornado.gen
import tornado.ioloop
import tornado.web
import voluptuous as vol
from tornado.routing import PathMatches

from viseron.const import (
    EVENT_CAMERA_REGISTERED,
    REGISTERED_CAMERAS,
    VISERON_SIGNAL_SHUTDOWN,
)

from .api import APIRouter
from .const import (
    COMPONENT,
    CONFIG_DEBUG,
    CONFIG_PORT,
    DEFAULT_DEBUG,
    DEFAULT_PORT,
    PATH_STATIC,
    WEBSOCKET_COMMANDS,
)
from .not_found_handler import NotFoundHandler
from .request_handler import ViseronRequestHandler
from .stream_handler import DynamicStreamHandler, StaticStreamHandler
from .websocket_api import WebSocketHandler
from .websocket_api.commands import get_cameras, subscribe_event

if TYPE_CHECKING:
    from viseron import Event, Viseron
    from viseron.domains.camera import AbstractCamera


LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = vol.Schema(
    {
        COMPONENT: vol.Schema(
            {
                vol.Optional(CONFIG_PORT, default=DEFAULT_PORT): vol.All(
                    int, vol.Range(min=1024, max=49151)
                ),
                vol.Optional(CONFIG_DEBUG, default=DEFAULT_DEBUG): bool,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def setup(vis: Viseron, config):
    """Set up the webserver component."""
    config = config[COMPONENT]
    webserver = WebServer(vis, config)
    vis.register_signal_handler(VISERON_SIGNAL_SHUTDOWN, webserver.stop)

    webserver.register_websocket_command(subscribe_event)
    webserver.register_websocket_command(get_cameras)

    webserver.start()

    return True


class IndexHandler(ViseronRequestHandler):
    """Handler for index page."""

    def get(self):
        """GET request."""
        self.render(os.path.join(PATH_STATIC, "index.html"))


class DeprecatedStreamHandler(tornado.web.RequestHandler):
    """Socket handler."""

    def get(self, camera):
        """GET request."""
        LOGGER.warning(
            f"The endpoint /{camera}/stream is deprecated. "
            f"Please use /{camera}/mjpeg-stream instead."
        )
        self.redirect(f"/{camera}/mjpeg-stream")


class WebServer(threading.Thread):
    """Webserver."""

    def __init__(self, vis: Viseron, config):
        super().__init__(name="Tornado WebServer", daemon=True)
        self._vis = vis
        self._config = config

        vis.data[COMPONENT] = self
        vis.data[WEBSOCKET_COMMANDS] = {}

        ioloop = asyncio.new_event_loop()
        asyncio.set_event_loop(ioloop)
        self.application = self.create_application()
        self.application.listen(config[CONFIG_PORT])
        self._ioloop = tornado.ioloop.IOLoop.current()

        self._vis.listen_event(EVENT_CAMERA_REGISTERED, self.camera_registered)
        for camera in self._vis.data[REGISTERED_CAMERAS].values():
            self._serve_camera_recordings(camera)

    def create_application(self):
        """Return tornado web app."""
        application = tornado.web.Application(
            [
                (
                    r"/(?P<camera>[A-Za-z0-9_]+)/mjpeg-stream",
                    DynamicStreamHandler,
                    {"vis": self._vis},
                ),
                (
                    (
                        r"/(?P<camera>[A-Za-z0-9_]+)/static-mjpeg-streams/"
                        r"(?P<mjpeg_stream>[A-Za-z0-9_\-]+)"
                    ),
                    StaticStreamHandler,
                    {"vis": self._vis},
                ),
                (r"/websocket", WebSocketHandler, {"vis": self._vis}),
                (r"/(?P<camera>[A-Za-z0-9_]+)/stream", DeprecatedStreamHandler),
                (r"/.*", IndexHandler, {"vis": self._vis}),
            ],
            default_handler_class=NotFoundHandler,
            static_path=PATH_STATIC,
            debug=self._config[CONFIG_DEBUG],
        )
        application.add_handlers(
            r".*",
            [
                (PathMatches(r"/api/.*"), APIRouter(self._vis, application)),
            ],
        )
        return application

    def register_websocket_command(self, handler):
        """Register a websocket command."""
        if handler.command in self._vis.data[WEBSOCKET_COMMANDS]:
            LOGGER.error(f"Command {handler.command} has already been registered")
            return

        self._vis.data[WEBSOCKET_COMMANDS][handler.command] = (handler, handler.schema)

    def _serve_camera_recordings(self, camera: AbstractCamera):
        """Serve recordings of each camera in a static file handler."""
        self.application.add_handlers(
            r".*",
            [
                (
                    fr"/recordings/{camera.identifier}/(.*)",
                    tornado.web.StaticFileHandler,
                    {"path": camera.recorder.recordings_folder},
                )
            ],
        )

    def camera_registered(self, event_data: Event):
        """Handle camera registering."""
        camera: AbstractCamera = event_data.data
        self._serve_camera_recordings(camera)

    def run(self):
        """Start ioloop."""
        self._ioloop.start()
        self._ioloop.close()

    def stop(self):
        """Stop ioloop."""
        LOGGER.debug("Stopping webserver")
        self._ioloop.stop()