"""
watch_incoming.py
Watches incoming/ for new Callyzer CSV exports and ingests them
automatically, using Windows' native file-system change notifications
(via the `watchdog` package) instead of polling.

You still export manually from Callyzer. The moment the exported CSV
lands in incoming/, this script runs the same ingest logic as
ingest_callyzer.py and moves the file to incoming/processed/ — no
command to run by hand.

USAGE:
    python watch_incoming.py

Leave the window open while you work; it watches until you close it
(Ctrl+C also stops it cleanly). See Start_watch.bat for a double-click
launcher.
"""

import os
import sys
import time

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from common import get_connection, SCRIPT_DIR
from ingest_callyzer import INCOMING_DIR, PROCESSED_DIR, process_file

# New exports can take a moment to finish writing to disk. Wait until the
# file size stops changing before reading it, so we don't ingest a
# half-written CSV.
SETTLE_CHECKS = 3
SETTLE_INTERVAL_SEC = 0.5


def _wait_until_settled(path):
    last_size = -1
    stable_count = 0
    while stable_count < SETTLE_CHECKS:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            return False  # file vanished (e.g. moved away already)
        if size == last_size:
            stable_count += 1
        else:
            stable_count = 0
            last_size = size
        time.sleep(SETTLE_INTERVAL_SEC)
    return True


class CallyzerExportHandler(FileSystemEventHandler):
    def _maybe_ingest(self, path):
        if not path.lower().endswith(".csv"):
            return
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(INCOMING_DIR):
            return  # ignore files already inside incoming/processed/
        if not _wait_until_settled(path):
            return
        print(f"\n[{time.strftime('%H:%M:%S')}] New export detected: {os.path.basename(path)}")
        # watchdog delivers events on its own thread; sqlite3 connections
        # can't be shared across threads, so open a fresh one per file.
        conn = get_connection()
        try:
            process_file(conn, path)
        except Exception as exc:
            print(f"  ERROR ingesting {os.path.basename(path)}: {exc}")
        finally:
            conn.close()

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_ingest(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._maybe_ingest(event.dest_path)


def main():
    os.makedirs(INCOMING_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

    handler = CallyzerExportHandler()
    observer = Observer()
    observer.schedule(handler, INCOMING_DIR, recursive=False)
    observer.start()

    print(f"Watching {INCOMING_DIR} for new Callyzer exports...")
    print("Export your CSV from Callyzer and save/copy it into that folder.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\nStopped watching.")
    observer.join()


if __name__ == "__main__":
    main()
