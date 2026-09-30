"""Vercel serverless endpoint: GET /api/run

Called automatically by Vercel Cron (sends 'Authorization: Bearer <CRON_SECRET>').
For manual runs in a browser, pass the secret as ?key=<CRON_SECRET>:
  /api/run?key=SECRET&preview=1   -> show the report in the browser, no mail, nothing saved
  /api/run?key=SECRET&force=1     -> send now, even if already sent today / market holiday
"""
import hmac
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ichimoku.core import run  # noqa: E402


def _authorised(headers, key):
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return False
    auth = headers.get("Authorization", "")
    return hmac.compare_digest(auth, f"Bearer {secret}") or hmac.compare_digest(key, secret)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        flag = lambda name: qs.get(name, ["0"])[0] == "1"

        if not _authorised(self.headers, qs.get("key", [""])[0]):
            return self._reply(401, "Unauthorized", "text/plain")

        status, body, ctype = run(send=not flag("preview"), force=flag("force"))
        self._reply(status, body, ctype)

    def _reply(self, status, body, ctype):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
