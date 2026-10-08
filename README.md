# Laptop Screen

Share this laptop with a 2010 Samsung TV. The TV lists it under Source as **Laptop Screen**, next to Universal Media Server. From there you can mirror the desktop with sound, play a video file, or play a web link. Nothing from a web link is saved.

## Open it

Install Python 3.10 or newer once, copy this folder onto the laptop, then double-click:

| Laptop | Double-click |
| --- | --- |
| Windows | `Launch Laptop Screen.bat` |
| Mac | `Launch Laptop Screen.command` |
| Linux | `Launch Laptop Screen.sh` |

The first open sets itself up and downloads FFmpeg. Later opens just start the app.

On a Mac, if the file will not open, run this once in Terminal from the folder:

```
chmod +x "Launch Laptop Screen.command"
```

On Linux, install Python’s virtual environment tools and the system FFmpeg so the screen can be captured:

```
sudo apt install python3 python3-venv python3-pip ffmpeg
```

## On the TV

Open **Source** and choose **Laptop Screen**. Allow the app on your home network if a firewall asks. Close the app when you are done.

The picture sits a couple of seconds behind the laptop. That is fine for video.

## Sound

- **Windows** takes the speakers directly. For the clearest sound, pick Speakers in the app when that is where the video is playing.
- **Mac** needs Screen Recording permission. System audio also needs a loopback device such as [BlackHole](https://existential.audio/blackhole/). Send the video’s sound there, then choose it in the audio list.
- **Linux** uses the X11 screen and PulseAudio or PipeWire. A pure Wayland session cannot share the whole desktop.

For a movie file the TV already plays, turn **Convert** off. The remote can then pause and seek.
