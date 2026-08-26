param(
    [string]$AliveFile = ""
)

# The dock handle: a thin chevron hugging the right edge that slides the whole HUD off screen and
# back, and flips itself as it goes.
#
# There is already an on/off switch in VS Code, but that retires every overlay - it is for "I am not
# using this today", not for "get out of the way for a minute". This keeps everything running and
# just parks it off the edge, so a chat that needs you still has a tab waiting when you pull it back.
#
# The dock keeps a 30px lane at the right edge for it. That is wider than it looks like it needs,
# because a tab's glow reaches 12px past its chip and glow is not transparent - and a layered
# window is hit-tested on alpha, so a handle tucked under that glow would never see a click.
# Flush to the screen edge is also the easiest thing there is to hit: no aiming required.
#
# It is deliberately its OWN window rather than part of the meter. It has to stay put while
# everything else leaves, and the meter both slides and rides up and down on the tab stack, so a
# handle drawn by it would wander off with the thing it is supposed to bring back.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
if (-not $script:PplReady) { exit 1 }

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_dock_handle", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
# Not $G: PowerShell variable names are case-insensitive, so a constant called $G and the
# Graphics object called $g are the same variable, and the second one to be assigned wins.
$HG = 8                     # glow margin around the strip
$W = 18; $H = 48            # the visible strip, filling the 30px lane the dock leaves for it
$FORM_W = $W + $HG*2; $FORM_H = $H + $HG*2

$script:closeReq = $false
$script:hot = $false
$script:flip = 0.0          # 0 = pointing right (push it away), 1 = pointing left (pull it back)
$script:lastDrawn = -99.0
$script:lastHot = $false
$script:lastFrame = 0
$script:lastBeat = 0
$script:lastPresence = 0
$script:curInterval = 200
$script:lbWas = $false
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width = $FORM_W; $form.Height = $FORM_H
$form.Left = [int]($screen.Right - $W - $HG)          # flush right, glow hanging off the edge
$form.Top  = [int]($screen.Bottom - 44 - $H - $HG)    # bottom of the dock, above the status bar

$script:flip = Dock-Phase

function InStrip {
    # No right-hand bound: the strip runs to the screen edge and the cursor cannot go further, so
    # capping it would only create a dead column at the very edge - exactly where you aim.
    $cp = [System.Windows.Forms.Cursor]::Position
    $x = $form.Left + $HG; $y = $form.Top + $HG
    return ($cp.X -ge $x -and $cp.Y -ge $y -and $cp.Y -lt ($y + $H))
}

$render = {
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    # A pill rounded on the left only - it reads as something attached to the screen edge rather
    # than a button floating near it, which is what it is.
    $a = if ($script:hot) { 235 } else { 170 }
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $r = 6
    # Runs past the right of the canvas on purpose. The screen clips it, so there is no visible right
    # edge - it reads as attached to the edge rather than parked near it - and, because a layered
    # window is hit-tested on alpha, the very last column of pixels on the screen is still the button.
    # You can throw the pointer at the edge without aiming, which is the whole point of putting it there.
    $right = $HG + $W + $HG
    $path.AddArc($HG, $HG, $r*2, $r*2, 180, 90)
    $path.AddLine($right, $HG, $right, ($HG + $H))
    $path.AddArc($HG, ($HG + $H - $r*2), $r*2, $r*2, 90, 90)
    $path.CloseFigure()
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($a, 26, 26, 28))
    $g.FillPath($bg, $path); $bg.Dispose()
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(($a - 60), 200, 200, 210)), 1
    $g.DrawPath($pen, $path); $pen.Dispose(); $path.Dispose()

    # The chevron, flipped by scaling it horizontally through zero: at the halfway point it is
    # edge-on and reads as a card turning over, which is exactly what is happening to it.
    $cx = $HG + $W / 2.0; $cy = $HG + $H / 2.0
    $sx = [Math]::Cos([Math]::PI * $script:flip)
    $armX = 3.0 * $sx; $armY = 5.0
    $ink = if ($script:hot) { [System.Drawing.Color]::FromArgb(244,244,248) }
           else { [System.Drawing.Color]::FromArgb(196,196,204) }
    $cp = New-Object System.Drawing.Pen $ink, 1.7
    $cp.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $cp.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
    $cp.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $pts = New-Object 'System.Drawing.PointF[]' 3
    $pts[0] = [System.Drawing.PointF]::new([single]($cx - $armX), [single]($cy - $armY))
    $pts[1] = [System.Drawing.PointF]::new([single]($cx + $armX), [single]$cy)
    $pts[2] = [System.Drawing.PointF]::new([single]($cx - $armX), [single]($cy + $armY))
    $g.DrawLines($cp, $pts); $cp.Dispose()

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 255)
    Assert-Topmost $form
    $bmp.Dispose()
}

$form.Add_HandleCreated({ [PerPixelLayered]::NoActivate($form.Handle) })
$form.Add_Shown({ [PerPixelLayered]::InitClickable($form.Handle); Assert-Topmost $form; & $render })
$form.Add_MouseDown({
    param($sender, $e)
    # Either button, same as the readout: nothing else claims a right-click out here.
    if (-not (InStrip)) { return }
    $script:lbWas = $true
    Set-DockStowed (-not (Dock-Stowed))
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 200
$timer.Add_Tick({
    if ($script:closeReq) { $form.Close(); return }
    $nowMs = NowMs

    # The same curve the dock travels on, so the chevron turns over exactly as the panel leaves -
    # not approximately, identically. It is the same function of the same clock.
    $script:flip = Dock-Phase

    $over = InStrip
    if ($over -ne $script:hot) { $script:hot = $over }
    if ([Math]::Abs($script:flip - $script:lastDrawn) -ge 0.01 -or $script:hot -ne $script:lastHot) {
        $script:lastDrawn = $script:flip; $script:lastHot = $script:hot
        & $render
    }

    if ($nowMs - $script:lastPresence -ge 1000) {
        $script:lastPresence = $nowMs
        if (-not (Hud-Enabled)) { $form.Close(); return }
        if ([PerPixelLayered]::FindWindowEndsWith("Visual Studio Code") -ne [IntPtr]::Zero) { $script:lastVs = NowMs }
        elseif ($script:lastVs -and ($nowMs - $script:lastVs) -gt 30000) { $form.Close(); return }
        Assert-Topmost $form
        Poll-HudDaemon
    }

    $want = if (Dock-Moving) { 15 } elseif ($script:hot) { 60 } else { 200 }
    if ($want -ne $script:curInterval) { $script:curInterval = $want; $timer.Interval = $want }
    if ($nowMs - $script:lastBeat -ge 600) { $script:lastBeat = $nowMs; Write-Beat $AliveFile }
})
$script:lastVs = NowMs
$timer.Start()

$form.Add_FormClosed({
    try { Remove-Item -LiteralPath $AliveFile -ErrorAction SilentlyContinue } catch {}
})

[System.Windows.Forms.Application]::Run($form)
