#!/usr/bin/env python3
"""Emporium Print Studio: the Mac app. Starts the engine in the background and shows it in its own window.

    desktop.py                 open the app window
    desktop.py --selftest      check that everything inside the app works, then exit (used by the build)
    desktop.py --headless      run the engine without a window and print the owner link (for development)
"""
import sys
import time


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        import selftest
        selftest.run()
        return
    import server
    server.init()
    port = server.start_background()
    url = f"http://127.0.0.1:{port}/?owner={server.OWNER_TOKEN}"
    if "--headless" in args:
        print(url, flush=True)
        while True:
            time.sleep(3600)
    import webview
    window = webview.create_window("Emporium Print Studio", url, width=1440, height=900, min_size=(1000, 660))

    def picker():
        res = window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else None

    server.PICKER = picker
    webview.start()


if __name__ == "__main__":
    main()
