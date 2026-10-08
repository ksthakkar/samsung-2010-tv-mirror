"""One FFmpeg process, fanned out to whoever is watching."""

from __future__ import annotations

import collections
import queue
import subprocess
import threading
import time
from typing import Callable

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHUNK = 188 * 64
VOD_CAP = 32 * 1024 * 1024


def _input_rejected(text: str) -> bool:
    lower = text.lower()
    return (
        "server returned 4" in lower
        or "http error 404" in lower
        or "not found/removed" in lower
        or "invalid data found when processing input" in lower
    )


def _friendly_error(text: str) -> str:
    lower = text.lower()
    if "404" in lower or "not found" in lower:
        return "The link opened, but the video file is missing. The server said it was removed."
    if (
        "not authorized" in lower
        or "screen recording" in lower
        or "cannot open display" in lower
        or "operation not permitted" in lower
    ):
        return (
            "The screen could not be opened. On a Mac, allow Screen Recording for Terminal "
            "or Python in System Settings, then open the app again. On Linux, desktop mirror "
            "needs an X11 session rather than a pure Wayland one."
        )
    if "unknown input format" in lower and "x11grab" in lower:
        return (
            "This FFmpeg cannot see the Linux screen. Install the system package "
            "(sudo apt install ffmpeg) and open the app again."
        )
    if "invalid data found" in lower or "error opening input" in lower:
        return (
            "FFmpeg could not read that link. Paste a direct .mp4 or .m3u8 address, "
            "or mirror the desktop while the video plays in the browser."
        )
    return text


class StreamPump:
    def __init__(self, log: Callable[[str], None]) -> None:
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.live = True
        self.running = False
        self._subs: list[queue.Queue[bytes]] = []
        self._vod: collections.deque[bytes] = collections.deque()
        self._vod_bytes = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._stderr: collections.deque[str] = collections.deque(maxlen=30)

    def start(
        self,
        commands: list[list[str]],
        live: bool,
        feed_audio: Callable | None,
        ready_timeout: float = 2.0,
        wait_for_output: bool = False,
        ready_bytes: int = 0,
    ) -> None:
        self.stop()
        self.live = live
        last_error = "FFmpeg did not start."
        for command in commands:
            self._stop = threading.Event()
            stop = self._stop
            saw = threading.Event()
            self._stderr.clear()
            uses_audio = feed_audio is not None and "pipe:0" in command
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if uses_audio else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=CREATE_NO_WINDOW,
            )
            self.proc = proc
            threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
            if uses_audio and feed_audio is not None:
                threading.Thread(target=feed_audio, args=(proc, stop), daemon=True).start()
            threading.Thread(target=self._read_stdout, args=(proc, stop, saw), daemon=True).start()
            if self._became_ready(proc, saw, ready_timeout, wait_for_output, ready_bytes):
                self.running = True
                return
            last_error = "\n".join(self._stderr) or f"FFmpeg exited with code {proc.returncode}."
            stop.set()
            self._kill(proc)
            if _input_rejected(last_error):
                break
            self.log("That encoder setting failed. Trying another.")
        self.proc = None
        self.running = False
        raise RuntimeError(_friendly_error(last_error))

    def _became_ready(
        self,
        proc: subprocess.Popen,
        saw: threading.Event,
        timeout: float,
        wait_for_output: bool,
        ready_bytes: int,
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return saw.is_set() and proc.returncode == 0
            if saw.is_set() and (not wait_for_output or ready_bytes <= 0 or self._buffered() >= ready_bytes):
                return True
            time.sleep(0.1)
        if proc.poll() is not None:
            return saw.is_set() and proc.returncode == 0
        if wait_for_output:
            return saw.is_set()
        return True

    def _buffered(self) -> int:
        with self._lock:
            return self._vod_bytes

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        proc = self.proc
        self.proc = None
        if proc is not None:
            self._kill(proc)
        with self._lock:
            self._vod.clear()
            self._vod_bytes = 0

    def subscribe(self) -> queue.Queue[bytes]:
        subscriber: queue.Queue[bytes] = queue.Queue(maxsize=20 if self.live else 3200)
        with self._lock:
            if len(self._subs) >= 2:
                raise RuntimeError("busy")
            if self.live:
                primed: list[bytes] = []
            else:
                primed = list(self._vod)
                self._vod.clear()
                self._vod_bytes = 0
            self._subs.append(subscriber)
        for chunk in primed:
            subscriber.put_nowait(chunk)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[bytes]) -> None:
        with self._lock:
            if subscriber in self._subs:
                self._subs.remove(subscriber)

    def _read_stdout(self, proc: subprocess.Popen, stop: threading.Event, saw: threading.Event) -> None:
        try:
            while not stop.is_set():
                chunk = proc.stdout.read(CHUNK) if proc.stdout else b""
                if not chunk:
                    break
                saw.set()
                self._fanout(chunk, stop)
        finally:
            if self.proc is proc:
                self.running = False

    def _fanout(self, chunk: bytes, stop: threading.Event) -> None:
        while not stop.is_set():
            with self._lock:
                subscribers = list(self._subs)
                if not self.live and not subscribers and self._vod_bytes + len(chunk) <= VOD_CAP:
                    self._vod.append(chunk)
                    self._vod_bytes += len(chunk)
                    return
            if self.live:
                for subscriber in subscribers:
                    self._put_live(subscriber, chunk, stop)
                return
            if not subscribers:
                time.sleep(0.05)
                continue
            if self._put_vod(subscribers, chunk, stop):
                return

    def _put_live(self, subscriber: queue.Queue[bytes], chunk: bytes, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                subscriber.put(chunk, timeout=0.2)
                return
            except queue.Full:
                with self._lock:
                    if subscriber not in self._subs:
                        return

    def _put_vod(self, subscribers: list[queue.Queue[bytes]], chunk: bytes, stop: threading.Event) -> bool:
        delivered = False
        for subscriber in subscribers:
            while not stop.is_set():
                try:
                    subscriber.put(chunk, timeout=0.2)
                    delivered = True
                    break
                except queue.Full:
                    with self._lock:
                        if subscriber not in self._subs:
                            break
        if delivered:
            return True
        with self._lock:
            if not self._subs and self._vod_bytes + len(chunk) <= VOD_CAP:
                self._vod.append(chunk)
                self._vod_bytes += len(chunk)
                return True
        return False

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        if not proc.stderr:
            return
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            self._stderr.append(line)
            if "error" in line.lower() or "failed" in line.lower():
                self.log(line)

    def _kill(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
