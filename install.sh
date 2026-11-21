#!/bin/bash
# Установщик DoxKit для Mac.
# Одной командой из интернета:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/doxa-tells/doxkit/main/install.sh)"
# Можно запускать повторно — обновит приложение, уже установленное пропустит.
set -e
REPO="doxa-tells/doxkit"
DEST="$HOME/DoxKit"

if [ "$(uname)" != "Darwin" ]; then echo "DoxKit работает только на Mac."; exit 1; fi
ARCH="$(uname -m)"   # arm64 = M1/M2/M3/M4, x86_64 = Intel

# Где лежат файлы приложения: рядом со скриптом или качаем с GitHub
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/app.py" ]; then
  echo "==> Скачиваю DoxKit…"
  SRC="$(mktemp -d)/doxkit"
  mkdir -p "$SRC"
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$SRC" --strip-components=1
fi

echo "==> 1/6 Homebrew (если попросит пароль — это пароль от вашего Mac, символы не видны при вводе)"
if ! command -v brew >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/brew ] && [ ! -x /usr/local/bin/brew ]; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; else eval "$(/usr/local/bin/brew shellenv)"; fi

echo "==> 2/6 ffmpeg, Ghostscript, Python, LibreOffice"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list ffmpeg      >/dev/null 2>&1 || brew install ffmpeg
brew list ghostscript >/dev/null 2>&1 || brew install ghostscript
brew list deno        >/dev/null 2>&1 || brew install deno   # нужен загрузчику для YouTube
[ -d /Applications/LibreOffice.app ] || brew install --cask libreoffice

echo "==> 3/6 Копирую файлы в $DEST"
# Убираем старую версию под именем FileKit (venv переносим, чтобы не качать заново)
pkill -f "FileKit/app.py" 2>/dev/null || true
if [ -d "$HOME/FileKit" ] && [ ! -d "$DEST" ]; then mv "$HOME/FileKit" "$DEST"; fi
rm -rf "$HOME/FileKit" /Applications/FileKit.app "$HOME/Applications/FileKit.app" "$HOME/Desktop/FileKit.app"
pkill -f "$DEST/app.py" 2>/dev/null || true
mkdir -p "$DEST"
cp "$SRC/app.py" "$SRC/index.html" "$SRC/make_icon.py" "$DEST/"

echo "==> 4/6 Python-библиотеки и Whisper"
PY="$(brew --prefix python@3.12)/bin/python3.12"
[ -d "$DEST/venv" ] || "$PY" -m venv "$DEST/venv"
"$DEST/venv/bin/python" -m pip install -q --upgrade pip
"$DEST/venv/bin/python" -m pip install -q flask pillow pillow-heif pdf2docx python-docx huggingface_hub pywebview
"$DEST/venv/bin/python" -m pip install -q -U "yt-dlp[default]"
if [ "$ARCH" = "arm64" ]; then
  "$DEST/venv/bin/python" -m pip install -q -U mlx-whisper
  echo "    Модель Whisper Turbo (~1.6 ГБ, скачивается один раз)…"
  "$DEST/venv/bin/python" -c "from huggingface_hub import snapshot_download; snapshot_download('mlx-community/whisper-large-v3-turbo')"
else
  echo "    Это Mac на Intel — расшифровка в текст недоступна (нужен чип M1 и новее). Остальное работает."
fi

echo "==> 5/6 Собираю DoxKit.app"
"$DEST/venv/bin/python" "$DEST/make_icon.py" "$DEST"

if [ -w /Applications ]; then APPDIR=/Applications; else APPDIR="$HOME/Applications"; mkdir -p "$APPDIR"; fi
APP="$APPDIR/DoxKit.app"
rm -rf "$APP" "$HOME/Applications/DoxKit.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DEST/icon.icns" "$APP/Contents/Resources/icon.icns"

cat > "$APP/Contents/MacOS/DoxKit" <<'EOF'
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$HOME/DoxKit"
exec "$HOME/DoxKit/venv/bin/python" "$HOME/DoxKit/app.py" >> "$HOME/DoxKit/filekit.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/DoxKit"

cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>DoxKit</string>
  <key>CFBundleDisplayName</key><string>DoxKit</string>
  <key>CFBundleIdentifier</key><string>work.benevsky.doxkit</string>
  <key>CFBundleVersion</key><string>1.1</string>
  <key>CFBundleShortVersionString</key><string>1.1</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>DoxKit</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
touch "$APP"

echo "==> 6/6 Иконка на рабочий стол"
rm -f "$HOME/Desktop/DoxKit" "$HOME/Desktop/DoxKit.app"
ln -s "$APP" "$HOME/Desktop/DoxKit.app"
rm -f "$DEST/start.sh"

echo ""
echo "Готово! DoxKit лежит в $APPDIR и на рабочем столе."
echo "Совет: перетащите иконку в Dock, чтобы всегда была под рукой."
open "$APP"
