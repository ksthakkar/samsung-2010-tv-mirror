pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name LaptopToTV --collect-all PySide6 --collect-all pyaudiowpatch --collect-all yt_dlp main.py
