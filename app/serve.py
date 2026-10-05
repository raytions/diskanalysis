"""Start uvicorn on a dual-stack socket.

Python 3.14 creates AF_INET6 sockets with IPV6_V6ONLY set, so binding ::
does not accept IPv4. Clear the flag before the listen socket is bound.
"""
import os
import socket

import uvicorn.server as server


def _install_dualstack() -> None:
    _startup = server.Server.startup

    async def startup(self, sockets=None):
        loop = __import__("asyncio").get_running_loop()
        _create = loop.create_server

        async def create_server(*args, **kwargs):
            host = kwargs.get("host")
            if host and ":" in host and kwargs.get("sock") is None:
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, kwargs.get("port")))
                sock.setblocking(False)
                kwargs = {k: v for k, v in kwargs.items() if k not in ("host", "port")}
                kwargs["sock"] = sock
            return await _create(*args, **kwargs)

        loop.create_server = create_server
        return await _startup(self, sockets)

    server.Server.startup = startup


def main() -> None:
    _install_dualstack()
    import uvicorn

    host = os.environ.get("APP_HOST", "::")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
