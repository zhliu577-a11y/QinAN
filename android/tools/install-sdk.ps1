# 在一台干净的 Windows 机器上装「够用来构建这个 App」的最小 Android SDK。
#
# 只装四样东西：cmdline-tools、platform-tools、platforms;android-34、build-tools;34.0.0。
# 不装 Android Studio，也不需要 Gradle。
#
# 用法（管理员或普通用户都行，默认装到 %LOCALAPPDATA%\Android\Sdk）：
#   powershell -ExecutionPolicy Bypass -File tools\install-sdk.ps1

[CmdletBinding()]
param(
    [string]$SdkRoot = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$Platform = "android-34",
    [string]$BuildTools = "34.0.0",
    [switch]$SkipLicenses
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$toolsZip = 'https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip'
$downloadDir = Join-Path $env:TEMP 'qinan-android-sdk'
$zipPath = Join-Path $downloadDir 'commandlinetools.zip'
$cmdlineRoot = Join-Path $SdkRoot 'cmdline-tools'
$cmdlineLatest = Join-Path $cmdlineRoot 'latest'

Write-Host "SDK 根目录：$SdkRoot"

if (-not (Test-Path (Join-Path $cmdlineLatest 'bin\sdkmanager.bat'))) {
    if (-not (Test-Path $downloadDir)) {
        New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    }
    if (-not (Test-Path $zipPath)) {
        Write-Host "下载 commandline-tools（约 130 MB，来自 dl.google.com）…"
        Invoke-WebRequest -Uri $toolsZip -OutFile $zipPath -UseBasicParsing
    }

    Write-Host '解压…'
    $extractDir = Join-Path $downloadDir 'extract'
    if (Test-Path $extractDir) {
        Remove-Item -LiteralPath $extractDir -Recurse -Force
    }
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

    if (-not (Test-Path $cmdlineRoot)) {
        New-Item -ItemType Directory -Path $cmdlineRoot -Force | Out-Null
    }
    if (Test-Path $cmdlineLatest) {
        Remove-Item -LiteralPath $cmdlineLatest -Recurse -Force
    }
    Move-Item -LiteralPath (Join-Path $extractDir 'cmdline-tools') -Destination $cmdlineLatest
}

$sdkManager = Join-Path $cmdlineLatest 'bin\sdkmanager.bat'

if (-not $SkipLicenses) {
    Write-Host '接受 SDK 许可（自动回 y）…'
    # sdkmanager 需要一串 y；写死许可文件更省事，也就不用交互。
    $licenseDir = Join-Path $SdkRoot 'licenses'
    if (-not (Test-Path $licenseDir)) {
        New-Item -ItemType Directory -Path $licenseDir -Force | Out-Null
    }
    $androidHash = '24333f8a63b6825ea9c5514f83c2829b004d1fee'
    $licenseFile = Join-Path $licenseDir 'android-sdk-license'
    $existing = if (Test-Path $licenseFile) { [System.IO.File]::ReadAllText($licenseFile) } else { '' }
    if ($existing -notmatch [regex]::Escape($androidHash)) {
        [System.IO.File]::WriteAllText($licenseFile,
            $existing.TrimEnd() + "`n" + $androidHash + "`n",
            [System.Text.UTF8Encoding]::new($false))
    }
    Write-Host "已写入许可文件 $licenseFile"
}

Write-Host "安装 platform-tools / platforms;$Platform / build-tools;$BuildTools …"
$packages = @('platform-tools', "platforms;$Platform", "build-tools;$BuildTools")
& $sdkManager "--sdk_root=$SdkRoot" $packages
if ($LASTEXITCODE -ne 0) {
    throw "sdkmanager 安装失败（退出码 $LASTEXITCODE）"
}

& $sdkManager "--sdk_root=$SdkRoot" --list_installed

Write-Host ''
Write-Host '完成。接着跑：powershell -ExecutionPolicy Bypass -File build.ps1'
