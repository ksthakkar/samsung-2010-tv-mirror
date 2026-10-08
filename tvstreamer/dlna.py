"""DLNA media server that a 2010 Samsung can browse from AllShare.

The TV is the player. This PC only advertises a library and serves the bytes.
Pushing playback with SetAVTransportURI is attempted as a convenience, but
most 2010 sets never advertise a renderer, so AllShare is the real path.
"""

from __future__ import annotations

import html
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from tvstreamer.net import http_post, is_lan_client

LIVE_FEATURES = (
    "DLNA.ORG_PN=AVC_TS_HD_60_AC3_ISO;DLNA.ORG_OP=00;DLNA.ORG_CI=0;"
    "DLNA.ORG_FLAGS=01500000000000000000000000000000"
)
FILE_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
LIVE_PROTOCOL = f"http-get:*:video/mpeg:{LIVE_FEATURES}"
FAKE_LENGTH = "2147483647"
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
CDS_TYPE = "urn:schemas-upnp-org:service:ContentDirectory:1"
CMS_TYPE = "urn:schemas-upnp-org:service:ConnectionManager:1"
AVT_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"


@dataclass
class Item:
    id: str
    title: str
    url: str
    mime: str
    protocol_info: str
    size: int | None = None
    duration: str | None = None
    direct_file: Path | None = None


class Library:
    def __init__(self) -> None:
        self.items: list[Item] = []
        self.update_id = 1
        self.lock = threading.Lock()
        self.direct_file: Path | None = None

    def replace(self, items: list[Item], direct_file: Path | None = None) -> None:
        with self.lock:
            self.items = items
            self.direct_file = direct_file
            self.update_id = (self.update_id + 1) & 0xFFFFFFFF

    def snapshot(self) -> tuple[list[Item], int, Path | None]:
        with self.lock:
            return list(self.items), self.update_id, self.direct_file


def device_xml(name: str, device_uuid: str) -> bytes:
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>{DEVICE_TYPE}</deviceType>
    <friendlyName>{escape(name)}</friendlyName>
    <manufacturer>Laptop</manufacturer>
    <modelName>Laptop Screen</modelName>
    <modelNumber>1.0</modelNumber>
    <dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>
    <UDN>uuid:{device_uuid}</UDN>
    <serviceList>
      <service>
        <serviceType>{CDS_TYPE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <SCPDURL>/cds.xml</SCPDURL>
        <controlURL>/ctl/cds</controlURL>
        <eventSubURL>/evt/cds</eventSubURL>
      </service>
      <service>
        <serviceType>{CMS_TYPE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/cms.xml</SCPDURL>
        <controlURL>/ctl/cms</controlURL>
        <eventSubURL>/evt/cms</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""
    return xml.encode("utf-8")


CDS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
  <action><name>Browse</name><argumentList>
    <argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
    <argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
    <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
    <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
    <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
    <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
    <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
    <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
    <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
    <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
  </argumentList></action>
  <action><name>Search</name><argumentList>
    <argument><name>ContainerID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
    <argument><name>SearchCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SearchCriteria</relatedStateVariable></argument>
    <argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
    <argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
    <argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
    <argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
    <argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
    <argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
    <argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
    <argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
  </argumentList></action>
  <action><name>GetSearchCapabilities</name><argumentList>
    <argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>
  </argumentList></action>
  <action><name>GetSortCapabilities</name><argumentList>
    <argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument>
  </argumentList></action>
  <action><name>GetSystemUpdateID</name><argumentList>
    <argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>
  </argumentList></action>
</actionList>
<serviceStateTable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_SearchCriteria</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
</serviceStateTable>
</scpd>"""

CMS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
  <action><name>GetProtocolInfo</name><argumentList>
    <argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
    <argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
  </argumentList></action>
  <action><name>GetCurrentConnectionIDs</name><argumentList>
    <argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>
  </argumentList></action>
  <action><name>GetCurrentConnectionInfo</name><argumentList>
    <argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
    <argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
    <argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
    <argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
    <argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
    <argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
    <argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
    <argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
  </argumentList></action>
</actionList>
<serviceStateTable>
  <stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
  <stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
</serviceStateTable>
</scpd>"""


def didl(items: list[Item]) -> str:
    body = "".join(_item_xml(item) for item in items)
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f"{body}</DIDL-Lite>"
    )


def root_didl(count: int) -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<container id="0" parentID="-1" restricted="1" childCount="{count}">'
        "<dc:title>Laptop Screen</dc:title>"
        "<upnp:class>object.container.storageFolder</upnp:class>"
        "</container></DIDL-Lite>"
    )


