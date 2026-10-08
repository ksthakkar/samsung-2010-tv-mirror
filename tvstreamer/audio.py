"""System-audio capture.

Windows loopback is captured here and handed to FFmpeg on stdin, because the
usual Windows FFmpeg build has no WASAPI device. macOS and Linux let FFmpeg
open the speakers itself, so this class only remembers which device to use.

WASAPI only delivers loopback audio while some program has the speakers open.
A silent playback stream keeps that endpoint awake, including during quiet
scenes.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from typing import Callable


Log = Callable[[str], None]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LoopbackCapture:
    def __init__(self, log: Log) -> None:
        self.log = log
        self.rate = 48000
        self.channels = 2
        self.sample_format = "s16le"
        self.device_name = ""
        self.pipes = False
        self.native_input: list[str] = []
        self.mac_audio = "none"
        self._pa = None
        self._out = None
        self._inp = None
        self._subs: list[queue.Queue[bytes]] = []
        self._lock = threading.Lock()
        self.running = False

    def list_devices(self) -> list[dict]:
        if sys.platform == "darwin":
            return _mac_devices()
        if sys.platform != "win32":
            return _linux_devices()
        pyaudio = _pyaudio()
        pa = pyaudio.PyAudio()
        try:
            try:
                default = pa.get_default_wasapi_loopback()
                default_index = default["index"]
            except Exception:
                default_index = None
            devices = []
            for info in pa.get_loopback_device_info_generator():
                name = str(info["name"]).replace(" [Loopback]", "")
                devices.append(
                    {
                        "index": int(info["index"]),
                        "name": name,
                        "default": info["index"] == default_index,
                    }
                )
            return devices
        finally:
            pa.terminate()

    def start(self, device_index: int | str | None = None) -> None:
        self.stop()
        self.native_input = []
        self.mac_audio = "none"
        self.pipes = False
        if sys.platform == "darwin":
            self._start_mac(device_index)
            return
        if sys.platform != "win32":
            self._start_linux(device_index)
            return
        self._start_windows(device_index)

    def _start_windows(self, device_index: int | str | None) -> None:
        pyaudio = _pyaudio()
        pa = pyaudio.PyAudio()
        self._pa = pa
        if device_index is None:
            loop = pa.get_default_wasapi_loopback()
        else:
            loop = pa.get_device_info_by_index(int(device_index))
        speaker = _speaker_for(pa, loop)
        self.rate = int(loop["defaultSampleRate"])
        self.channels = int(loop["maxInputChannels"] or 2)
        self.device_name = str(loop["name"]).replace(" [Loopback]", "")
        out_channels = max(1, int(speaker["maxOutputChannels"] or 2))
        out_rate = int(speaker["defaultSampleRate"])
        frame = out_channels * 2

        def out_cb(_data, count, _info, _status):
            return (b"\x00" * (count * frame), pyaudio.paContinue)

        def in_cb(data, _count, _info, _status):
            self._publish(bytes(data))
            return (None, pyaudio.paContinue)

        self._out = pa.open(
            format=pyaudio.paInt16,
            channels=out_channels,
            rate=out_rate,
            output=True,
            output_device_index=int(speaker["index"]),
            frames_per_buffer=512,
            stream_callback=out_cb,
        )
        self._inp = None
        last_error: Exception | None = None
        for audio_format, sample_format in ((pyaudio.paFloat32, "f32le"), (pyaudio.paInt16, "s16le")):
            try:
                self._inp = pa.open(
                    format=audio_format,
                    channels=self.channels,
                    rate=self.rate,
                    input=True,
                    input_device_index=int(loop["index"]),
                    frames_per_buffer=512,
                    stream_callback=in_cb,
                )
                self.sample_format = sample_format
                break
            except Exception as exc:
                last_error = exc
                self._inp = None
        if self._inp is None:
            self.stop()
            raise RuntimeError(f"Could not open system audio ({last_error})")
        self.running = True
        self.pipes = True
        bits = "32-bit" if self.sample_format == "f32le" else "16-bit"
        self.log(f"System audio: {self.device_name}, {self.rate} Hz {bits}")
        if _is_headset(self.device_name):
            self.log(
                "That device is a headset, so Windows has already processed the sound. "
                "For the clearest result on the TV, choose Speakers in the audio list "
                "when that is where the video is playing."
            )

    def subscribe(self) -> queue.Queue[bytes]:
        subscriber: queue.Queue[bytes] = queue.Queue(maxsize=128)
        with self._lock:
            self._subs.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[bytes]) -> None:
        with self._lock:
            if subscriber in self._subs:
                self._subs.remove(subscriber)

    def stop(self) -> None:
        self.running = False
        for stream in (self._inp, self._out):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self._inp = None
        self._out = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        with self._lock:
            self._subs.clear()

    def _publish(self, data: bytes) -> None:
        with self._lock:
            subscribers = list(self._subs)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(data)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                    subscriber.put_nowait(data)
                except queue.Empty:
                    pass
                except queue.Full:
                    pass

    def _start_linux(self, device_index: int | str | None) -> None:
        devices = _linux_devices()
        chosen = _pick(devices, device_index)
        if chosen is None:
            raise RuntimeError(
                "No speaker loopback was found. Install PipeWire or PulseAudio, "
                "or mirror the desktop with No audio."
            )
        self.device_name = str(chosen["name"])
        self.native_input = ["-thread_queue_size", "1024", "-f", "pulse", "-i", str(chosen["index"])]
        self.running = True
        self.log(f"System audio: {self.device_name}")

    def _start_mac(self, device_index: int | str | None) -> None:
        devices = _mac_devices()
        chosen = _pick(devices, device_index)
        if chosen is None:
            raise RuntimeError(
                "macOS does not hand over system audio on its own. Install a loopback "
                "device such as BlackHole, send the video's sound there, and choose it "
                "in the audio list. The picture can still be mirrored with No audio."
            )
        self.device_name = str(chosen["name"])
        self.mac_audio = str(chosen["index"])
        self.running = True
        self.log(f"System audio: {self.device_name}")


def _pyaudio():
    import pyaudiowpatch as pyaudio

    return pyaudio


def _pick(devices: list[dict], device_index: int | str | None) -> dict | None:
    if not devices:
        return None
    if device_index is None:
        return next((item for item in devices if item.get("default")), devices[0])
    for item in devices:
        if str(item["index"]) == str(device_index):
            return item
    return None


def _linux_devices() -> list[dict]:
    try:
        sources = subprocess.run(
            ["pactl", "list", "short", "sources"],
            capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW,
        )
        sink = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [{"index": "@DEFAULT_MONITOR@", "name": "System audio", "default": True}]
    if sources.returncode != 0:
        return [{"index": "@DEFAULT_MONITOR@", "name": "System audio", "default": True}]
    default_name = sink.stdout.strip() + ".monitor"
    devices = []
    for line in sources.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[1].endswith(".monitor"):
            continue
        name = parts[1]
        label = name.removesuffix(".monitor").split(".")[-1].replace("-", " ")
        devices.append({"index": name, "name": label, "default": name == default_name})
    if devices and not any(item["default"] for item in devices):
        devices[0]["default"] = True
    return devices or [{"index": "@DEFAULT_MONITOR@", "name": "System audio", "default": True}]


def _mac_devices() -> list[dict]:
    from tvstreamer.ffmpeg_util import avfoundation_devices, find_ffmpeg

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return []
    _videos, audios = avfoundation_devices(ffmpeg)
    devices = []
    for index, name in audios:
        if not _is_loopback_name(name):
            continue
        devices.append({"index": index, "name": name, "default": not devices})
    return devices


def _is_loopback_name(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in ("blackhole", "soundflower", "loopback", "wavtap", "background music"))


def _is_headset(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in ("headphone", "headset", "bluetooth", "airpod", "buds", "beats"))


def _speaker_for(pa, loop: dict) -> dict:
    loop_name = str(loop["name"])
    for info in pa.get_device_info_generator():
        if info.get("isLoopbackDevice"):
            continue
        if int(info.get("maxOutputChannels") or 0) < 1:
            continue
        if str(info["name"]) and str(info["name"]) in loop_name:
            return info
    return pa.get_default_wasapi_device(d_out=True)
