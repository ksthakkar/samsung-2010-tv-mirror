"""Ties discovery, encoding, and the AllShare library together."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from tvstreamer.audio import LoopbackCapture
from tvstreamer.dlna import (
    FILE_FEATURES,
    LIVE_PROTOCOL,
    Advertiser,
    Item,
    Library,
    Server,
    discover_renderers,
    push_play,
)
from tvstreamer.ffmpeg_util import (
    MIME_BY_SUFFIX,
    app_dir,
    choose_encoders,
    desktop_attempts,
    encoder_label,
    ensure_ffmpeg,
    file_command,
    find_ffprobe,
    media_facts,
    web_command,
    working_encoders,
)
from tvstreamer.net import ipv4_addresses, is_http_url, is_private
from tvstreamer.pump import StreamPump


class Engine:
    def __init__(self, log) -> None:
        self.log = log
        self.friendly_name = "Laptop Screen"
        self.device_uuid = _load_uuid()
        self.ip = ""
        self.port = 0
        self.ffmpeg: Path | None = None
        self.ffprobe: Path | None = None
        self.encoders: list[str] = []
        self.audio_devices: list[dict] = []
        self.library = Library()
        self.pump = StreamPump(log)
        self.audio = LoopbackCapture(log)
        self.advertiser: Advertiser | None = None
        self.httpd: Server | None = None
        self.file_mime = "video/mp4"
        self.sharing = False
        self.share_title = ""
        self.shared_url = ""
        self.share_protocol = LIVE_PROTOCOL
        self._lock = threading.Lock()

    @property
    def location(self) -> str:
        return f"http://{self.ip}:{self.port}/device.xml"

    @property
    def stream_url(self) -> str:
        return f"http://{self.ip}:{self.port}/stream.ts"

    def bootstrap(self) -> None:
        addresses = ipv4_addresses()
        if not addresses:
            raise RuntimeError("This PC does not have a network address. Connect to Wi-Fi first.")
        self.ip = addresses[0]
        self.ffmpeg = ensure_ffmpeg(self.log)
        self.ffprobe = find_ffprobe(self.ffmpeg)
        self.encoders = working_encoders(self.ffmpeg, self.log)
        if not self.encoders:
            raise RuntimeError("FFmpeg cannot encode H.264 on this PC.")
        names = ", ".join(encoder_label(name) for name in self.encoders)
        self.log(f"Encoders ready: {names}.")
        try:
            self.audio_devices = self.audio.list_devices()
        except Exception as exc:
            self.audio_devices = []
            self.log(f"System audio is unavailable: {exc}")
        self._serve()
        self.advertiser = Advertiser(self)
        self.advertiser.start()
        self.log(f"Source name: {self.friendly_name}")
        self.log(f"Address: http://{self.ip}:{self.port}/")
        self.log("Only devices on this home network can connect. Stop the app when you are done.")

    def set_ip(self, ip: str) -> None:
        if ip == self.ip:
            return
        if not is_private(ip):
            self.log("That address is not on the home network.")
            return
        self.ip = ip
        self.log(f"Using {ip}. On the TV, open Source and choose Laptop Screen again.")
        if self.advertiser:
            self.advertiser.announce()

    def share_desktop(self, width: int, height: int, encoder: str, audio: bool, device_index: int | str | None) -> None:
        with self._lock:
            self._stop_locked()
            bitrate = 12000 if height >= 1080 else 8000
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                self.log("This Linux session is not X11. Desktop mirror needs a session that still offers X11.")
            use_audio = audio
            if use_audio:
                try:
                    self.audio.start(device_index)
                except Exception as exc:
                    use_audio = False
                    self.log(f"System audio did not start ({exc}). The picture will be silent.")
            commands = []
            for name in choose_encoders(encoder, self.encoders):
                if use_audio:
                    commands.extend(desktop_attempts(
                        self.ffmpeg, name, width, height, 30, bitrate,
                        self.audio.rate, self.audio.channels, True, self.audio.sample_format,
                        native_input=self.audio.native_input,
                        mac_audio=self.audio.mac_audio,
                    ))
                commands.extend(desktop_attempts(
                    self.ffmpeg, name, width, height, 30, bitrate, 48000, 2, False,
                ))
            try:
                self.pump.start(commands, live=True, feed_audio=self._feed if use_audio else None, ready_timeout=3.0)
            except Exception:
                self.audio.stop()
                raise
            if not _sharing_audio(self.pump.proc):
                self.audio.stop()
                self.log("Mirroring the screen without system audio.")
            else:
                self.log("Mirroring the screen with clear stereo at AC-3 640 kbps. Picture and sound stay together.")
            self._publish_stream("Desktop mirror")
            label = encoder_label(_running_encoder(self.pump.proc))
            self.log(f"{width}x{height} at 30 fps, {label}, about {bitrate / 1000:.1f} Mbps.")
            self.log("On the TV: open Source, choose Laptop Screen, then Desktop mirror.")

    def share_file(self, path: Path, convert: bool, encoder: str) -> None:
        with self._lock:
            self._stop_locked()
            if not path.is_file():
                raise RuntimeError("That file is not there.")
            path = path.resolve()
            has_audio, duration = media_facts(self.ffprobe, path)
            if convert:
                commands = [
                    file_command(self.ffmpeg, name, path, has_audio)
                    for name in choose_encoders(encoder, self.encoders)
                ]
                self.pump.start(commands, live=False, feed_audio=None, wait_for_output=True, ready_timeout=40, ready_bytes=4_000_000)
                self._publish_stream(path.name)
                self.log(f"Converting {path.name} ahead of the TV. The remote can pause, but seeking will not work.")
            else:
                mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")
                self.file_mime = mime
                url = f"http://{self.ip}:{self.port}/media"
                protocol = f"http-get:*:{mime}:{FILE_FEATURES}"
                item = Item(
                    id="movie",
                    title=path.name,
                    url=url,
                    mime=mime,
                    protocol_info=protocol,
                    size=path.stat().st_size,
                    duration=duration,
                    direct_file=path,
                )
                self.library.replace([item], direct_file=path)
                self.share_title = path.name
                self.shared_url = url
                self.share_protocol = protocol
                self.sharing = True
                if self.advertiser:
                    self.advertiser.announce()
                self.log(f"Sharing {path.name} unchanged. The TV remote can pause and seek.")
            self.log("On the TV: open Source, choose Laptop Screen, then the file name.")

    def share_url(self, page_url: str, encoder: str) -> None:
        with self._lock:
            self._stop_locked()
            title, video_url, audio_url, headers = _resolve_page(page_url)
            self.log(f"Found \"{title}\". Converting 1080p ahead of the TV. Nothing is saved.")
            commands = []
            for name in choose_encoders(encoder, self.encoders):
                accel = [True, False] if name == "h264_qsv" else [False]
                for use_gpu in accel:
                    if audio_url:
                        commands.append(web_command(
                            self.ffmpeg, name, video_url, audio_url, headers, hwaccel=use_gpu,
                        ))
                    else:
                        commands.append(web_command(
                            self.ffmpeg, name, video_url, None, headers, embedded_audio=True, hwaccel=use_gpu,
                        ))
                        commands.append(web_command(
                            self.ffmpeg, name, video_url, None, headers, embedded_audio=False, hwaccel=use_gpu,
                        ))
            self.pump.start(
                commands, live=False, feed_audio=None,
                wait_for_output=True, ready_timeout=45, ready_bytes=6_000_000,
            )
            self._publish_stream(title)
            self.log("The first stretch is ready. Conversion keeps running while the TV plays.")
            self.log("On the TV: open Source, choose Laptop Screen. Seeking a web video will not work.")

    def stop_share(self) -> None:
        with self._lock:
            was_sharing = self.sharing or self.pump.running
            self._stop_locked()
            if was_sharing:
                self.log("Stopped.")

    def push_to_tv(self) -> None:
        if not self.sharing:
            self.log("Start sharing first, then choose Laptop Screen in the TV's Source list.")
            return
        self.log("Looking for a TV that accepts a remote start...")
        renderers = discover_renderers()
        if not renderers:
            self.log("No TV accepted a remote start. That is normal for a 2010 Samsung.")
            self.log("On the TV, open Source and choose Laptop Screen.")
            return
        for renderer in renderers:
            if not _lan_control(renderer["control"]):
                continue
            self.log(f"Found {renderer['name']} at {renderer['ip']}.")
            error = push_play(renderer["control"], self.shared_url, self.share_title, self.share_protocol)
            if not error:
                self.log(f"Asked {renderer['name']} to play.")
                return
            self.log(error)
        self.log("The TV was found, but it did not start. Open Source and choose Laptop Screen.")

    def shutdown(self) -> None:
        self.stop_share()
        if self.advertiser:
            self.advertiser.stop()
        httpd = self.httpd
        self.httpd = None
        if httpd is not None:
            threading.Thread(target=_close_http, args=(httpd,), daemon=True).start()

    def _serve(self) -> None:
        last = None
        for port in range(8192, 8210):
            try:
                self.httpd = Server(("0.0.0.0", port), self)
            except OSError as exc:
                last = exc
                continue
            self.port = port
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            return
        raise RuntimeError(f"Could not open a port for the TV ({last}).")

    def _publish_stream(self, title: str) -> None:
        item = Item("desktop" if title == "Desktop mirror" else "video", title, self.stream_url, "video/mpeg", LIVE_PROTOCOL)
        self.library.replace([item])
        self.share_title = title
        self.shared_url = self.stream_url
        self.share_protocol = LIVE_PROTOCOL
        self.sharing = True
        if self.advertiser:
            self.advertiser.announce()

    def _stop_locked(self) -> None:
        self.pump.stop()
        self.audio.stop()
        self.library.replace([])
        self.sharing = False

    def _feed(self, proc, stop: threading.Event) -> None:
        """Hand system audio to FFmpeg in the order it was captured.

        Gaps get filled in by the TV, and that smear is what sounds dull.
        Samples are only dropped when more than a second has piled up during
        a stall, so a short hiccup does not punch a hole in the sound.
        """
        subscriber = self.audio.subscribe()
        sample_bytes = 4 if self.audio.sample_format == "f32le" else 2
        frame = max(self.audio.channels, 1) * sample_bytes
        rate_bytes = max(self.audio.rate, 1) * frame
        batch = max(rate_bytes // 50, frame)
        batch -= batch % frame
        ceiling = max(rate_bytes // 4, batch)
        pending = bytearray()
        next_slot = time.perf_counter()
        try:
            while not stop.is_set() and proc.poll() is None:
                try:
                    pending.extend(subscriber.get(timeout=0.02))
                except queue.Empty:
                    pass
                while True:
                    try:
                        pending.extend(subscriber.get_nowait())
                    except queue.Empty:
                        break
                # A headset often delivers a clump. Sending that clump at once
                # stacks timestamps, and the TV smears them. Keep the newest
                # slice and hand it over one small piece at a time.
                if len(pending) > ceiling:
                    pending = _tail(pending, frame, batch * 2)
                now = time.perf_counter()
                if now < next_slot:
                    time.sleep(next_slot - now)
                    continue
                if len(pending) < frame:
                    next_slot = time.perf_counter() + 0.01
                    continue
                size = min(len(pending) - (len(pending) % frame), batch)
                try:
                    proc.stdin.write(pending[:size])
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
                del pending[:size]
                next_slot = time.perf_counter() + (size / rate_bytes)
        finally:
            self.audio.unsubscribe(subscriber)
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass


def _tail(pending: bytearray, frame: int, keep: int) -> bytearray:
    if len(pending) > keep:
        pending = pending[-keep:]
    extra = len(pending) % frame
    if extra:
        del pending[:extra]
    return pending


def _close_http(httpd: Server) -> None:
    try:
        httpd.shutdown()
    except Exception:
        pass
    try:
        httpd.server_close()
    except Exception:
        pass


def _sharing_audio(proc) -> bool:
    args = list(proc.args) if proc is not None and not isinstance(proc.args, str) else []
    if "pipe:0" in args:
        return True
    for index, arg in enumerate(args[:-1]):
        if arg == "-f" and args[index + 1] in {"pulse", "pipewire"}:
            return True
        if arg == "-i" and ":" in str(args[index + 1]) and not str(args[index + 1]).endswith(":none"):
            if "avfoundation" in args:
                return True
    return False


def _lan_control(url: str) -> bool:
    from urllib.parse import urlsplit
    if not is_http_url(url):
        return False
    return is_private(urlsplit(url).hostname or "")


def _running_encoder(proc) -> str:
    args = list(proc.args) if proc is not None else []
    for name in ("h264_qsv", "h264_nvenc", "h264_amf", "h264_videotoolbox", "libx264"):
        if name in args:
            return name
    return "libx264"


def _resolve_page(page_url: str) -> tuple[str, str, str | None, dict]:
    page_url = _require_http(page_url)
    if _is_direct_media(page_url):
        name = page_url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "Web video"
        return name, page_url, None, _browser_headers(page_url)
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed. Run: pip install yt-dlp") from exc
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bv*[height<=1080]+ba/b",
        "skip_download": True,
        "socket_timeout": 20,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(page_url, download=False)
    except Exception as exc:
        raise RuntimeError(
            "That page did not expose a video file. Paste a direct .mp4 or .m3u8 address, "
            "or mirror the desktop while the video plays in the browser."
        ) from exc
    if info is None or not info.get("url") and not info.get("requested_formats"):
        raise RuntimeError("That link did not return a video.")
    title = info.get("title") or "Web video"
    formats = info.get("requested_formats") or [info]
    video = formats[0]
    audio = formats[1] if len(formats) > 1 else None
    headers = dict(video.get("http_headers") or {}) or _browser_headers(page_url)
    audio_url = audio.get("url") if audio else None
    if audio and not video.get("http_headers"):
        headers = dict(audio.get("http_headers") or headers)
    if not video.get("url"):
        raise RuntimeError("That page did not expose a video file.")
    video_url = _require_http(video["url"])
    if audio_url:
        audio_url = _require_http(audio_url)
    return title, video_url, audio_url, _clean_headers(headers)


def _require_http(url: str) -> str:
    if not is_http_url(url):
        raise RuntimeError("Only http and https links are allowed.")
    return url.strip()


def _clean_headers(headers: dict) -> dict[str, str]:
    kept = {}
    for key in ("User-Agent", "Referer"):
        value = headers.get(key)
        if isinstance(value, str) and value.isprintable() and "\r" not in value and "\n" not in value:
            kept[key] = value[:300]
    return kept


def _is_direct_media(url: str) -> bool:
    if not is_http_url(url):
        return False
    path = url.split("?", 1)[0].lower()
    return path.endswith((
        ".mp4", ".m4v", ".mkv", ".webm", ".mov", ".avi", ".ts", ".m3u8", ".m3u", ".mpd",
    ))


def _browser_headers(url: str) -> dict[str, str]:
    origin = url.split("/", 3)
    referer = url if len(origin) < 3 else origin[0] + "//" + origin[2] + "/"
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Referer": referer,
    }


def _load_uuid() -> str:
    path = app_dir() / "config.json"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("uuid"):
                return str(data["uuid"])
        except (OSError, json.JSONDecodeError):
            pass
    import uuid
    value = str(uuid.uuid4())
    try:
        path.write_text(json.dumps({"uuid": value}), encoding="utf-8")
    except OSError:
        pass
    return value
