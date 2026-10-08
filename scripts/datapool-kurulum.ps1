<#
.SYNOPSIS
    Veri havuzu kurulumu: paket kurulumu, klasor ayari, Claude Desktop MCP ayari, ilk tarama ve gece gorevi.

.DESCRIPTION
    Windows PowerShell 5.1 ve PowerShell 7 ile calisir. Yonetici yetkisi gerekmez.
    Adimlar:
      1. Python 3.10+ bulunur, win32-mcp-server[datapool] kurulur.
      2. Taranacak klasorler belirlenir (-Roots ya da OneDrive) ve
         WIN32_MCP_DATAPOOL_ROOTS kullanici degiskenine yazilir.
      3. ODA File Converter kontrol edilir (DWG icerigi icin).
      4. Claude Desktop ayar dosyasina "win32" MCP sunucusu eklenir (once yedek alinir).
      5. Ilk tarama calistirilir.
      6. Her gece 02:00'de artimli tarama yapan zamanlanmis gorev olusturulur.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\datapool-kurulum.ps1

.EXAMPLE
    .\datapool-kurulum.ps1 -Roots "C:\Users\ben\OneDrive - Firma\Projeler","C:\Users\ben\OneDrive - Firma\Teklifler" -Workers 6 -Hydrate
#>
[CmdletBinding()]
param(
    [string[]]$Roots = @(),
    [int]$Workers = 4,
    [switch]$Hydrate,
    [switch]$SkipInstall,
    [switch]$SkipIndex,
    [switch]$SkipClaude,
    [switch]$SkipSchedule,
    [switch]$ForceClaudeConfig,
    [string]$Source = "git+https://github.com/mustafadiscii-lang/win32-mcp-server.git",
    [string]$TaskTime = "02:00"
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "    [tamam] $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "    [uyari] $Text" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
# 1. Python ve paket
# ---------------------------------------------------------------------------
Write-Step "Python araniyor"
$python = $null
$pythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"; $pythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} else {
    throw "Python bulunamadi. https://www.python.org/downloads/ adresinden Python 3.10+ kurun ('Add to PATH' secili olsun)."
}
$version = & $python @pythonArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.10") { throw "Python $version bulundu; en az 3.10 gerekli." }
Write-Ok "Python $version"

if (-not $SkipInstall) {
    Write-Step "win32-mcp-server[datapool] kuruluyor"
    & $python @pythonArgs -m pip install --upgrade "win32-mcp-server[datapool] @ $Source"
    if ($LASTEXITCODE -ne 0) { throw "pip kurulumu basarisiz oldu." }
    Write-Ok "Paket kuruldu"
}

$scriptsDir = (& $python @pythonArgs -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
$userScriptsDir = (& $python @pythonArgs -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))").Trim()
$datapoolExe = $null
$serverExe = $null
foreach ($dir in @($scriptsDir, $userScriptsDir)) {
    if (-not $datapoolExe -and (Test-Path -LiteralPath (Join-Path $dir "win32-mcp-datapool.exe"))) {
        $datapoolExe = Join-Path $dir "win32-mcp-datapool.exe"
    }
    if (-not $serverExe -and (Test-Path -LiteralPath (Join-Path $dir "win32-mcp-server.exe"))) {
        $serverExe = Join-Path $dir "win32-mcp-server.exe"
    }
}
if (-not $datapoolExe -or -not $serverExe) {
    throw "win32-mcp-datapool.exe / win32-mcp-server.exe bulunamadi ($scriptsDir). Kurulumu -SkipInstall olmadan tekrar deneyin."
}
Write-Ok "Komutlar: $datapoolExe"

# ---------------------------------------------------------------------------
# 2. Klasorler
# ---------------------------------------------------------------------------
Write-Step "Taranacak klasorler belirleniyor"
if ($Roots.Count -eq 0) {
    foreach ($var in @("OneDriveCommercial", "OneDriveConsumer", "OneDrive")) {
        $value = [Environment]::GetEnvironmentVariable($var)
        if ($value -and (Test-Path -LiteralPath $value) -and ($Roots -notcontains $value)) { $Roots += $value }
    }
}
if ($Roots.Count -eq 0) { throw "OneDrive klasoru bulunamadi. -Roots ile klasor verin." }
foreach ($root in $Roots) {
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "Klasor yok: $root" }
    Write-Ok $root
}
$rootsValue = ($Roots | ForEach-Object { (Resolve-Path -LiteralPath $_).Path }) -join ";"
[Environment]::SetEnvironmentVariable("WIN32_MCP_DATAPOOL_ROOTS", $rootsValue, "User")
$env:WIN32_MCP_DATAPOOL_ROOTS = $rootsValue
Write-Ok "WIN32_MCP_DATAPOOL_ROOTS kullanici degiskenine yazildi"

