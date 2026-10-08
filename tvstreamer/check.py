"""Run the share pipeline without the window."""

from __future__ import annotations

import queue
import sys
import time
import urllib.request

from tvstreamer.dlna import CDS_TYPE
from tvstreamer.engine import Engine
from tvstreamer.net import http_post


def run_check() -> int:
    def log(message: str) -> None:
        print(message, flush=True)

    engine = Engine(log)
    engine.bootstrap()
    if not engine.encoders:
        log("No usable H.264 encoder")
        return 1

    log("Checking system audio...")
    hear_audio = True
    try:
        engine.audio.start(None)
    except Exception as exc:
        hear_audio = False
        log(f"System audio is not available ({exc}). The picture check will be silent.")
    else:
        if engine.audio.pipes:
            subscriber = engine.audio.subscribe()
            captured = 0
            deadline = time.time() + 2.5
            while time.time() < deadline and captured < 8000:
                try:
                    captured += len(subscriber.get(timeout=0.4))
                except queue.Empty:
                    pass
            engine.audio.unsubscribe(subscriber)
            engine.audio.stop()
            log(f"Captured {captured} audio bytes")
            if captured < 8000:
                log("System audio capture failed")
                return 1
        else:
            log(f"System audio opened: {engine.audio.device_name}")
            engine.audio.stop()

    log("Starting a desktop sample...")
    engine.share_desktop(1280, 720, "auto", audio=hear_audio, device_index=None)
    base = f"http://127.0.0.1:{engine.port}"
    description = urllib.request.urlopen(base + "/device.xml", timeout=5).read()
    if b"MediaServer" not in description or b"Laptop Screen" not in description:
        log("Device description is missing the media server")
        return 1

    browse = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Browse xmlns:u="{CDS_TYPE}">
<ObjectID>0</ObjectID>
<BrowseFlag>BrowseDirectChildren</BrowseFlag>
<Filter>*</Filter>
<StartingIndex>0</StartingIndex>
<RequestedCount>0</RequestedCount>
<SortCriteria></SortCriteria>
</u:Browse>
</s:Body></s:Envelope>"""
    status, body = http_post(
        base + "/ctl/cds",
        browse,
        {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{CDS_TYPE}#Browse"'},
    )
    if status != 200 or "Desktop mirror" not in body:
        log(f"Browse failed: {status} {body[:400]}")
        return 1
    log("AllShare library lists Desktop mirror")

    request = urllib.request.Request(base + "/stream.ts", headers={"Range": "bytes=0-"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status not in (200, 206):
            log(f"Unexpected stream status {response.status}")
            return 1
        data = response.read(200_000)
    if b"\x47" not in data[:188]:
        log("Stream is not MPEG-TS")
        return 1
    log(f"Received {len(data)} bytes of MPEG-TS")
    engine.stop_share()
    engine.shutdown()
    log("OK")
    return 0


if __name__ == "__main__":
    sys.exit(run_check())
