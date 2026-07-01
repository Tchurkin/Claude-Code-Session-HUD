param(
    [string]$AliveFile = ""
)

# A small always-on-top "new chat" button (a Claude-style spark). Left-click focuses a
# VS Code window and sends Ctrl+Alt+N (bound to "Claude Code: Open in New Window"), so a
# new chat opens in its own window without you remembering the shortcut. Right-click hides.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_claude_button", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$CW = 44; $CH = 44; $GLOW = 12; $R = 10
$ACCENT = [System.Drawing.Color]::FromArgb(217, 119, 87)     # Claude clay/orange
$FORM_W = $CW + $GLOW*2; $FORM_H = $CH + $GLOW*2

$script:hot = $false; $script:closeReq = $false; $script:tick = 0
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W; $form.Height = $FORM_H
# The button rides just above the badge ("chat tab") stack; at the corner when there are none.
$ns = Join-Path $env:USERPROFILE ".claude\hal_voice\badges_stack"
$dockBottom = $screen.Bottom - 16
$GAPB = 8
$script:curTop    = $dockBottom - $GLOW - $CH
$script:targetTop = $script:curTop
$script:lastTop   = -99999
$form.Left = $screen.Right - $CW - 16 - $GLOW
$form.Top  = $script:curTop

function StackHeight {
    $now = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    $count = 0; $sum = 0
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($ns, "*.json")) {
            try { $d = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json } catch { continue }
            if ($d -and $d.beat -and (($now - [int64]$d.beat) -lt 1500)) { $count++; $sum += [int]$d.h }
        }
    } catch {}
    return @($count, $sum)
}

function RoundedPath($x, $y, $w, $h, $rad) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad*2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc(($x+$w-$d), $y, $d, $d, 270, 90)
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d, $d, 0, 90)
    $p.AddArc($x, ($y+$h-$d), $d, $d, 90, 90)
    $p.CloseFigure(); return $p
}
function CA($a, $c) { [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }

$render = {
    $acc = if ($script:hot) { [System.Drawing.Color]::FromArgb(240, 150, 120) } else { $ACCENT }
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    $gbase = if ($script:hot) { 170 } else { 120 }
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int]($gbase * [Math]::Exp(-$sp * 0.34))
        if ($alpha -lt 4) { continue }
        $gp = RoundedPath ($GLOW-$sp) ($GLOW-$sp) ($CW+$sp*2) ($CH+$sp*2) ([Math]::Min($R+$sp,16))
        $pen = New-Object System.Drawing.Pen((CA $alpha $acc), 1.5)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }

    $cpath = RoundedPath $GLOW $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(232, 20, 18, 17))
    $g.FillPath($bg, $cpath); $bg.Dispose()
    $bpen = New-Object System.Drawing.Pen((CA 210 $acc), 1.3)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose(); $cpath.Dispose()

    # Claude-style spark: rays of two lengths radiating from the center.
    $cx = $GLOW + $CW/2; $cy = $GLOW + $CH/2
    $penS = New-Object System.Drawing.Pen($acc, 2.4)
    $penS.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $penS.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
    $rays = 12
    for ($i = 0; $i -lt $rays; $i++) {
        $ang = $i * (2 * [Math]::PI / $rays)
        $len = if ($i % 2 -eq 0) { 12 } else { 6.5 }
        $x1 = $cx + [Math]::Cos($ang) * 3.5; $y1 = $cy + [Math]::Sin($ang) * 3.5
        $x2 = $cx + [Math]::Cos($ang) * (3.5 + $len); $y2 = $cy + [Math]::Sin($ang) * (3.5 + $len)
        $g.DrawLine($penS, [float]$x1, [float]$y1, [float]$x2, [float]$y2)
    }
    $penS.Dispose()

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 245)
    $bmp.Dispose()
}

$openNew = {
    $h = [PerPixelLayered]::FindWindowEndsWith("Visual Studio Code")
    if ($h -ne [IntPtr]::Zero) {
        [PerPixelLayered]::FocusWindow($h)
        Start-Sleep -Milliseconds 170
        [System.Windows.Forms.SendKeys]::SendWait("^%n")   # Ctrl+Alt+N -> Open in New Window
    }
}

$form.Add_MouseDown({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Right) { $script:closeReq = $true }
    else { & $openNew }
})
$form.Add_MouseEnter({ $script:hot = $true;  & $render })
$form.Add_MouseLeave({ $script:hot = $false; & $render })
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

$script:lastVs = NowMs
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 30
$timer.Add_Tick({
    if ($script:closeReq) { $form.Close(); return }
    $script:tick++
    if (($script:tick % 4) -eq 1) {                  # ~every 120ms: recompute where the stack tops out
        $info = StackHeight; $cnt = $info[0]; $sum = $info[1]
        if ($cnt -eq 0) { $bBottom = $dockBottom }
        else { $bBottom = $dockBottom - ($sum + ($cnt - 1) * $GAPB) - $GAPB }
        $script:targetTop = [int]($bBottom - $GLOW - $CH)
    }
    if (($script:tick % 33) -eq 0) {                 # ~every 1s: heartbeat + VS Code presence
        if ($AliveFile) { try { [System.IO.File]::WriteAllText($AliveFile, (NowMs).ToString()) } catch {} }
        if ([PerPixelLayered]::FindWindowEndsWith("Visual Studio Code") -ne [IntPtr]::Zero) { $script:lastVs = NowMs }
        elseif ((NowMs) - $script:lastVs -gt 30000) { $form.Close(); return }   # VS Code gone -> retire
    }
    $delta = $script:targetTop - $script:curTop
    if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:targetTop } else { $script:curTop += $delta * 0.22 }
    $newTop = [int]$script:curTop
    if ($newTop -ne $script:lastTop) {
        $script:lastTop = $newTop
        $form.Top = $newTop
        [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
    }
})
$timer.Start()

$form.Add_FormClosed({ if ($AliveFile) { try { Remove-Item -LiteralPath $AliveFile -ErrorAction SilentlyContinue } catch {} } })

[System.Windows.Forms.Application]::Run($form)
