"""Single-process launcher for Sentinel AI on :5001.

Uses wsgiref rather than app.run(): the Flask dev server, even with
use_reloader=False, was repeatedly leaving two processes bound to 5001 on
this machine. One wsgiref process, one socket, no reloader fork.
"""
import os
from socketserver import ThreadingMixIn
from wsgiref.simple_server import make_server, WSGIServer

from app import app, db, init_badges


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """One thread per connection.

    The plain wsgiref server is single-threaded, so one long-lived request --
    and the twin holds several: the SSE /stream endpoint plus keep-alive
    sockets from each open map -- blocks every other request and the whole
    page appears to hang. daemon_threads lets the process exit cleanly.
    """
    daemon_threads = True


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        init_badges()
        os.makedirs(app.config.get('UPLOAD_FOLDER', 'static/uploads'), exist_ok=True)
    print("Starting Sentinel AI (SentinelAI-SHIH) on http://127.0.0.1:5001 ...")
    make_server('0.0.0.0', 5001, app, server_class=ThreadingWSGIServer).serve_forever()
