#!/usr/bin/env python3
"""
run.py – Start BOTH the Discord bot and the Flask dashboard in parallel.
Usage:  python run.py
"""
import subprocess, sys, os, signal, threading

ROOT = os.path.dirname(os.path.abspath(__file__))

def stream(proc, prefix):
    for line in iter(proc.stdout.readline, b''):
        print(f"[{prefix}] {line.decode().rstrip()}", flush=True)

procs = []

def shutdown(sig, frame):
    print("\nShutting down…")
    for p in procs:
        p.terminate()
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

bot_env = {**os.environ, "PYTHONUNBUFFERED": "1"}

bot_proc = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "bot", "bot.py")],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=bot_env,
)
dash_proc = subprocess.Popen(
    [sys.executable, os.path.join(ROOT, "dashboard", "app.py")],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=bot_env,
)
procs.extend([bot_proc, dash_proc])

threading.Thread(target=stream, args=(bot_proc,  "BOT "), daemon=True).start()
threading.Thread(target=stream, args=(dash_proc, "DASH"), daemon=True).start()

bot_proc.wait()
dash_proc.wait()
