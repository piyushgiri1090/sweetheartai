import ctypes
import os
import sys
import threading
from pathlib import Path

from server import HindiAIHTTPServer, HindiAIHandler


def show_startup_error(message):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "Sweetheart", 0x10)
    else:
        print(message, file=sys.stderr)


def main():
    try:
        import webview
    except ImportError as error:
        message = (
            "Desktop app dependencies are missing. Build this app with "
            "build-desktop.ps1 or install requirements-desktop.txt."
        )
        show_startup_error(message)
        raise RuntimeError(message) from error

    server = HindiAIHTTPServer(("127.0.0.1", 0), HindiAIHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address, port = server.server_address

    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    webview_data = local_app_data / "Sweetheart" / "WebView2"
    webview_data.mkdir(parents=True, exist_ok=True)

    webview.create_window(
        "Sweetheart",
        f"http://{address}:{port}/",
        width=1100,
        height=760,
        min_size=(380, 520),
        background_color="#090d15",
    )
    try:
        webview.start(
            debug=False,
            private_mode=False,
            storage_path=str(webview_data),
        )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
