# 生成启动图标 PNG（纯色圆角方块 + 字母 Q）。
#
# 用脚本生成而不是往仓库里塞二进制图片，好处是仓库保持全文本、图标随时可改。
# 只在 build.ps1 发现图标缺失时调用，也可以单独跑：powershell -File tools\make-icon.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Drawing

$resDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'app\src\main\res'

# 密度目录 -> 边长像素
$sizes = [ordered]@{
    'mipmap-mdpi'    = 48
    'mipmap-hdpi'    = 72
    'mipmap-xhdpi'   = 96
    'mipmap-xxhdpi'  = 144
    'mipmap-xxxhdpi' = 192
}

$background = [System.Drawing.Color]::FromArgb(255, 21, 101, 192)
$foreground = [System.Drawing.Color]::White

foreach ($entry in $sizes.GetEnumerator()) {
    $dir = Join-Path $resDir $entry.Key
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $size = $entry.Value
    $bitmap = New-Object System.Drawing.Bitmap($size, $size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
        $graphics.Clear([System.Drawing.Color]::Transparent)

        # 圆角方块
        $radius = [Math]::Max(2, [int]($size / 5))
        $rect = New-Object System.Drawing.Rectangle(0, 0, $size, $size)
        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $d = $radius * 2
        $path.AddArc($rect.X, $rect.Y, $d, $d, 180, 90)
        $path.AddArc($rect.Right - $d, $rect.Y, $d, $d, 270, 90)
        $path.AddArc($rect.Right - $d, $rect.Bottom - $d, $d, $d, 0, 90)
        $path.AddArc($rect.X, $rect.Bottom - $d, $d, $d, 90, 90)
        $path.CloseFigure()
        $brush = New-Object System.Drawing.SolidBrush($background)
        $graphics.FillPath($brush, $path)

        # 字母 Q
        $fontSize = [float]($size * 0.62)
        $font = New-Object System.Drawing.Font('Segoe UI', $fontSize, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
        $textBrush = New-Object System.Drawing.SolidBrush($foreground)
        $format = New-Object System.Drawing.StringFormat
        $format.Alignment = [System.Drawing.StringAlignment]::Center
        $format.LineAlignment = [System.Drawing.StringAlignment]::Center
        $box = New-Object System.Drawing.RectangleF(0, 0, [float]$size, [float]$size)
        $graphics.DrawString('Q', $font, $textBrush, $box, $format)

        $target = Join-Path $dir 'ic_launcher.png'
        $bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
        Write-Host "已生成 $target ($size x $size)"
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}
