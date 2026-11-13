"""Capture check for the Jarvis app's audio helper: start it, receive both audio streams, report levels.

Standard library only, so it runs before any speech model is installed:

    python3 scripts/ears_spike.py --seconds 30 --say "Testing, one two three"

With --say the Mac speaks the sentence part-way through, which should show up
on the system stream (and on the microphone stream when the speakers are on).
"""

from __future__ import annotations

import argparse
import array
import json
import math
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app" / "build" / "Jarvis.app"
HEADER = struct.Struct("<2sBBI")
SOURCES = {0: "system", 1: "mic"}
STATUS = 2
SAMPLE_RATE = 16_000


def receive_exactly(conn: socket.socket, count: int) -> bytes | None:
    chunks = []
    while count:
        chunk = conn.recv(count)
        if not chunk:
            return None
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_packets(conn: socket.socket, audio: dict[str, array.array], events: list[dict]) -> None:
    started = time.monotonic()
    while True:
        try:
            header = receive_exactly(conn, HEADER.size)
            if header is None:
                return
            magic, kind, _, length = HEADER.unpack(header)
            if magic != b"JE":
                print("bad packet header, stopping", flush=True)
                return
            payload = receive_exactly(conn, length) if length else b""
            if payload is None:
                return
        except OSError:
            return
        if kind == STATUS:
            event = json.loads(payload)
            event["at"] = round(time.monotonic() - started, 1)
            events.append(event)
            if event.get("event") != "level":
                print("status:", json.dumps(event), flush=True)
        elif kind in SOURCES:
            audio[SOURCES[kind]].frombytes(payload)


def describe(samples: array.array) -> dict:
    if not samples:
        return {"seconds": 0.0, "peak": 0.0, "rms": 0.0, "loudest_second": None}
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    loudest, loudest_rms = None, 0.0
    for start in range(0, len(samples), SAMPLE_RATE):
        window = samples[start : start + SAMPLE_RATE]
        window_rms = math.sqrt(sum(value * value for value in window) / len(window))
        if window_rms > loudest_rms:
            loudest, loudest_rms = start // SAMPLE_RATE, window_rms
    return {
        "seconds": round(len(samples) / SAMPLE_RATE, 1),
        "peak": round(peak, 4),
        "rms": round(rms, 5),
        "loudest_second": loudest,
    }


def write_wav(path: Path, samples: array.array) -> None:
    pcm = array.array("h", (int(max(-1.0, min(1.0, value)) * 32767) for value in samples))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--say", help="sentence for the Mac to speak during the capture")
    parser.add_argument("--say-after", type=float, default=12, help="seconds to wait before speaking")
    parser.add_argument("--wav-dir", type=Path, help="folder to save the two streams as WAV files")
    args = parser.parse_args()

    if not APP.exists():
        print(f"{APP} is missing. Run: bash app/build.sh")
        return 1

    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    subprocess.run(["open", "-n", str(APP), "--args", "--port", str(port)], check=True)
    server.settimeout(20)
    try:
        conn, _ = server.accept()
    except TimeoutError:
        print("The audio helper did not connect within 20 seconds.")
        return 1

    audio = {"system": array.array("f"), "mic": array.array("f")}
    events: list[dict] = []
    reader = threading.Thread(target=read_packets, args=(conn, audio, events), daemon=True)
    reader.start()

    started = time.monotonic()
    spoken = False
    while time.monotonic() - started < args.seconds and reader.is_alive():
        if args.say and not spoken and time.monotonic() - started >= args.say_after:
            subprocess.Popen(["say", args.say])
            spoken = True
        time.sleep(0.2)

    conn.close()  # the helper exits when the connection goes away
    reader.join(timeout=3)

    summary = {source: describe(samples) for source, samples in audio.items()}
    levels = [event for event in events if event.get("event") == "level"]
    for source in audio:
        peaks = [round(event["peak"], 3) for event in levels if event.get("source") == source]
        summary[source]["helper_peaks"] = peaks
    print(json.dumps(summary, indent=2))

    if args.wav_dir:
        args.wav_dir.mkdir(parents=True, exist_ok=True)
        for source, samples in audio.items():
            if samples:
                write_wav(args.wav_dir / f"{source}.wav", samples)
        print(f"WAV files in {args.wav_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
