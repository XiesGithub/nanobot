"""Hold private HTTP delivery gates until the transport actually writes bytes."""

from websockets.asyncio.server import ServerConnection

from nanobot.knowledge.mail.contracts import MailError
from nanobot.knowledge.mail.delivery import acquire_delivery


class PrivateMailConnection(ServerConnection):
    def send_data(self):
        # websockets calls this synchronously from send_context after queuing the
        # HTTP response. Release only after transport.write has committed bytes.
        locks = getattr(self, "_mail_delivery_locks", None)
        try:
            return super().send_data()
        finally:
            if locks is not None:
                self._mail_delivery_locks = None
                for lock in reversed(locks):
                    lock.release()


async def finish_private_http(service, connection, request, response):
    pending = getattr(connection, "_mail_pending_http", None)
    if pending is not None:
        if "Cache-Control" in response.headers:
            del response.headers["Cache-Control"]
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
    if pending is None or response.status_code >= 400:
        return response
    from nanobot.channels.websocket import _http_error
    access, validate = pending
    connection._mail_pending_http = None
    locks = []
    try:
        locks = await acquire_delivery(service, access)
        if validate:
            validate()
        connection._mail_delivery_locks = locks
        return response
    except MailError as error:
        for lock in reversed(locks):
            lock.release()
        return _http_error(403, error.code)
    except BaseException:
        for lock in reversed(locks):
            lock.release()
        raise
