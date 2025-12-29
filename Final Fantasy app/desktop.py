import threading
import webview
import time
from backend.app import create_app
from waitress import serve
import socket as s

def get_free_port():
    sock = s.socket()
    sock.bind(('', 0))
    addr, port = sock.getsockname()
    sock.close()
    return port

def run_server(port):
    app = create_app()
    serve(app, host='127.0.0.1', port=port, threads=4)

if __name__ == "__main__":
    port = get_free_port()
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    time.sleep(0.6)
    # Force modern Edge Chromium engine for reliable CSS Grid/Flexbox support
    webview.create_window(
        "Fantasy Sports Team Analyzer",
        f"http://127.0.0.1:{port}/",
        width=1100,
        height=760,
        min_size=(980, 640)
    )
    webview.start(gui='edgechromium', debug=False)
