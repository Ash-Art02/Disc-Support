"""Auto-restart wrapper: python run.py
- Runs bot.py, restarts it if it crashes
- Auto-restarts when bot.py / .env changes (so updates = just save the file)
- Use this instead of `python bot.py` for local background hosting.
"""
import os
import sys
import time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
BOT = os.path.join(HERE, "bot.py")
WATCH = {"bot.py", ".env", "requirements.txt"}

def mtimes():
    out = {}
    for name in WATCH:
        p = os.path.join(HERE, name)
        try:
            out[name] = os.path.getmtime(p)
        except OSError:
            out[name] = 0
    return out

def main():
    last = mtimes()
    backoff = 5
    print(f"Watching {BOT} - edit + save to auto-update. Ctrl+C to stop.")
    while True:
        proc = subprocess.Popen([sys.executable, BOT], cwd=HERE)
        try:
            while proc.poll() is None:
                time.sleep(2)
                cur = mtimes()
                if cur != last:
                    print("Change detected - restarting bot...")
                    last = cur
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
            else:
                # process exited on its own (crash or /restart which exits 0 via exec - actually exec replaces, so this is crash)
                code = proc.returncode
                print(f"Bot exited with code {code} - restarting in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                last = mtimes()
                continue
            backoff = 5  # clean file-change restart, reset backoff
        except KeyboardInterrupt:
            print("Stopping...")
            try:
                proc.terminate()
            except Exception:
                pass
            sys.exit(0)

if __name__ == "__main__":
    main()
