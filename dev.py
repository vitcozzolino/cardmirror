"""Run CardMirror and restart it when source files change."""

import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
POLL_INTERVAL_SECONDS = 0.5


def source_snapshot():
    files = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
            continue
        files.append(path)
    return {
        path: (path.stat().st_mtime_ns, path.stat().st_size)
        for path in files
        if path.is_file()
    }


def stop_process(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main():
    snapshot = source_snapshot()
    process = None
    try:
        while True:
            print("[dev] Starting CardMirror...", flush=True)
            process = subprocess.Popen(
                [sys.executable, "main.py", *sys.argv[1:]],
                cwd=PROJECT_ROOT,
            )
            while process.poll() is None:
                time.sleep(POLL_INTERVAL_SECONDS)
                current_snapshot = source_snapshot()
                if current_snapshot != snapshot:
                    print("[dev] Source change detected; restarting...", flush=True)
                    stop_process(process)
                    snapshot = current_snapshot
                    break
            else:
                return process.returncode or 0
    except KeyboardInterrupt:
        print("[dev] Stopping.", flush=True)
        return 130
    finally:
        if process is not None:
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
