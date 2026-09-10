# launcher.py
"""Dedicated entrypoint for the standalone Windows portable launcher.

Handles:
- Immediate GUI splash screen creation using tkinter.
- Suppressing console stream crashes in windowed GUI mode via NullWriter.
- Spawning system tray icon via pystray and Pillow (lazy-loaded).
- Auto-opening default browser pointing to the running backend.
- Launching the FastAPI server (importing and running app.py).
"""
import os
import sys
import threading
import time
import webbrowser

# Define a dummy NullWriter to suppress standard stream crashes (isatty etc.) in GUI mode
class NullWriter:
    def write(self, text):
        pass
    def flush(self):
        pass
    def isatty(self):
        return False

if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()


splash_root = None

# If running from a frozen PyInstaller bundle, launch the splash screen IMMEDIATELY
if getattr(sys, 'frozen', False):
    import tkinter as tk

    def show_splash_instantly():
        global splash_root
        try:
            splash_root = tk.Tk()
            splash_root.title("Faustus")
            splash_root.overrideredirect(True)
            splash_root.configure(bg="#1a1c23")

            # Accented borders
            splash_root.config(highlightbackground="#e06c75", highlightcolor="#e06c75", highlightthickness=1)

            w, h = 360, 160
            ws = splash_root.winfo_screenwidth()
            hs = splash_root.winfo_screenheight()
            x = (ws - w) // 2
            y = (hs - h) // 2
            splash_root.geometry(f"{w}x{h}+{x}+{y}")

            tk.Label(splash_root, text="⛵ Faustus", font=("Segoe UI", 22, "bold"), bg="#1a1c23", fg="#e06c75").pack(pady=(22, 2))
            tk.Label(splash_root, text="Launching background services...", font=("Segoe UI", 10), bg="#1a1c23", fg="#d1d4e0").pack(pady=2)
            tk.Label(splash_root, text="Please wait, this will take a few seconds.", font=("Segoe UI", 8, "italic"), bg="#1a1c23", fg="#5c6370").pack(pady=(12, 0))

            splash_root.attributes("-topmost", True)
            splash_root.mainloop()
        except Exception:
            pass

    # Launch the GUI splash screen immediately on a background thread
    threading.Thread(target=show_splash_instantly, daemon=True).start()


def create_tray_image():
    # Generate a beautiful 64x64 icon matching Faustus brand red accent (#e06c75)
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)
    accent_red = (224, 108, 117, 255)
    light_red = (224, 108, 117, 150)

    # Draw premium sailing boat
    dc.polygon([(32, 10), (32, 45), (12, 45)], fill=accent_red)
    dc.polygon([(32, 18), (32, 45), (48, 45)], fill=light_red)
    dc.polygon([(8, 48), (56, 48), (44, 56), (20, 56)], fill=accent_red)
    return image


def on_open_browser(icon, item, url):
    webbrowser.open(url)


def on_exit(icon, item):
    icon.stop()
    os._exit(0)


def setup_system_tray(url):
    try:
        import pystray
        icon_img = create_tray_image()
        menu = (
            pystray.MenuItem('Open Faustus', lambda icon, item: on_open_browser(icon, item, url), default=True),
            pystray.MenuItem('Exit', on_exit)
        )
        tray_icon = pystray.Icon(
            "Faustus",
            icon_img,
            "Faustus",
            menu
        )
        tray_icon.run()
    except Exception:
        pass


def open_browser(url):
    # Allow uvicorn and app lifecycles to complete warmups
    time.sleep(3.5)

    # Safely close the splash screen
    try:
        global splash_root
        if splash_root:
            splash_root.after(0, splash_root.destroy)
    except Exception:
        pass

    webbrowser.open(url)


if __name__ == "__main__":
    import uvicorn

    # OPS-05: mark this boot attempt BEFORE the risky part (importing app.py,
    # which runs the app's own startup init). If the process dies here or
    # anywhere before `mark_boot_completed()` below, the NEXT launch sees this
    # one as unfinished — two such unfinished boots in a row is what puts the
    # next launch into safe mode on its own (see src/safe_mode.py). Best-effort:
    # a settings-write hiccup here must never block starting the app itself.
    try:
        from src import safe_mode
        safe_mode.mark_boot_started()
    except Exception:
        safe_mode = None

    # Import the FastAPI app from app.py
    from app import app

    if safe_mode is not None:
        try:
            safe_mode.mark_boot_completed()
        except Exception:
            pass

    bind_host = os.getenv("APP_BIND", "127.0.0.1")
    bind_port = int(os.getenv("APP_PORT", "7000"))
    url = f"http://{bind_host}:{bind_port}"

    if getattr(sys, 'frozen', False):
        # Start browser manager thread
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        # Start system tray manager thread
        threading.Thread(target=setup_system_tray, args=(url,), daemon=True).start()

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
