<#
.SYNOPSIS
    Снимок лица робота: забирает PNG с робота на этот компьютер.

.DESCRIPTION
    Скрипт живёт на компьютере, а не на роботе, и это не случайно: папка
    назначения — на диске компьютера, и робот сам туда файл положить не
    может. Поэтому забираем, а не робот отдаёт: подключаемся по SSH, просим
    робота сделать снимок (face/screenshot.py — экран рисуется мимо X через
    DRM напрямую, поэтому обычные scrot/import на роботе не работают),
    затем забираем результат по SCP и убираем временный файл на роботе.

    Снимку нужен CAP_SYS_ADMIN (ядро не отдаёт GEM-хендл активного кадра
    рядовому процессу — это защита от чтения чужого экрана кем попало),
    поэтому на роботе он идёт через sudo. Чтобы не вводить пароль при
    каждом запуске (вводить его и вправду некому — SSH здесь не привязан
    к терминалу), на роботе один раз ставится узкое разрешение без пароля
    ИМЕННО на этот скрипт: bash scripts/setup_screenshot_sudo.sh.

.PARAMETER RobotHost
    Адрес робота (IP или имя в сети). Можно не указывать, если один раз
    задать переменную окружения ROBOT_HOST.

.PARAMETER RobotUser
    Имя пользователя на роботе. По умолчанию — wheeltec.

.PARAMETER Destination
    Куда сохранить снимок на этом компьютере.

.EXAMPLE
    .\скриншот_робота.ps1 -RobotHost 192.168.1.50

.EXAMPLE
    # Один раз:
    [Environment]::SetEnvironmentVariable('ROBOT_HOST', '192.168.1.50', 'User')
    # Дальше просто:
    .\скриншот_робота.ps1
#>
param(
    [string]$RobotHost = $env:ROBOT_HOST,
    [string]$RobotUser = "wheeltec",
    [string]$Destination = "F:\Robot-AI\Скрин"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RobotHost)) {
    Write-Error ("Не знаю адрес робота. Запусти так: " +
        ".\скриншот_робота.ps1 -RobotHost 192.168.1.50`n" +
        "Или задай один раз: [Environment]::SetEnvironmentVariable(" +
        "'ROBOT_HOST','192.168.1.50','User') — и перезапусти PowerShell.")
    exit 1
}

if (-not (Test-Path $Destination)) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Write-Host "Создал папку $Destination"
}

$target = "$RobotUser@$RobotHost"
Write-Host "Прошу робота сделать снимок ($target)..."

# sudo -n — без интерактивного запроса пароля. Если правило из
# setup_screenshot_sudo.sh на роботе не поставлено, эта команда сразу
# откажет с понятным текстом вместо того, чтобы зависнуть на приглашении
# ввести пароль, которое здесь некому увидеть и некому набрать.
$remoteOutput = ssh $target "sudo -n python3 ~/Robot-AI/face/screenshot.py 2>&1"

if ($LASTEXITCODE -ne 0) {
    Write-Error ("Робот не смог сделать снимок:`n$remoteOutput`n`n" +
        "Если в тексте выше 'a password is required' или 'not allowed' " +
        "— на роботе не настроено sudo без пароля для этого скрипта. " +
        "Один раз на роботе: bash scripts/setup_screenshot_sudo.sh")
    exit 1
}

Write-Host $remoteOutput

# screenshot.py печатает "сохранено: /tmp/robot-face-....png (WxH)" —
# путь и есть то единственное, что нам отсюда нужно.
if ($remoteOutput -notmatch "сохранено:\s+(\S+\.png)") {
    Write-Error "Не нашёл путь к файлу в ответе робота:`n$remoteOutput"
    exit 1
}
$remotePath = $Matches[1]

$localName = Split-Path $remotePath -Leaf
$localPath = Join-Path $Destination $localName

Write-Host "Забираю $remotePath -> $localPath"
scp "${target}:$remotePath" $localPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "scp не смог скачать файл — сеть или права?"
    exit 1
}

# Прибираемся за собой на роботе: /tmp и так очистится при перезагрузке,
# но копить там снимки до неё незачем.
ssh $target "rm -f $remotePath" | Out-Null

Write-Host "Готово: $localPath"
Invoke-Item $localPath
