"""以 HTTPS 方式启动 note-api（用于证书过期演练）。"""
from __future__ import annotations

import ssl
import sys

from .service import Handler, log
from http.server import ThreadingHTTPServer


def main() -> None:
    port = int(sys.argv[1])
    host = sys.argv[2]
    cert, key = sys.argv[3], sys.argv[4]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    server = ThreadingHTTPServer((host, port), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    log("INFO", f"note-api started (tls) cert={cert} listen={host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
