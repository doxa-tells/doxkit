# Установщик DoxKit для Windows 10/11.
# Одной командой (PowerShell):
#   irm https://raw.githubusercontent.com/doxa-tells/doxkit/main/install.ps1 | iex
# Можно запускать повторно — обновит приложение, уже установленное пропустит.

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = "doxa-tells/doxkit"
$Dest = Join-Path $env:LOCALAPPDATA "DoxKit"

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host ""; Write-Host $t -ForegroundColor Red; Read-Host "Нажмите Enter, чтобы закрыть"; exit 1 }

function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
              [Environment]::GetEnvironmentVariable("Path", "User")
}

function WG-Install($id, $name) {
  $null = winget list --id $id -e --accept-source-agreements 2>$null
  if ($LASTEXITCODE -eq 0) { Write-Host "    $name — уже установлен"; return }
  Write-Host "    Ставлю $name…"
  winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Host "    ! ${name}: winget вернул код $LASTEXITCODE (продолжаю)" -ForegroundColor Yellow }
}

# ---------------------------------------------------------------------------
Step "1/6 Проверяю систему"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Нужна 64-битная Windows 10 или 11." }
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  Fail "Не найден winget. Откройте Microsoft Store, найдите «Установщик приложений» (App Installer), обновите его и запустите команду ещё раз."
}

# ---------------------------------------------------------------------------
Step "2/6 Программы: Python, ffmpeg, Ghostscript, LibreOffice, Deno"
Write-Host "    Windows может спросить разрешение — нажимайте «Да»." -ForegroundColor Yellow
WG-Install "Python.Python.3.12"                "Python 3.12"
WG-Install "Gyan.FFmpeg"                       "ffmpeg"
WG-Install "ArtifexSoftware.GhostScript"       "Ghostscript"
WG-Install "TheDocumentFoundation.LibreOffice" "LibreOffice"
WG-Install "DenoLand.Deno"                     "Deno"
Refresh-Path

# Находим Python 3.12
$Py = $null
foreach ($c in @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:ProgramFiles "Python312\python.exe"))) {
  if (Test-Path $c) { $Py = $c; break }
}
if (-not $Py) {
  try { $Py = (& py -3.12 -c "import sys; print(sys.executable)").Trim() } catch {}
}
if (-not $Py -or -not (Test-Path $Py)) { Fail "Не нашёл Python 3.12 после установки. Перезагрузите компьютер и запустите команду ещё раз." }

# ---------------------------------------------------------------------------
Step "3/6 Скачиваю DoxKit"
Get-Process pythonw, python -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like "$Dest*" } | Stop-Process -Force -ErrorAction SilentlyContinue
$tmp = Join-Path $env:TEMP ("doxkit_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zip = Join-Path $tmp "doxkit.zip"
Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
Expand-Archive $zip -DestinationPath $tmp -Force
$srcDir = Get-ChildItem $tmp -Directory | Select-Object -First 1
New-Item -ItemType Directory -Path $Dest -Force | Out-Null
foreach ($f in @("app.py", "index.html", "make_icon.py")) {
  Copy-Item (Join-Path $srcDir.FullName $f) $Dest -Force
}
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
Step "4/6 Python-библиотеки и Whisper (несколько минут)"
$Venv = Join-Path $Dest "venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) { & $Py -m venv $Venv }
$VPy  = Join-Path $Venv "Scripts\python.exe"
$VPyw = Join-Path $Venv "Scripts\pythonw.exe"
& $VPy -m pip install -q --upgrade pip
& $VPy -m pip install -q flask pillow pillow-heif pdf2docx python-docx huggingface_hub pywebview faster-whisper
if ($LASTEXITCODE -ne 0) { Fail "Не удалось поставить библиотеки Python. Проверьте интернет и запустите команду ещё раз." }
& $VPy -m pip install -q -U "yt-dlp[default]"
Write-Host "    Модель Whisper Turbo (~1.6 ГБ, скачивается один раз)…"
& $VPy -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"
Write-Host "    Нейросеть «Убрать голос» (BS-RoFormer, ~640 МБ, один раз — 5–10 минут, не закрывайте)…"
& $VPy -m pip install -q -U "audio-separator[cpu]" audioread soundfile
$Models = Join-Path $Dest "models"
& $VPy -c "from audio_separator.separator import Separator as S; s=S(model_file_dir=r'$Models'); s.load_model(model_filename='model_bs_roformer_ep_317_sdr_12.9755.ckpt')"

# ---------------------------------------------------------------------------
Step "5/6 Иконка"
& $VPy (Join-Path $Dest "make_icon.py") $Dest | Out-Null

# ---------------------------------------------------------------------------
Step "6/6 Ярлыки на рабочем столе и в меню Пуск"
$ws = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "DoxKit.lnk"),
  (Join-Path ([Environment]::GetFolderPath("Programs")) "DoxKit.lnk")
)
foreach ($l in $targets) {
  $s = $ws.CreateShortcut($l)
  $s.TargetPath = $VPyw
  $s.Arguments = "`"$(Join-Path $Dest 'app.py')`""
  $s.WorkingDirectory = $Dest
  $s.IconLocation = (Join-Path $Dest "icon.ico")
  $s.Description = "DoxKit — сжатие, конвертация, расшифровка, скачивание"
  $s.Save()
}

Write-Host ""
Write-Host "Готово! Ярлык DoxKit — на рабочем столе и в меню Пуск." -ForegroundColor Green
Write-Host "Готовые файлы будут в папке «Загрузки\DoxKit»."
Start-Process $VPyw -ArgumentList "`"$(Join-Path $Dest 'app.py')`"" -WorkingDirectory $Dest