def _item_xml(item: Item) -> str:
    extra = ""
    if item.size is not None:
        extra += f' size="{item.size}"'
    if item.duration:
        extra += f' duration="{escape(item.duration)}"'
    return (
        f'<item id="{escape(item.id)}" parentID="0" restricted="1">'
        f"<dc:title>{escape(item.title)}</dc:title>"
        "<upnp:class>object.item.videoItem</upnp:class>"
        f'<res protocolInfo="{escape(item.protocol_info)}"{extra}>{escape(item.url)}</res>'
        "</item>"
    )


def soap_ok(service: str, action: str, fields: dict[str, str]) -> bytes:
    inner = "".join(f"<{key}>{html.escape(value, quote=False)}</{key}>" for key, value in fields.items())
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action}Response xmlns:u="{service}">{inner}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    )
    return xml.encode("utf-8")


def soap_fault(code: int, description: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
        "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
        '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
        f"<errorCode>{code}</errorCode><errorDescription>{escape(description)}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
    )
    return xml.encode("utf-8")


def parse_soap(body: str) -> tuple[str, dict[str, str]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", {}
    known = {
        "Browse", "Search", "GetSearchCapabilities", "GetSortCapabilities", "GetSystemUpdateID",
        "GetProtocolInfo", "GetCurrentConnectionIDs", "GetCurrentConnectionInfo",
    }
    for element in root.iter():
        name = element.tag.split("}")[-1]
        if name in known:
            args = {child.tag.split("}")[-1]: (child.text or "") for child in list(element)}
            return name, args
    return "", {}


def browse_response(library: Library, args: dict[str, str]) -> tuple[int, bytes]:
    items, update_id, _file = library.snapshot()
    object_id = args.get("ObjectID", "0")
    flag = args.get("BrowseFlag", "BrowseDirectChildren")
    try:
        start = int(args.get("StartingIndex") or "0")
    except ValueError:
        start = 0
    try:
        count = int(args.get("RequestedCount") or "0")
    except ValueError:
        count = 0
    if object_id in {"0", "0/", ""} and flag == "BrowseMetadata":
        payload = root_didl(len(items))
        returned = 1
        total = 1
    elif object_id in {"0", "0/", ""}:
        selected = items[start:]
        if count:
            selected = selected[:count]
        payload = didl(selected)
        returned = len(selected)
        total = len(items)
    else:
        matched = [item for item in items if item.id == object_id]
        if not matched:
            return 500, soap_fault(701, "No such object")
        payload = didl(matched if flag == "BrowseMetadata" else [])
        returned = 1 if flag == "BrowseMetadata" else 0
        total = returned
    body = soap_ok(CDS_TYPE, "Browse", {
        "Result": payload,
        "NumberReturned": str(returned),
        "TotalMatches": str(total),
        "UpdateID": str(update_id),
    })
    return 200, body


def handle_cds(library: Library, action: str, args: dict[str, str]) -> tuple[int, bytes]:
    items, update_id, _file = library.snapshot()
    if action == "Browse":
        return browse_response(library, args)
    if action == "Search":
        body = soap_ok(CDS_TYPE, "Search", {
            "Result": didl([]),
            "NumberReturned": "0",
            "TotalMatches": "0",
            "UpdateID": str(update_id),
        })
        return 200, body
    if action == "GetSearchCapabilities":
        return 200, soap_ok(CDS_TYPE, action, {"SearchCaps": ""})
    if action == "GetSortCapabilities":
        return 200, soap_ok(CDS_TYPE, action, {"SortCaps": ""})
    if action == "GetSystemUpdateID":
        return 200, soap_ok(CDS_TYPE, action, {"Id": str(update_id)})
    return 500, soap_fault(401, "Invalid Action")


def handle_cms(action: str) -> tuple[int, bytes]:
    if action == "GetProtocolInfo":
        source = ",".join([
            LIVE_PROTOCOL,
            "http-get:*:video/mp4:*",
            "http-get:*:video/*:*",
        ])
        return 200, soap_ok(CMS_TYPE, action, {"Source": source, "Sink": ""})
    if action == "GetCurrentConnectionIDs":
        return 200, soap_ok(CMS_TYPE, action, {"ConnectionIDs": "0"})
    if action == "GetCurrentConnectionInfo":
        return 200, soap_ok(CMS_TYPE, action, {
            "RcsID": "-1",
            "AVTransportID": "-1",
            "ProtocolInfo": LIVE_PROTOCOL,
            "PeerConnectionManager": "",
            "PeerConnectionID": "-1",
            "Direction": "Output",
            "Status": "OK",
        })
    return 500, soap_fault(401, "Invalid Action")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TVStreamer/1.0"

    def log_message(self, fmt: str, *args) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"/stream.ts", "/media"}:
            return
        self.server.engine.log(f"{self.command} {path}")  # type: ignore[attr-defined]

    def do_HEAD(self) -> None:
        if self._refuse_remote():
            return
        self._head = True
        self.do_GET()

    def do_GET(self) -> None:
        if self.command == "GET" and self._refuse_remote():
            return
        path = self.path.split("?", 1)[0]
        engine = self.server.engine  # type: ignore[attr-defined]
        head = getattr(self, "_head", False) or self.command == "HEAD"
        if path in {"/", "/index.html"}:
            self._bytes(200, "text/html; charset=utf-8", _HOME)
            return
        if path == "/device.xml":
            self._bytes(200, 'text/xml; charset="utf-8"', device_xml(engine.friendly_name, engine.device_uuid))
            return
        if path == "/cds.xml":
            self._bytes(200, 'text/xml; charset="utf-8"', CDS_XML)
            return
        if path == "/cms.xml":
            self._bytes(200, 'text/xml; charset="utf-8"', CMS_XML)
            return
        if path == "/stream.ts":
            self._stream(engine, head)
            return
        if path == "/media":
            self._file(engine, head)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self._refuse_remote():
            return
        engine = self.server.engine  # type: ignore[attr-defined]
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self.send_error(400)
            return
        if length < 0 or length > 256 * 1024:
            self.send_error(413)
            return
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        action, args = parse_soap(raw)
        path = self.path.split("?", 1)[0]
        if path == "/ctl/cds":
            status, payload = handle_cds(engine.library, action, args)
        elif path == "/ctl/cms":
            status, payload = handle_cms(action)
        else:
            status, payload = 500, soap_fault(401, "Invalid Action")
        self._bytes(status, 'text/xml; charset="utf-8"', payload)

    def do_SUBSCRIBE(self) -> None:
        if self._refuse_remote():
            return
        self.send_response(200)
        self.send_header("SID", f"uuid:{uuid.uuid4()}")
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _refuse_remote(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        if is_lan_client(host):
            return False
        self.send_error(403)
        return True

    def _bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _stream(self, engine, head: bool) -> None:
        pump = engine.pump
        if head:
            self._live_headers()
            return
        if not pump.running:
            self.send_error(503, "Nothing is sharing right now")
            return
        try:
            subscriber = pump.subscribe()
        except RuntimeError:
            self.send_error(503, "The stream is already open")
            return
        try:
            first = _first_chunk(subscriber, pump)
            if first is None:
                self.send_error(503, "The picture is not ready yet")
                return
            self._live_headers()
            pending = bytearray(first)
            engine.log("The TV connected to the picture.")
            if pump.live:
                try:
                    self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            batch = 188 * 7 if pump.live else 256 * 1024
            pause = 0.2 if pump.live else 0.3
            while True:
                try:
                    chunk = subscriber.get(timeout=pause)
                except Exception:
                    chunk = b""
                    if not pump.running and subscriber.empty():
                        break
                if chunk:
                    pending.extend(chunk)
                if pump.live or len(pending) >= batch or (pending and not chunk):
                    if pending:
                        self.wfile.write(pending)
                        self.wfile.flush()
                        pending.clear()
            if pending:
                self.wfile.write(pending)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError:
            pass
        finally:
            pump.unsubscribe(subscriber)

    def _live_headers(self) -> None:
        # Old Samsung players buffer until they see a file length. A live stream
        # has none, so this reports a large file and then just keeps sending.
        ranged = self.headers.get("Range")
        if ranged:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-{int(FAKE_LENGTH) - 1}/{FAKE_LENGTH}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("Content-Length", FAKE_LENGTH)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", LIVE_FEATURES)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def _file(self, engine, head: bool) -> None:
        _items, _update, path = engine.library.snapshot()
        if path is None or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].split(",", 1)[0].strip()
            left, _, right = spec.partition("-")
            try:
                if left == "":
                    start = max(0, size - int(right or "0"))
                else:
                    start = int(left)
                    end = int(right) if right else size - 1
            except ValueError:
                self.send_error(400)
                return
            end = min(end, size - 1)
            if start < 0 or start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        mime = engine.file_mime or "video/mp4"
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        mode = self.headers.get("transferMode.dlna.org") or "Interactive"
        if mode not in {"Streaming", "Interactive", "Background"}:
            mode = "Interactive"
        self.send_header("transferMode.dlna.org", mode)
        self.send_header("contentFeatures.dlna.org", FILE_FEATURES)
        self.end_headers()
        if head:
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                blob = handle.read(min(64 * 1024, remaining))
                if not blob:
                    break
                try:
                    self.wfile.write(blob)
                except OSError:
                    break
                remaining -= len(blob)


