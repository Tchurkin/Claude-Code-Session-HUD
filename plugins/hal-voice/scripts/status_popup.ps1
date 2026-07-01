param(
    [string]$Text       = "WORKING...",
    [int]   $DurationMs = 300000,
    [switch]$Loading,
    [int]   $AccentR    = 0,
    [int]   $AccentG    = 165,
    [int]   $AccentB    = 58,
    [string]$PidFile    = ""
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Per-pixel-alpha layered window + cross-process popup-stacking helpers.
. (Join-Path $PSScriptRoot 'popup_common.ps1')

$screen   = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
# Per-session PID file so this chat's caller can replace only ITS own status popup
# (other chats' popups live on and stack instead of being killed).
if ($PidFile) { try { [System.IO.File]::WriteAllText($PidFile, $PID.ToString()) } catch {} }

$CW=440; $R=6; $GLOW=16; $PAD_L=18; $PAD_T=10; $BAR_H=3
$ACCENT = [System.Drawing.Color]::FromArgb($AccentR, $AccentG, $AccentB)   # this chat's color

$mFont = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)

$tb = New-Object System.Drawing.Bitmap(1,1); $tg = [System.Drawing.Graphics]::FromImage($tb)
$meas = $tg.MeasureString($Text, $mFont, ($CW - $PAD_L - 30))
$tg.Dispose(); $tb.Dispose()
$textH = [int][Math]::Ceiling($meas.Height) + 4
$barRow = if ($Loading) { $BAR_H + 8 } else { 0 }
$CH = $PAD_T + $textH + $PAD_T + $barRow

$FORM_W = $CW + $GLOW*2
$FORM_H = $CH + $GLOW*2

$CS=10; $CXL = $GLOW + $CW - 24; $CYT = $GLOW + 10
$script:closeHot = $false
$script:barPos = 0; $script:barDir = 3
$TRACK_X = $GLOW + $PAD_L
$TRACK_W = $CW - $PAD_L*2
$FILL_W  = 84
$BAR_Y   = $GLOW + $CH - $BAR_H - 5

function CA($a,$c){ [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }
function RoundedPath($x,$y,$w,$h,$r){
    # square LEFT corners, rounded RIGHT corners
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r*2
    $p.AddLine($x, $y, ($x+$w-$r), $y)                  # top edge (from square top-left)
    $p.AddArc(($x+$w-$d), $y,         $d,$d, 270, 90)   # rounded top-right
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d,$d, 0,   90)   # rounded bottom-right
    $p.AddLine(($x+$w-$r), ($y+$h), $x, ($y+$h))        # bottom edge (to square bottom-left)
    $p.CloseFigure()                                    # left edge (square corners)
    return $p
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W
$form.Height = $FORM_H
# Stacking anchor: newest popup sits here; older ones slide below it (see the stack timer).
$GAP = 8
$script:baseTop   = $screen.Top + 36 - $GLOW
$script:curTop    = $script:baseTop
$script:targetTop = $script:baseTop
$script:lastTop   = $script:baseTop
$script:tick      = 0
$form.Left   = $screen.Right - $CW - 20 - $GLOW
$form.Top    = $script:baseTop

$render = {
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    # Outer glow on TOP / RIGHT / BOTTOM only - clip out the left margin so the left
    # edge reads as a solid green border instead of glow.
    $glowClip = New-Object System.Drawing.RectangleF ([float]$GLOW, 0, [float]($FORM_W - $GLOW), [float]$FORM_H)
    $g.SetClip($glowClip)
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int](150 * [Math]::Exp(-$sp * 0.30))
        if ($alpha -lt 4) { continue }
        $x=$GLOW-$sp; $y=$GLOW-$sp; $w=$CW+$sp*2; $h=$CH+$sp*2; $r=[Math]::Min($R+$sp,14)
        $gp  = RoundedPath $x $y $w $h $r
        $pen = New-Object System.Drawing.Pen((CA $alpha $ACCENT), 1.5)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }
    $g.ResetClip()

    $cpath = RoundedPath $GLOW $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(220, 17, 17, 17))
    $g.FillPath($bg, $cpath); $bg.Dispose()

    # Solid green LEFT border (~2x the old accent strip), clipped to the content shape
    $g.SetClip($cpath)
    $sb = New-Object System.Drawing.SolidBrush $ACCENT
    $g.FillRectangle($sb, $GLOW, $GLOW, 8, $CH); $sb.Dispose()
    $g.ResetClip()

    $bpen = New-Object System.Drawing.Pen((CA 165 $ACCENT), 1.2)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose()
    $cpath.Dispose()

    $mb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(178,178,178))
    $rect = New-Object System.Drawing.RectangleF ([float]($GLOW+$PAD_L), [float]($GLOW+$PAD_T), [float]($CW-$PAD_L-30), [float]$textH)
    $g.DrawString($Text, $mFont, $mb, $rect); $mb.Dispose()

    if ($Loading) {
        $trk = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(36,36,36))
        $g.FillRectangle($trk, $TRACK_X, $BAR_Y, $TRACK_W, $BAR_H); $trk.Dispose()
        $fil = New-Object System.Drawing.SolidBrush $ACCENT
        $g.FillRectangle($fil, ($TRACK_X + $script:barPos), $BAR_Y, $FILL_W, $BAR_H); $fil.Dispose()
    }

    $cc = if ($script:closeHot) { 240 } else { 130 }
    $xp = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb($cc,190,190,190)), 1.7
    $g.DrawLine($xp, $CXL, $CYT, ($CXL+$CS), ($CYT+$CS))
    $g.DrawLine($xp, ($CXL+$CS), $CYT, $CXL, ($CYT+$CS))
    $xp.Dispose()

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 240)
    $bmp.Dispose()
}

function HitClose($x,$y){ ($x -ge ($CXL-7)) -and ($x -le ($CXL+$CS+7)) -and ($y -ge ($CYT-7)) -and ($y -le ($CYT+$CS+7)) }

$form.Add_MouseDown({ $form.Close() })   # click anywhere to dismiss
$form.Add_MouseMove({
    param($s,$e)
    $h = HitClose $e.X $e.Y
    if ($h -ne $script:closeHot) { $script:closeHot = $h; if (-not $Loading) { & $render } }
})
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

if ($Loading) {
    $anim = New-Object System.Windows.Forms.Timer
    $anim.Interval = 16
    $anim.Add_Tick({
        $script:barPos += $script:barDir
        if (($script:barPos + $FILL_W) -ge $TRACK_W) { $script:barDir = -3 }
        if ($script:barPos -le 0)                     { $script:barDir =  3 }
        & $render
    })
    $anim.Start()
}

# Stacking. The slot only changes when popups appear/disappear, so we hit the shared
# registry just ~8x/sec; every frame we just ease toward the target and slide the window
# with a cheap SetWindowPos (no GDI redraw). The already-blitted content slides with it.
$stackTimer = New-Object System.Windows.Forms.Timer
$stackTimer.Interval = 20
$stackTimer.Add_Tick({
    $script:tick++
    if (($script:tick % 6) -eq 1) {
        $ordered = Stack-Sync $CH $true
        $script:targetTop = Stack-TargetTop $script:baseTop $GAP $ordered
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
$stackTimer.Start()

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $DurationMs
$timer.Add_Tick({ $form.Close() })
$timer.Start()

$form.Add_FormClosed({
    try { Stack-Sync $CH $false } catch {}
    if ($PidFile) { try { Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue } catch {} }
})

[System.Windows.Forms.Application]::Run($form)
