param(
    [string]$AliveFile = ""
)

# A tiny always-on-top on/off switch for the whole HUD, parked in the bottom-right corner
# (a plugin can't add a real VS Code status-bar item, so we draw our own). Click to toggle:
# green power icon = on, dim gray = off. When off, the other overlays (badges, spark button,
# window tint) see the flag via Hud-Enabled and close themselves; this button stays so you can
# switch it back on. Left-click toggles; hover shows a hint.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_toggle", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$CW = 30; $CH = 30; $GLOW = 10; $R = 8; $TIP_W = 92; $OX = $TIP_W
$FORM_W = $CW + $GLOW*2 + $TIP_W; $FORM_H = $CH + $GLOW*2
$tipFont = New-Object System.Drawing.Font("Segoe UI", 9)

$ON  = [System.Drawing.Color]::FromArgb(0, 210, 90)      # green when enabled
$OFF = [System.Drawing.Color]::FromArgb(120, 120, 128)   # gray when disabled

$script:hot = $false; $script:closeReq = $false; $script:tick = 0
$script:enabled = Hud-Enabled
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W; $form.Height = $FORM_H
$form.Left = $screen.Right - $CW - 14 - $GLOW - $TIP_W    # bottom-right corner; canvas extends left for the hint
$form.Top  = $screen.Bottom - $CH - 12 - $GLOW

function CA($a, $c) { [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }
function RoundedPath($x, $y, $w, $h, $rad) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad*2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc(($x+$w-$d), $y, $d, $d, 270, 90)
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d, $d, 0, 90)
    $p.AddArc($x, ($y+$h-$d), $d, $d, 90, 90)
    $p.CloseFigure(); return $p
}

$render = {
    $acc = if ($script:enabled) { $ON } else { $OFF }
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint  = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    $gbase = if ($script:hot) { 150 } else { if ($script:enabled) { 95 } else { 45 } }
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int]($gbase * [Math]::Exp(-$sp * 0.36))
        if ($alpha -lt 4) { continue }
        $gp = RoundedPath ($GLOW+$OX-$sp) ($GLOW-$sp) ($CW+$sp*2) ($CH+$sp*2) ([Math]::Min($R+$sp,14))
        $pen = New-Object System.Drawing.Pen((CA $alpha $acc), 1.4)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }

    $cpath = RoundedPath ($GLOW+$OX) $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(234, 20, 20, 22))
    $g.FillPath($bg, $cpath); $bg.Dispose()
    $borderA = if ($script:enabled) { 210 } else { 150 }
    $bpen = New-Object System.Drawing.Pen((CA $borderA $acc), 1.3)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose(); $cpath.Dispose()

    # power glyph: an arc ring with a gap at top + a vertical stem
    $cx = $GLOW + $OX + $CW/2; $cy = $GLOW + $CH/2
    $pen = New-Object System.Drawing.Pen($acc, 2.0)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
    $rad = 6.5
    $g.DrawArc($pen, [float]($cx-$rad), [float]($cy-$rad+1), [float]($rad*2), [float]($rad*2), 300, 300)
    $g.DrawLine($pen, [float]$cx, [float]($cy-8), [float]$cx, [float]($cy-1))
    $pen.Dispose()

    if ($script:hot) {
        $tip = if ($script:enabled) { "HUD: on" } else { "HUD: off" }
        $tw  = [int][Math]::Ceiling($g.MeasureString($tip, $tipFont).Width)
        $tbw = $tw + 16; $tbh = [int]$tipFont.Height + 8
        $tbx = $GLOW + $OX - 10 - $tbw
        if ($tbx -lt 2) { $tbx = 2 }
        $tby = $GLOW + [int](($CH - $tbh)/2)
        $tp = RoundedPath $tbx $tby $tbw $tbh 5
        $tbg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(240, 22, 22, 24))
        $g.FillPath($tbg, $tp); $tbg.Dispose()
        $tpen = New-Object System.Drawing.Pen((CA 130 $acc), 1)
        $g.DrawPath($tpen, $tp); $tpen.Dispose(); $tp.Dispose()
        $ttb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(237,237,241))
        $g.DrawString($tip, $tipFont, $ttb, [float]($tbx + 8), [float]($tby + 4)); $ttb.Dispose()
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 245)
    $bmp.Dispose()
}

$form.Add_MouseDown({
    param($s, $e)
    $script:enabled = -not $script:enabled
    Set-HudEnabled $script:enabled
    & $render
})
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 30
$timer.Add_Tick({
    if ($script:closeReq) { $form.Close(); return }
    $script:tick++
    if (($script:tick % 33) -eq 0) {          # ~every 1s: heartbeat + pick up external flag changes
        if ($AliveFile) { try { [System.IO.File]::WriteAllText($AliveFile, (NowMs).ToString()) } catch {} }
        $ext = Hud-Enabled
        if ($ext -ne $script:enabled) { $script:enabled = $ext; & $render }
    }
    # hover (cursor-rect poll)
    $bl = $form.Left + $GLOW + $OX; $bt = $form.Top + $GLOW
    $cp = [System.Windows.Forms.Cursor]::Position
    $over = ($cp.X -ge $bl -and $cp.X -lt ($bl + $CW) -and $cp.Y -ge $bt -and $cp.Y -lt ($bt + $CH))
    if ($over -ne $script:hot) { $script:hot = $over; & $render }
})
$timer.Start()

$form.Add_FormClosed({ if ($AliveFile) { try { Remove-Item -LiteralPath $AliveFile -ErrorAction SilentlyContinue } catch {} } })

[System.Windows.Forms.Application]::Run($form)
