# 不用 Gradle，直接调 aapt2 / javac / d8 / zipalign / apksigner 打出 debug APK。
#
# 前置：tools\install-sdk.ps1 装好 SDK，机器上有 JDK（17 及以上都行）。
# 用法：
#   powershell -ExecutionPolicy Bypass -File build.ps1
#   powershell -ExecutionPolicy Bypass -File build.ps1 -SkipSmoke -Clean
#
# 产物：android\build\qinan-agent-debug.apk

[CmdletBinding()]
param(
    [string]$SdkRoot = $(if ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } else { "$env:LOCALAPPDATA\Android\Sdk" }),
    [string]$Platform = "android-34",
    [string]$BuildTools = "34.0.0",
    [int]$MinSdk = 24,
    [int]$TargetSdk = 34,
    [string]$VersionName = "1.0",
    [int]$VersionCode = 1,
    [string]$JsonJarUrl = "https://repo1.maven.org/maven2/org/json/json/20240303/json-20240303.jar",
    [switch]$SkipSmoke,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = $PSScriptRoot
$Main = Join-Path $Root 'app\src\main'
$Build = Join-Path $Root 'build'

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Invoke-Tool([string]$File, [string[]]$Arguments, [string]$What) {
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What 失败（$File 退出码 $LASTEXITCODE）"
    }
}

function Find-JavaHome {
    if ($env:JAVA_HOME -and (Test-Path (Join-Path $env:JAVA_HOME 'bin\javac.exe'))) {
        return $env:JAVA_HOME
    }
    $roots = @(
        'C:\Program Files\Eclipse Adoptium',
        'C:\Program Files\Java',
        'C:\Program Files\Microsoft',
        'C:\Program Files\Android\Android Studio\jbr'
    )
    $candidates = @()
    foreach ($root in $roots) {
        if (Test-Path $root) {
            $candidates += (Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | ForEach-Object { $_.FullName })
            $candidates += $root
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate 'bin\javac.exe')) {
            return $candidate
        }
    }
    throw '找不到 JDK。装一个 JDK 17+ 或设置 JAVA_HOME 后重试。'
}

if ($Clean -and (Test-Path $Build)) {
    Write-Step "清理 $Build"
    Remove-Item -LiteralPath $Build -Recurse -Force
}

$JavaHome = Find-JavaHome
$env:JAVA_HOME = $JavaHome
$javac = Join-Path $JavaHome 'bin\javac.exe'
$java = Join-Path $JavaHome 'bin\java.exe'
$keytool = Join-Path $JavaHome 'bin\keytool.exe'
Write-Host "JDK: $JavaHome"

$aapt2 = Join-Path $SdkRoot "build-tools\$BuildTools\aapt2.exe"
$d8 = Join-Path $SdkRoot "build-tools\$BuildTools\d8.bat"
$zipalign = Join-Path $SdkRoot "build-tools\$BuildTools\zipalign.exe"
$apksigner = Join-Path $SdkRoot "build-tools\$BuildTools\apksigner.bat"
$androidJar = Join-Path $SdkRoot "platforms\$Platform\android.jar"

foreach ($tool in @($aapt2, $d8, $zipalign, $apksigner, $androidJar)) {
    if (-not (Test-Path $tool)) {
        throw "缺少 $tool —— 先跑 tools\install-sdk.ps1（或用 -SdkRoot 指定 SDK 位置）"
    }
}
Write-Host "SDK: $SdkRoot"

New-Item -ItemType Directory -Path $Build -Force | Out-Null
$classesDir = Join-Path $Build 'classes'
$genDir = Join-Path $Build 'gen'
$dexDir = Join-Path $Build 'dex'
$libsDir = Join-Path $Build 'libs'
$resZip = Join-Path $Build 'resources.zip'
$baseApk = Join-Path $Build 'base.apk'
$alignedApk = Join-Path $Build 'aligned.apk'
$finalApk = Join-Path $Build 'qinan-agent-debug.apk'

# ---------- 图标 ----------

