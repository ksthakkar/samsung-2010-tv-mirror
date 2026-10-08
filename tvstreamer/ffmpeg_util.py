"""FFmpeg location, encoder checks, and command lines.

A 2010 Samsung AllShare player is happiest with H.264 Baseline (no B-frames)
and AC-3 audio inside a normal MPEG-TS file. AAC inside MPEG-TS is often silent
on that generation, and a High profile stream often will not start.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ffmpeg_dir() -> Path:
    return app_dir() / "vendor" / "ffmpeg"


def _ffmpeg_names() -> tuple[str, str]:
    if sys.platform == "win32":
        return "ffmpeg.exe", "ffprobe.exe"
    return "ffmpeg", "ffprobe"


def find_ffmpeg() -> Path | None:
    ffmpeg_name, _probe = _ffmpeg_names()
    candidates = [
        path for path in (
            ffmpeg_dir() / ffmpeg_name,
            Path(shutil.which("ffmpeg") or ""),
        )
        if path and path.is_file()
    ]
    if sys.platform.startswith("linux"):
        # The portable Linux build cannot open the screen. A distro build can.
        for candidate in candidates:
            if _lists_device(candidate, "x11grab"):
                return candidate
    return candidates[0] if candidates else None


def find_ffprobe(ffmpeg: Path) -> Path | None:
    _ffmpeg_name, probe_name = _ffmpeg_names()
    sibling = ffmpeg.with_name(probe_name)
    if sibling.is_file():
        return sibling
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def ensure_ffmpeg(log) -> Path:
    found = find_ffmpeg()
    if found:
        return found
    destination = ffmpeg_dir()
    destination.mkdir(parents=True, exist_ok=True)
    log("Downloading FFmpeg (about 110 MB). This happens once.")
    if sys.platform == "win32":
        _install_windows_ffmpeg(destination, log)
    elif sys.platform == "darwin":
        _install_mac_ffmpeg(destination, log)
    else:
        _install_linux_ffmpeg(destination, log)
    found = find_ffmpeg()
    if not found:
        raise RuntimeError("FFmpeg was downloaded, but the ffmpeg program was not in the archive.")
    if sys.platform.startswith("linux") and not _lists_device(found, "x11grab"):
        log("Desktop mirror needs the system FFmpeg, which can see the screen.")
        log("Install it with: sudo apt install ffmpeg   (or the same package on your distro).")
        log("File and web playback still work with the downloaded copy.")
    _mark_executable(found)
    probe = find_ffprobe(found)
    if probe is not None and probe.parent == found.parent:
        _mark_executable(probe)
    return found


def _install_windows_ffmpeg(destination: Path, log) -> None:
    archive = destination / "essentials.zip"
    _download(FFMPEG_URL, archive, log)
    with zipfile.ZipFile(archive) as bundle:
        for name in bundle.namelist():
            base = name.replace("\\", "/").split("/")[-1].lower()
            if base in {"ffmpeg.exe", "ffprobe.exe"}:
                target = destination / base
                with bundle.open(name) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                log(f"Installed {target.name}")
    archive.unlink(missing_ok=True)


def _install_mac_ffmpeg(destination: Path, log) -> None:
    for name, url in (
        ("ffmpeg", "https://evermeet.cx/ffmpeg/getrelease/zip"),
        ("ffprobe", "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip"),
    ):
        archive = destination / f"{name}.zip"
        _download(url, archive, log)
        with zipfile.ZipFile(archive) as bundle:
            member = next(item for item in bundle.namelist() if not item.endswith("/"))
            target = destination / name
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            _mark_executable(target)
            log(f"Installed {target.name}")
        archive.unlink(missing_ok=True)


def _install_linux_ffmpeg(destination: Path, log) -> None:
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"aarch64", "arm64"} else "amd64"
    url = f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"
    archive = destination / "ffmpeg.tar.xz"
    _download(url, archive, log)
    with tarfile.open(archive, "r:xz") as bundle:
        for member in bundle.getmembers():
            base = Path(member.name).name
            if base not in {"ffmpeg", "ffprobe"} or not member.isfile():
                continue
            source = bundle.extractfile(member)
            if source is None:
                continue
            target = destination / base
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            _mark_executable(target)
            log(f"Installed {target.name}")
    archive.unlink(missing_ok=True)


def _lists_device(ffmpeg: Path, name: str) -> bool:
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-devices"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = f"{proc.stdout}\n{proc.stderr}".lower()
    return name.lower() in text


def _mark_executable(path: Path) -> None:
    if sys.platform == "win32" or not path.is_file():
        return
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _download(url: str, destination: Path, log) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "tvstreamer"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = 0
        next_report = 20 * 1024 * 1024
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            output.write(chunk)
            total += len(chunk)
            if total >= next_report:
                log(f"Downloaded {total // (1024 * 1024)} MB")
                next_report += 20 * 1024 * 1024


def listed_encoders(ffmpeg: Path) -> set[str]:
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
        timeout=20,
    )
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("h264"):
            names.add(parts[1])
        if len(parts) >= 2 and parts[1] == "libx264":
            names.add("libx264")
    return names


def probe_encoder(ffmpeg: Path, encoder: str) -> bool:
    if encoder == "libx264":
        return True
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=0.25",
        "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def working_encoders(ffmpeg: Path, log) -> list[str]:
    present = listed_encoders(ffmpeg)
    if sys.platform == "darwin":
        order = ["h264_videotoolbox", "h264_nvenc", "libx264"]
    elif sys.platform.startswith("linux"):
        order = ["h264_nvenc", "h264_qsv", "libx264"]
    else:
        order = ["h264_qsv", "h264_nvenc", "h264_amf", "libx264"]
    working = []
    for name in order:
        if name not in present and name != "libx264":
            continue
        if name == "libx264" and "libx264" not in present:
            continue
        log(f"Checking encoder {name}...")
        if probe_encoder(ffmpeg, name):
            working.append(name)
    return working


def encoder_label(name: str) -> str:
    return {
        "h264_qsv": "Intel Quick Sync",
        "h264_nvenc": "NVIDIA",
        "h264_amf": "AMD",
        "h264_videotoolbox": "Apple",
        "libx264": "Software",
    }.get(name, name)


def choose_encoders(preference: str, available: list[str]) -> list[str]:
    if preference == "auto":
        return list(available) or ["libx264"]
    ordered = [preference]
    if "libx264" in available and preference != "libx264":
        ordered.append("libx264")
    return [name for name in ordered if name in available or name == "libx264"]


def desktop_command(
    ffmpeg: Path,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    audio_rate: int,
    audio_channels: int,
    use_pipe_audio: bool,
    sample_format: str = "s16le",
) -> list[str]:
    pixel_format, codec = _codec_args(encoder, fps, bitrate_k, live=True)
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "warning",
        "-thread_queue_size", "16",
        "-rtbufsize", "64M",
        "-use_wallclock_as_timestamps", "1",
        "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1", "-i", "desktop",
    ]
    cmd += _desktop_audio(audio_rate, audio_channels, use_pipe_audio, sample_format)
    cmd += [
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", f"fps={fps}," + _fit(width, height, pixel_format, pad=True) + "," + _video_clock(use_pipe_audio),
        *codec,
        *_ac3_args(),
        *_clarity_filter(use_pipe_audio),
        *_mux_tail(use_pipe_audio),
    ]
    return cmd


def desktop_attempts(
    ffmpeg: Path,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    audio_rate: int,
    audio_channels: int,
    use_pipe_audio: bool,
    sample_format: str = "s16le",
    native_input: list[str] | None = None,
    mac_audio: str = "none",
) -> list[list[str]]:
    """Fastest capture first. Later commands run only if that capture will not start."""
    if sys.platform == "darwin":
        audio_index = mac_audio if use_pipe_audio else "none"
        return [
            _mac_desktop(ffmpeg, encoder, width, height, fps, bitrate_k, audio_index, strict=True),
            _mac_desktop(ffmpeg, encoder, width, height, fps, bitrate_k, audio_index, strict=False),
        ]
    if sys.platform != "win32":
        return _linux_attempts(
            ffmpeg, encoder, width, height, fps, bitrate_k,
            native_input if use_pipe_audio else None,
        )
    attempts = []
    if encoder == "h264_qsv" and _same_shape(width, height):
        attempts.append(_desktop_gpu(
            ffmpeg, width, height, fps, bitrate_k, audio_rate, audio_channels, use_pipe_audio, sample_format,
        ))
    attempts.append(_desktop_dda(
        ffmpeg, encoder, width, height, fps, bitrate_k, audio_rate, audio_channels, use_pipe_audio, sample_format,
    ))
    attempts.append(desktop_command(
        ffmpeg, encoder, width, height, fps, bitrate_k, audio_rate, audio_channels, use_pipe_audio, sample_format,
    ))
    return attempts


def _mac_desktop(
    ffmpeg: Path,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    mac_audio: str,
    strict: bool,
) -> list[str]:
    pixel_format, codec = _codec_args(encoder, fps, bitrate_k, live=True)
    videos, _audios = avfoundation_devices(ffmpeg)
    screen = next((index for index, name in videos if "screen" in name.lower() or "display" in name.lower()), "1")
    has_audio = mac_audio not in {"", "none"}
    spec = f"{screen}:{mac_audio if has_audio else 'none'}"
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "warning",
        "-f", "avfoundation",
        "-framerate", str(fps),
    ]
    if strict:
        cmd += ["-capture_cursor", "1", "-pixel_format", "uyvy422"]
    cmd += ["-i", spec]
    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0"]
    cmd += [
        "-vf", f"fps={fps}," + _fit(width, height, pixel_format, pad=True),
        *codec,
        *_ac3_args(),
        *_clarity_filter(has_audio),
        *_mux_tail(has_audio),
    ]
    return cmd


def _linux_attempts(
    ffmpeg: Path,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    native_input: list[str] | None,
) -> list[list[str]]:
    inputs: list[list[str] | None] = [native_input]
    if native_input and "pulse" in native_input:
        swapped = ["pipewire" if part == "pulse" else part for part in native_input]
        inputs.append(swapped)
    commands = []
    for audio in inputs:
        commands.append(_linux_desktop(
            ffmpeg, encoder, width, height, fps, bitrate_k, audio, sized=True,
        ))
        commands.append(_linux_desktop(
            ffmpeg, encoder, width, height, fps, bitrate_k, audio, sized=False,
        ))
    return commands


def _linux_desktop(
    ffmpeg: Path,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    native_input: list[str] | None,
    sized: bool,
) -> list[str]:
    pixel_format, codec = _codec_args(encoder, fps, bitrate_k, live=True)
    screen_w, screen_h = _screen_size()
    grab_w = screen_w if screen_w >= 2 else width
    grab_h = screen_h if screen_h >= 2 else height
    grab_w -= grab_w % 2
    grab_h -= grab_h % 2
    display = os.environ.get("DISPLAY") or ":0"
    has_audio = bool(native_input)
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "warning",
        "-thread_queue_size", "64",
        "-f", "x11grab",
        "-framerate", str(fps),
        "-draw_mouse", "1",
    ]
    if sized:
        cmd += ["-video_size", f"{grab_w}x{grab_h}"]
    cmd += ["-i", display]
    if has_audio:
        cmd += list(native_input or [])
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0"]
    cmd += [
        "-vf", f"fps={fps}," + _fit(width, height, pixel_format, pad=True),
        *codec,
        *_ac3_args(),
        *_clarity_filter(has_audio),
        *_mux_tail(has_audio),
    ]
    return cmd


def avfoundation_devices(ffmpeg: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], []
    section = ""
    videos: list[tuple[str, str]] = []
    audios: list[tuple[str, str]] = []
    for line in (proc.stderr + "\n" + proc.stdout).splitlines():
        lower = line.lower()
        if "video devices" in lower:
            section = "video"
            continue
        if "audio devices" in lower:
            section = "audio"
            continue
        if "] [" not in line and "[" not in line:
            continue
        start = line.rfind("[")
        end = line.rfind("]")
        if start < 0 or end < start:
            continue
        token = line[start + 1:end].strip()
        if not token.isdigit():
            continue
        name = line[end + 1:].strip()
        if not name:
            continue
        (videos if section == "video" else audios).append((token, name))
    return videos, audios


def _desktop_gpu(
    ffmpeg: Path,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    audio_rate: int,
    audio_channels: int,
    use_pipe_audio: bool,
    sample_format: str = "s16le",
) -> list[str]:
    _pixel, codec = _codec_args("h264_qsv", fps, bitrate_k, live=True)
    graph = (
        f"ddagrab=draw_mouse=1:framerate={fps},"
        f"hwmap=derive_device=qsv,format=qsv,"
        f"scale_qsv=w={width}:h={height},{_video_clock(use_pipe_audio)}[v]"
    )
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "warning",
        "-init_hw_device", "d3d11va=dx11",
        "-init_hw_device", "qsv=qs@dx11",
        "-filter_hw_device", "dx11",
        "-filter_complex", graph,
    ]
    cmd += _desktop_audio(audio_rate, audio_channels, use_pipe_audio, sample_format)
    cmd += [
        "-map", "[v]", "-map", "0:a:0",
        *codec,
        *_ac3_args(),
        *_clarity_filter(use_pipe_audio),
        *_mux_tail(use_pipe_audio),
    ]
    return cmd


def _desktop_dda(
    ffmpeg: Path,
    encoder: str,
    width: int,
    height: int,
    fps: int,
    bitrate_k: int,
    audio_rate: int,
    audio_channels: int,
    use_pipe_audio: bool,
    sample_format: str = "s16le",
) -> list[str]:
    pixel_format, codec = _codec_args(encoder, fps, bitrate_k, live=True)
    graph = (
        f"ddagrab=draw_mouse=1:framerate={fps}:output_fmt=bgra,"
        "hwdownload,format=bgra,"
        f"{_fit(width, height, pixel_format, pad=True)},{_video_clock(use_pipe_audio)}[v]"
    )
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "warning",
        "-filter_complex", graph,
    ]
    cmd += _desktop_audio(audio_rate, audio_channels, use_pipe_audio, sample_format)
    cmd += [
        "-map", "[v]", "-map", "0:a:0",
        *codec,
        *_ac3_args(),
        *_clarity_filter(use_pipe_audio),
        *_mux_tail(use_pipe_audio),
    ]
    return cmd


def _ac3_args() -> list[str]:
    """Highest AC-3 rate, as plain stereo.

    Left unmarked, a 2010 Samsung treats the track as Dolby Surround and
    runs a matrix on it. That matrix is what makes voices and music sound dull.
    Coupling off keeps the high frequencies in each channel.
    """
    return [
        "-c:a", "ac3", "-b:a", "640k", "-ar", "48000", "-ac", "2",
        "-dialnorm", "-31",
        "-dsur_mode", "1",
        "-dsurex_mode", "1",
        "-dheadphone_mode", "1",
        "-dmix_mode", "2",
        "-channel_coupling", "0",
    ]


def _clarity_filter(synced: bool) -> list[str]:
    """Lift the band a 2010 TV speaker can actually make.

    AC-3 at 640 kbps already keeps the highs. The set itself rolls off, so a
    small shelf above 3 kHz is what makes speech and music sound clearer.
    Peaks are eased off without a lookahead, so the timestamps stay even.
    """
    if not synced:
        return []
    shelf = "highshelf=f=3000:g=3:t=s:w=0.9:r=f32,alimiter=limit=0.97:level=false:attack=0.1"
    # Windows audio already shares the PC clock. Other systems capture picture
    # and sound in one program, and a tiny resample keeps them from drifting.
    chain = shelf if sys.platform == "win32" else f"aresample=async=1,{shelf}"
    return ["-af", chain]


def _video_clock(synced: bool) -> str:
    """Wall clock when sound is real. A plain frame counter drifts away from it."""
    if not synced:
        return "setpts=N/FRAME_RATE/TB"
    return (
        "setpts='if(eq(N,0),RTCTIME/1000000/TB,"
        "max(RTCTIME/1000000/TB,PREV_OUTPTS+1))'"
    )


def _mux_tail(synced: bool) -> list[str]:
    # Four seconds lets the muxer hold the earlier stream until the other
    # catches up, instead of sending one of them 20 ms early.
    if synced:
        timing = [
            "-fps_mode", "passthrough",
            "-max_interleave_delta", "4000000",
            "-avoid_negative_ts", "make_zero",
        ]
    else:
        timing = [
            "-af", "aresample=async=1:first_pts=0",
            "-fps_mode", "cfr",
            "-max_interleave_delta", "20000",
        ]
    return [
        *timing,
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-pat_period", "0.1",
        "pipe:1",
    ]


def _desktop_audio(
    audio_rate: int,
    audio_channels: int,
    use_pipe_audio: bool,
    sample_format: str = "s16le",
) -> list[str]:
    if use_pipe_audio:
        return [
            "-use_wallclock_as_timestamps", "1",
            "-thread_queue_size", "8",
            "-f", sample_format,
            "-ar", str(audio_rate),
            "-ac", str(audio_channels),
            *(["-channel_layout", "stereo"] if audio_channels == 2 else []),
            "-i", "pipe:0",
        ]
    return ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]


def _same_shape(width: int, height: int) -> bool:
    screen_w, screen_h = _screen_size()
    if screen_w < 2 or screen_h < 2 or height < 2:
        return True
    return abs((screen_w / screen_h) - (width / height)) < 0.03


def _screen_size() -> tuple[int, int]:
    try:
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is not None and app.primaryScreen() is not None:
            size = app.primaryScreen().size()
            return int(size.width()), int(size.height())
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            user = ctypes.windll.user32
            return int(user.GetSystemMetrics(0)), int(user.GetSystemMetrics(1))
        except (AttributeError, OSError, ValueError):
            return 0, 0
    if sys.platform == "darwin":
        return _darwin_screen()
    return _linux_screen()


def _darwin_screen() -> tuple[int, int]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    numbers = []
    for part in proc.stdout.replace(",", " ").split():
        try:
            numbers.append(int(part))
        except ValueError:
            continue
    if len(numbers) >= 4:
        return max(0, numbers[2] - numbers[0]), max(0, numbers[3] - numbers[1])
    return 0, 0


def _linux_screen() -> tuple[int, int]:
    try:
        proc = subprocess.run(
            ["xrandr", "--current"],
            capture_output=True, text=True, timeout=5, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, 0
    import re
    match = re.search(r"(\d+)x(\d+)\s+\d+(?:\.\d+)?\*", proc.stdout)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def file_command(ffmpeg: Path, encoder: str, path: Path, has_audio: bool) -> list[str]:
    pixel_format, codec = _codec_args(encoder, fps=24, bitrate_k=10000, live=False, quality=True)
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "warning", "-i", str(path)]
    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-map", "0:v:0", "-map", "1:a:0"]
    cmd += [
        "-vf", _fit(1920, 1080, pixel_format, pad=False, quality=True),
        *codec,
        *_ac3_args(),
        "-fps_mode", "passthrough",
        "-shortest",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-pat_period", "0.1",
        "pipe:1",
    ]
    return cmd


def web_command(
    ffmpeg: Path,
    encoder: str,
    video_url: str,
    audio_url: str | None,
    headers: dict,
    embedded_audio: bool = True,
    hwaccel: bool = False,
) -> list[str]:
    pixel_format, codec = _codec_args(encoder, fps=24, bitrate_k=12000, live=False, quality=True)
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "warning"]
    if hwaccel and encoder == "h264_qsv":
        cmd += ["-hwaccel", "qsv", "-hwaccel_output_format", "nv12"]
    _add_url_input(cmd, video_url, headers)
    if audio_url:
        _add_url_input(cmd, audio_url, headers)
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    elif embedded_audio:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    else:
        cmd += [
            "-protocol_whitelist", "lavfi,http,https,tcp,tls,crypto",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0",
        ]
    cmd += [
        "-vf", _fit(1920, 1080, pixel_format, pad=False, quality=True),
        *codec,
        *_ac3_args(),
        "-fps_mode", "passthrough",
        "-shortest",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-pat_period", "0.1",
        "pipe:1",
    ]
    return cmd


def _add_url_input(cmd: list[str], url: str, headers: dict) -> None:
    # Network protocols only. A page must not be able to point FFmpeg at a local file.
    cmd += [
        "-protocol_whitelist", "http,https,tcp,tls,crypto",
        "-allowed_extensions", "ALL",
        "-allowed_segment_extensions", "ALL",
        "-extension_picky", "0",
        "-seekable", "0",
    ]
    lines = []
    agent = _header_value(headers.get("User-Agent"))
    referer = _header_value(headers.get("Referer"))
    if agent:
        lines.append(f"User-Agent: {agent}")
    if referer:
        lines.append(f"Referer: {referer}")
    cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if lines:
        cmd += ["-headers", "\r\n".join(lines) + "\r\n"]
    cmd += ["-i", url]


def _header_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n")
    return cleaned[:300]


def _fit(width: int, height: int, pixel_format: str, pad: bool, quality: bool = False) -> str:
    flags = ":flags=lanczos" if quality else ""
    if pad:
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease{flags},"
            f"scale=trunc(iw/2)*2:trunc(ih/2)*2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format={pixel_format}"
        )
    return (
        f"scale='min({width},iw)':'min({height},ih)':force_original_aspect_ratio=decrease{flags},"
        f"scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1,format={pixel_format}"
    )


def _codec_args(encoder: str, fps: int, bitrate_k: int, live: bool, quality: bool = False) -> tuple[str, list[str]]:
    if live:
        # One second of rate control. A few seconds of delay is acceptable,
        # and a tiny buffer was throwing away picture quality.
        buf = bitrate_k
        gop = str(max(fps, 1))
    else:
        buf = bitrate_k * 2
        gop = "48"
    rate = ["-b:v", f"{bitrate_k}k", "-maxrate", f"{bitrate_k}k", "-bufsize", f"{buf}k"]
    if encoder == "h264_qsv":
        if live:
            tuning = [
                "-preset", "veryfast", "-scenario", "remotegaming",
                "-async_depth", "4", "-low_delay_brc", "0", "-look_ahead", "0",
                "-extbrc", "1", "-mbbrc", "1",
                "-forced_idr", "1",
            ]
        elif quality:
            tuning = [
                "-preset", "slow", "-scenario", "archive", "-async_depth", "4",
                "-extbrc", "1", "-mbbrc", "1",
            ]
        else:
            tuning = ["-preset", "medium", "-async_depth", "4"]
        return "nv12", [
            "-c:v", "h264_qsv", *tuning, "-profile:v", "baseline", "-level", "4.0",
            "-g", gop, "-bf", "0", *rate,
        ]
    if encoder == "h264_nvenc":
        preset = "p2" if live else "p6"
        return "yuv420p", [
            "-c:v", "h264_nvenc", "-preset", preset, "-profile:v", "baseline",
            "-level", "4.0", "-rc", "cbr", "-g", gop, "-bf", "0", "-pix_fmt", "yuv420p", *rate,
        ]
    if encoder == "h264_amf":
        quality_name = "speed" if live else "quality"
        return "yuv420p", [
            "-c:v", "h264_amf", "-quality", quality_name, "-profile:v", "baseline", "-rc", "cbr",
            "-g", gop, "-bf", "0", "-pix_fmt", "yuv420p", *rate,
        ]
    if encoder == "h264_videotoolbox":
        return "yuv420p", [
            "-c:v", "h264_videotoolbox", "-profile:v", "baseline", "-level", "4.0",
            "-allow_sw", "1", "-g", gop, "-bf", "0", "-pix_fmt", "yuv420p", *rate,
        ]
    preset = "veryfast" if live else "slow"
    return "yuv420p", [
        "-c:v", "libx264", "-preset", preset, "-profile:v", "baseline", "-level", "4.0",
        "-pix_fmt", "yuv420p", "-g", gop, "-bf", "0", *rate,
    ]


def media_facts(ffprobe: Path | None, path: Path) -> tuple[bool, str | None]:
    if ffprobe is None:
        return True, None
    audio = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
    )
    duration = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30,
    )
    has_audio = bool(audio.stdout.strip())
    clock = None
    try:
        seconds = float(duration.stdout.strip())
        hours, rem = divmod(int(seconds), 3600)
        minutes, secs = divmod(rem, 60)
        clock = f"{hours}:{minutes:02d}:{secs:02d}.000"
    except ValueError:
        clock = None
    return has_audio, clock


MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/mp4",
    ".mkv": "video/x-matroska",
    ".avi": "video/avi",
    ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mpeg",
    ".m2ts": "video/mpeg",
    ".webm": "video/webm",
}

# Containers a 2010 Samsung often plays without a conversion.
DIRECT_OK = {".mp4", ".m4v", ".mpg", ".mpeg", ".ts"}


def suggest_convert(path: Path) -> bool:
    return path.suffix.lower() not in DIRECT_OK