def _first_chunk(subscriber, pump) -> bytes | None:
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            return subscriber.get(timeout=0.4)
        except Exception:
            if not pump.running:
                return None
    return None


_HOME = b"""<!doctype html><meta charset="utf-8"><title>Laptop Screen</title>
<body style="font-family:Segoe UI,sans-serif;background:#16181d;color:#eee;padding:32px">
<h1>Laptop Screen is running</h1>
<p>On the Samsung, open Source and choose Laptop Screen.</p>
</body>"""


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, engine) -> None:
        self.engine = engine
        super().__init__(address, Handler)


class Advertiser:
    """Tell the TV this PC is a media server."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listen: socket.socket | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self._listen_search, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._bye()
        if self._listen is not None:
            try:
                self._listen.close()
            except OSError:
                pass

    def announce(self) -> None:
        self._notify("ssdp:alive")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._notify("ssdp:alive")
            except OSError as exc:
                self.engine.log(f"Could not announce on the network: {exc}")
            self._stop.wait(15)

    def _bye(self) -> None:
        try:
            self._notify("ssdp:byebye")
        except OSError:
            pass

    def _notify(self, nts: str) -> None:
        location = self.engine.location
        ip = self.engine.ip
        targets = [
            "upnp:rootdevice",
            f"uuid:{self.engine.device_uuid}",
            DEVICE_TYPE,
            CDS_TYPE,
            CMS_TYPE,
        ]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            for target in targets:
                usn = target if target.startswith("uuid:") else f"uuid:{self.engine.device_uuid}::{target}"
                packet = (
                    "NOTIFY * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    "NTS: " + nts + "\r\n"
                    f"NT: {target}\r\n"
                    f"USN: {usn}\r\n"
                    f"LOCATION: {location}\r\n"
                    "SERVER: LaptopScreen/1.0 UPnP/1.0 TVStreamer/1.0\r\n\r\n"
                )
                sock.sendto(packet.encode("utf-8"), ("239.255.255.250", 1900))
        finally:
            sock.close()

    def _listen_search(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 1900))
            group = struct.pack("4s4s", socket.inet_aton("239.255.255.250"), socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group)
            sock.settimeout(1.0)
        except OSError as exc:
            self.engine.log(f"Another program already owns discovery port 1900 ({exc}). The TV can still find this laptop from announcements.")
            sock.close()
            return
        self._listen = sock
        self.engine.log("Listening for the TV's network search.")
        while not self._stop.is_set():
            try:
                data, address = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", "ignore")
            if "M-SEARCH" not in text.upper():
                continue
            target = "ssdp:all"
            for line in text.split("\r\n"):
                if line.lower().startswith("st:"):
                    target = line.split(":", 1)[1].strip()
            self._reply(address, target)

    def _reply(self, address, target: str) -> None:
        ours = {"ssdp:all", "upnp:rootdevice", DEVICE_TYPE, CDS_TYPE, CMS_TYPE, f"uuid:{self.engine.device_uuid}"}
        if target not in ours and target != "ssdp:all":
            return
        targets = [target] if target != "ssdp:all" else ["upnp:rootdevice", DEVICE_TYPE, CDS_TYPE]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            try:
                sock.bind((self.engine.ip, 0))
            except OSError:
                pass
            for item in targets:
                usn = item if item.startswith("uuid:") else f"uuid:{self.engine.device_uuid}::{item}"
                packet = (
                    "HTTP/1.1 200 OK\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    "EXT:\r\n"
                    f"LOCATION: {self.engine.location}\r\n"
                    "SERVER: LaptopScreen/1.0 UPnP/1.0 TVStreamer/1.0\r\n"
                    f"ST: {item}\r\n"
                    f"USN: {usn}\r\n\r\n"
                )
                sock.sendto(packet.encode("utf-8"), address)
        finally:
            sock.close()


def discover_renderers(timeout: float = 3.0) -> list[dict]:
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {AVT_TYPE}\r\n\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(0.6)
    found: dict[str, dict] = {}
    try:
        sock.sendto(request.encode("utf-8"), ("239.255.255.250", 1900))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, address = sock.recvfrom(4096)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "ignore")
            location = ""
            for line in text.split("\r\n"):
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
            if not location or location in found:
                continue
            control = _avtransport_control(location)
            if control:
                found[location] = {"ip": address[0], "location": location, "control": control, "name": _friendly_name(location)}
    finally:
        sock.close()
    return list(found.values())


def _friendly_name(location: str) -> str:
    try:
        with urllib_request(location) as response:
            root = ET.fromstring(response.read())
    except Exception:
        return location
    for element in root.iter():
        if element.tag.split("}")[-1] == "friendlyName" and element.text:
            return element.text.strip()
    return location


def _avtransport_control(location: str) -> str | None:
    try:
        with urllib_request(location) as response:
            root = ET.fromstring(response.read())
    except Exception:
        return None
    base = location.split("/")[0] + "//" + location.split("/")[2]
    for service in root.iter():
        if service.tag.split("}")[-1] != "service":
            continue
        fields = {child.tag.split("}")[-1]: (child.text or "") for child in list(service)}
        if "AVTransport" not in fields.get("serviceType", ""):
            continue
        control = fields.get("controlURL", "")
        if control.startswith("http"):
            return control
        if not control.startswith("/"):
            control = "/" + control
        return base + control
    return None


def urllib_request(url: str):
    import urllib.request
    return urllib.request.urlopen(url, timeout=3)


def push_play(control_url: str, stream_url: str, title: str, protocol_info: str) -> str:
    meta = didl([Item("1", title, stream_url, "video/mpeg", protocol_info)])
    set_body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:SetAVTransportURI xmlns:u="{AVT_TYPE}">
<InstanceID>0</InstanceID>
<CurrentURI>{html.escape(stream_url)}</CurrentURI>
<CurrentURIMetaData>{html.escape(meta)}</CurrentURIMetaData>
</u:SetAVTransportURI>
</s:Body></s:Envelope>"""
    status, text = http_post(control_url, set_body, {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{AVT_TYPE}#SetAVTransportURI"',
    })
    if status != 200:
        return f"The TV refused the address ({status}). {text[:300]}"
    play_body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:Play xmlns:u="{AVT_TYPE}"><InstanceID>0</InstanceID><Speed>1</Speed></u:Play>
</s:Body></s:Envelope>"""
    status, text = http_post(control_url, play_body, {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{AVT_TYPE}#Play"',
    })
    if status != 200:
        return f"The TV loaded the address but did not start ({status}). {text[:300]}"
    return ""