$icon = Join-Path $Main 'res\mipmap-xxhdpi\ic_launcher.png'
if (-not (Test-Path $icon)) {
    Write-Step '生成启动图标'
    # 注意：调 PowerShell 脚本不会更新 $LASTEXITCODE，所以这里只能靠异常传播
    # （make-icon.ps1 自己开了 ErrorActionPreference = Stop）。
    try {
        & (Join-Path $Root 'tools\make-icon.ps1')
    } catch {
        throw "生成图标失败：$($_.Exception.Message)"
    }
}

# ---------- 桌面冒烟测试 ----------

if (-not $SkipSmoke) {
    Write-Step '冒烟测试（桌面 JVM 上验证请求格式与错误解析）'
    if (-not (Test-Path $libsDir)) {
        New-Item -ItemType Directory -Path $libsDir -Force | Out-Null
    }
    # org.json 在桌面上不是 JDK 自带的，需要单独下一个小 jar（约 70 KB）。
    $jsonJar = Join-Path $libsDir 'json-20240303.jar'
    if (-not (Test-Path $jsonJar)) {
        Write-Host "下载 $JsonJarUrl"
        Invoke-WebRequest -Uri $JsonJarUrl -OutFile $jsonJar -UseBasicParsing
    }

    $smokeDir = Join-Path $Build 'smoke'
    if (Test-Path $smokeDir) {
        Remove-Item -LiteralPath $smokeDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $smokeDir -Force | Out-Null

    # 故意不加 android.jar，并且排除 MainActivity：如果哪天往 ApiClient / TaskPayload
    # 里混进了 android.* 引用，这里就会编译失败。
    $smokeSources = @(Get-ChildItem -LiteralPath (Join-Path $Main 'java') -Recurse -Filter *.java |
        Where-Object { $_.Name -ne 'MainActivity.java' } |
        ForEach-Object { $_.FullName })
    $smokeSources += (Join-Path $Root 'tools\SmokeTest.java')
    Invoke-Tool $javac (@('-encoding', 'UTF-8', '-nowarn', '-d', $smokeDir, '-cp', $jsonJar) + $smokeSources) '冒烟测试编译'
    Invoke-Tool $java @('-Dfile.encoding=UTF-8', '-cp', "$smokeDir;$jsonJar", 'com.qinan.agent.SmokeTest') '冒烟测试运行'
}

# ---------- 编译资源 ----------

Write-Step '编译资源（aapt2 compile）'
$resDir = Join-Path $Main 'res'
if (Test-Path $resZip) {
    Remove-Item -LiteralPath $resZip -Force
}
Invoke-Tool $aapt2 @('compile', '--dir', $resDir, '-o', $resZip) 'aapt2 compile'

Write-Step '链接资源与清单（aapt2 link）'
if (Test-Path $genDir) {
    Remove-Item -LiteralPath $genDir -Recurse -Force
}
New-Item -ItemType Directory -Path $genDir -Force | Out-Null
Invoke-Tool $aapt2 @(
    'link', '-o', $baseApk,
    '-I', $androidJar,
    '--manifest', (Join-Path $Main 'AndroidManifest.xml'),
    '-R', $resZip,
    '--java', $genDir,
    '--min-sdk-version', $MinSdk,
    '--target-sdk-version', $TargetSdk,
    '--version-code', $VersionCode,
    '--version-name', $VersionName,
    '--auto-add-overlay'
) 'aapt2 link'

# ---------- 编译 Java ----------

Write-Step '编译 Java（javac）'
if (Test-Path $classesDir) {
    Remove-Item -LiteralPath $classesDir -Recurse -Force
}
New-Item -ItemType Directory -Path $classesDir -Force | Out-Null
$javaSources = @(Get-ChildItem -LiteralPath (Join-Path $Main 'java') -Recurse -Filter *.java |
    ForEach-Object { $_.FullName })
$javaSources += @(Get-ChildItem -LiteralPath $genDir -Recurse -Filter *.java |
    ForEach-Object { $_.FullName })
# 这里用 --release 8 + -classpath android.jar，而不是 -source/-target 8 + -bootclasspath android.jar。
# 原因：android.jar 里没有 java.lang.invoke.LambdaMetafactory，一旦拿它当 bootclasspath，
# javac 编译 lambda（MainActivity 里全是 lambda）就会报「找不到符号 metafactory」。
# --release 8 走 JDK 自带的 ct.sym（老版本 API 签名），java.* 从 JDK 取、android.* 从
# classpath 取，两边都能解析。
Invoke-Tool $javac (@(
    '-encoding', 'UTF-8',
    '--release', '8',
    '-classpath', $androidJar,
    '-Xlint:-options',
    '-nowarn',
    '-d', $classesDir
) + $javaSources) 'javac'

# ---------- dex ----------

Write-Step '转 dex（d8）'
if (Test-Path $dexDir) {
    Remove-Item -LiteralPath $dexDir -Recurse -Force
}
New-Item -ItemType Directory -Path $dexDir -Force | Out-Null
$classFiles = @(Get-ChildItem -LiteralPath $classesDir -Recurse -Filter *.class |
    ForEach-Object { $_.FullName })
Invoke-Tool $d8 (@(
    '--release',
    '--min-api', $MinSdk,
    '--lib', $androidJar,
    '--output', $dexDir
) + $classFiles) 'd8'

# ---------- 组包 ----------

Write-Step '把 classes.dex 放进 APK'
# PowerShell 5.1 要显式把这两个程序集都加载进来，否则找不到 ZipArchiveMode 这个类型。
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$apk = [System.IO.Compression.ZipFile]::Open($baseApk, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    $dexFile = Join-Path $dexDir 'classes.dex'
    if (-not (Test-Path $dexFile)) {
        throw "d8 没有产出 $dexFile"
    }
    [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $apk, $dexFile, 'classes.dex', [System.IO.Compression.CompressionLevel]::Optimal)
} finally {
    $apk.Dispose()
}

Write-Step '对齐（zipalign）'
if (Test-Path $alignedApk) {
    Remove-Item -LiteralPath $alignedApk -Force
}
Invoke-Tool $zipalign @('-f', '-p', '4', $baseApk, $alignedApk) 'zipalign'

# ---------- 签名 ----------

Write-Step '签名（apksigner，debug keystore）'
$keystore = Join-Path $Build 'debug.keystore'
if (-not (Test-Path $keystore)) {
    Invoke-Tool $keytool @(
        '-genkeypair',
        '-keystore', $keystore,
        '-storepass', 'android',
        '-keypass', 'android',
        '-alias', 'androiddebugkey',
        '-keyalg', 'RSA',
        '-keysize', '2048',
        '-validity', '10000',
        '-dname', 'CN=Android Debug,O=Android,C=US',
        '-noprompt'
    ) 'keytool'
}
if (Test-Path $finalApk) {
    Remove-Item -LiteralPath $finalApk -Force
}
Invoke-Tool $apksigner @(
    'sign',
    '--ks', $keystore,
    '--ks-pass', 'pass:android',
    '--key-pass', 'pass:android',
    '--ks-key-alias', 'androiddebugkey',
    '--out', $finalApk,
    $alignedApk
) 'apksigner'
Invoke-Tool $apksigner @('verify', '--print-certs', $finalApk) 'apksigner verify'

# ---------- 收尾 ----------

Write-Step '产物信息'
# 走一次管道：aapt2 dump 会往 stderr 打警告，PowerShell 5.1 下原生命令写 stderr 会让 $?
# 变成 false，`powershell -File build.ps1` 就会以退出码 1 结束（CI 里会误判失败）。
& $aapt2 @('dump', 'badging', $finalApk) 2>&1 | Out-Host

$size = (Get-Item $finalApk).Length
Write-Host ''
Write-Host ("APK: {0}  ({1:N0} 字节 / {2:N0} KB)" -f $finalApk, $size, ($size / 1KB)) -ForegroundColor Green
Write-Host '装机：adb install -r "' -NoNewline
Write-Host $finalApk -NoNewline
Write-Host '"'

# 上面所有真实失败都会 throw，走到这里就是全绿，显式给 0，避免被上一条命令的 $? 带偏。
exit 0