# ---------------------------------------------------------------------------
# 3. ODA File Converter
# ---------------------------------------------------------------------------
Write-Step "ODA File Converter kontrol ediliyor"
$oda = $env:WIN32_MCP_ODA_CONVERTER
if (-not $oda) {
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        $hit = Get-ChildItem -Path (Join-Path $base "ODA") -Filter "ODAFileConverter.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName | Select-Object -Last 1
        if ($hit) { $oda = $hit.FullName; break }
    }
}
if ($oda) {
    Write-Ok "Bulundu: $oda"
} else {
    Write-Warn "Bulunamadi. DWG dosyalari yalnizca surum/ad bilgisiyle indekslenecek."
    Write-Warn "Icerik icin ucretsiz indirin: https://www.opendesign.com/guestfiles/oda_file_converter"
    Write-Warn "Kurduktan sonra bu betigi -SkipInstall ile tekrar calistirin."
}

# ---------------------------------------------------------------------------
# 4. Claude Desktop
# ---------------------------------------------------------------------------
if (-not $SkipClaude) {
    Write-Step "Claude Desktop MCP ayari"
    $configDir = Join-Path $env:APPDATA "Claude"
    $configPath = Join-Path $configDir "claude_desktop_config.json"
    if (-not (Test-Path -LiteralPath $configDir)) { New-Item -ItemType Directory -Path $configDir | Out-Null }

    $config = [pscustomobject]@{}
    if (Test-Path -LiteralPath $configPath) {
        $raw = [IO.File]::ReadAllText($configPath)
        if ($raw.Trim()) { $config = $raw | ConvertFrom-Json }
        $backup = "$configPath.yedek-$(Get-Date -Format yyyyMMdd-HHmmss)"
        Copy-Item $configPath $backup
        Write-Ok "Yedek: $backup"
    }
    if (-not ($config.PSObject.Properties.Name -contains "mcpServers")) {
        $config | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
    }
    $exists = $config.mcpServers.PSObject.Properties.Name -contains "win32"
    if ($exists -and -not $ForceClaudeConfig) {
        Write-Warn "'win32' sunucusu zaten tanimli; degistirilmedi (uzerine yazmak icin -ForceClaudeConfig)."
    } else {
        $serverEnv = [ordered]@{ WIN32_MCP_DATAPOOL_ROOTS = $rootsValue }
        if ($oda) { $serverEnv["WIN32_MCP_ODA_CONVERTER"] = $oda }
        $entry = [pscustomobject]@{ command = $serverExe; env = [pscustomobject]$serverEnv }
        if ($exists) { $config.mcpServers.PSObject.Properties.Remove("win32") }
        $config.mcpServers | Add-Member -NotePropertyName win32 -NotePropertyValue $entry
        $json = $config | ConvertTo-Json -Depth 32
        # Claude Desktop BOM'lu JSON'u okuyamayabilir; BOM'suz UTF-8 yaz.
        [IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Ok "Eklendi: $configPath (Claude Desktop'i yeniden baslatin)"
    }
}

# ---------------------------------------------------------------------------
# 5. Ilk tarama
# ---------------------------------------------------------------------------
if (-not $SkipIndex) {
    Write-Step "Ilk tarama basliyor (buyuk arsivlerde uzun surebilir, tekrar calistirmak guvenlidir)"
    $indexArgs = @("index", "--workers", "$Workers")
    if ($Hydrate) { $indexArgs += "--hydrate" }
    if ($oda) { $indexArgs += @("--oda", $oda) }
    & $datapoolExe @indexArgs
    if ($LASTEXITCODE -ne 0) { throw "Tarama hata ile bitti (kod $LASTEXITCODE)." }
    Write-Step "Havuz ozeti"
    & $datapoolExe stats
}

# ---------------------------------------------------------------------------
# 6. Gece gorevi
# ---------------------------------------------------------------------------
if (-not $SkipSchedule) {
    Write-Step "Zamanlanmis gorev (her gun $TaskTime)"
    try {
        $taskArgs = "index -q --workers $Workers"
        if ($Hydrate) { $taskArgs += " --hydrate" }
        if ($oda) { $taskArgs += " --oda `"$oda`"" }
        $action = New-ScheduledTaskAction -Execute $datapoolExe -Argument $taskArgs
        $trigger = New-ScheduledTaskTrigger -Daily -At $TaskTime
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
        Register-ScheduledTask -TaskName "VeriHavuzu" -Action $action -Trigger $trigger -Settings $settings `
            -Description "OneDrive proje arsivini veri havuzuna artimli olarak indeksler" -Force | Out-Null
        Write-Ok "Gorev 'VeriHavuzu' olusturuldu"
    } catch {
        Write-Warn "Gorev olusturulamadi: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "Kurulum tamamlandi." -ForegroundColor Green
Write-Host "Arama ornegi:  & `"$datapoolExe`" search `"zemin etudu`""
Write-Host "Ajan talimati: docs/DATAPOOL.md, bolum 4"
