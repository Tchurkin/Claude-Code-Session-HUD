param(
    [string]$Text = "PROJECT DOMINATED!!",
    [int]$DurationMs = 4500,
    [int]$AccentR = 0,
    [int]$AccentG = 215,
    [int]$AccentB = 80
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# Per-pixel-alpha layered window + cross-process popup-stacking helpers.
. (Join-Path $PSScriptRoot 'popup_common.ps1')

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea

$CW=440; $R=6; $GLOW=16; $PAD_L=18; $PAD_T=11; $HDR_H=18
$ACCENT = [System.Drawing.Color]::FromArgb($AccentR, $AccentG, $AccentB)

$hdrFont = New-Object System.Drawing.Font("Segoe UI", 8,  [System.Drawing.FontStyle]::Bold)
$msgFont = New-Object System.Drawing.Font("Segoe UI", 13, [System.Drawing.FontStyle]::Bold)

# Measure message height
$tb = New-Object System.Drawing.Bitmap(1,1); $tg = [System.Drawing.Graphics]::FromImage($tb)
$meas = $tg.MeasureString($Text, $msgFont, ($CW - $PAD_L - 30))
$tg.Dispose(); $tb.Dispose()
$textH = [int][Math]::Ceiling($meas.Height) + 4
$CH = $PAD_T + $HDR_H + 6 + $textH + $PAD_T

$FORM_W = $CW + $GLOW*2
$FORM_H = $CH + $GLOW*2

# Close-X geometry (bitmap coords)
$CS=10; $CXL = $GLOW + $CW - 24; $CYT = $GLOW + 12
$script:closeHot = $false

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
        $alpha = [int](190 * [Math]::Exp(-$sp * 0.28))
        if ($alpha -lt 4) { continue }
        $x=$GLOW-$sp; $y=$GLOW-$sp; $w=$CW+$sp*2; $h=$CH+$sp*2; $r=[Math]::Min($R+$sp,14)
        $gp  = RoundedPath $x $y $w $h $r
        $pen = New-Object System.Drawing.Pen((CA $alpha $ACCENT), 1.5)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }
    $g.ResetClip()

    # Content: semi-transparent dark fill
    $cpath = RoundedPath $GLOW $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(224, 18, 18, 18))
    $g.FillPath($bg, $cpath); $bg.Dispose()

    # Solid green LEFT border (~2x the old accent strip), clipped to the content shape
    $g.SetClip($cpath)
    $sb = New-Object System.Drawing.SolidBrush $ACCENT
    $g.FillRectangle($sb, $GLOW, $GLOW, 10, $CH); $sb.Dispose()
    $g.ResetClip()

    # Border
    $bpen = New-Object System.Drawing.Pen((CA 215 $ACCENT), 1.3)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose()
    $cpath.Dispose()

    # Header
    $hb = New-Object System.Drawing.SolidBrush $ACCENT
    $g.DrawString("CLAUDE CODE", $hdrFont, $hb, [float]($GLOW+$PAD_L), [float]($GLOW+$PAD_T)); $hb.Dispose()

    # Message
    $mb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(243,243,243))
    $rect = New-Object System.Drawing.RectangleF ([float]($GLOW+$PAD_L), [float]($GLOW+$PAD_T+$HDR_H+4), [float]($CW-$PAD_L-30), [float]$textH)
    $g.DrawString($Text, $msgFont, $mb, $rect); $mb.Dispose()

    # Close X
    $cc = if ($script:closeHot) { 245 } else { 150 }
    $xp = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb($cc,205,205,205)), 1.8
    $g.DrawLine($xp, $CXL, $CYT, ($CXL+$CS), ($CYT+$CS))
    $g.DrawLine($xp, ($CXL+$CS), $CYT, $CXL, ($CYT+$CS))
    $xp.Dispose()

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 244)
    $bmp.Dispose()
}

function HitClose($x,$y){ ($x -ge ($CXL-7)) -and ($x -le ($CXL+$CS+7)) -and ($y -ge ($CYT-7)) -and ($y -le ($CYT+$CS+7)) }

$form.Add_MouseDown({ $form.Close() })   # click anywhere to dismiss
$form.Add_MouseMove({
    param($s,$e)
    $h = HitClose $e.X $e.Y
    if ($h -ne $script:closeHot) { $script:closeHot = $h; & $render }
})
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

# Stacking. The slot only changes when popups appear/disappear, so we hit the shared
# registry just ~8x/sec; every frame we just ease toward the target and slide the window
# with a cheap SetWindowPos (no GDI redraw) - that keeps the push-down buttery.
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

$form.Add_FormClosed({ try { Stack-Sync $CH $false } catch {} })

[System.Windows.Forms.Application]::Run($form)
