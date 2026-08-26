param(
    [string]$Title      = "Claude",            # session name (LLM 1-3 word label)
    [string]$Body       = "Waiting for you",   # what it's waiting on
    [int]   $AccentR    = 0,                    # this session's color
    [int]   $AccentG    = 215,
    [int]   $AccentB    = 80,
    [int64] $Hwnd       = 0,                    # click -> focus this chat's window
    [int]   $DurationMs = 9000,                 # auto-dismiss (paused while hovered)
    [string]$PidFile    = "",                   # when set, record our PID so a chat can replace its own card
    [string]$Chat       = "",                   # the chat's editor-tab label: click switches to it
    [string]$Sid        = ""
)

# An on-screen "a session needs you" card we draw ourselves - always-on-top, can't be
# suppressed by Windows notification settings/Focus Assist, and colored to match the session
# badge. Top-right; multiple popups stack downward (newest on top). Reuses the layered-window
# + cross-process stacking helpers in popup_common.ps1.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
if (-not $script:PplReady) { exit 1 }   # no drawing type -> exit so the supervisor respawns a working one

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$ACCENT = [System.Drawing.Color]::FromArgb($AccentR, $AccentG, $AccentB)

$CW=360; $R=6; $GLOW=16; $PAD_L=20; $PAD_R=16; $PAD_T=12; $BAR_W=8; $GAP_TB=3
$TIP_W=150; $OX=$TIP_W                                    # room to the LEFT for the hover hint
$tFont   = New-Object System.Drawing.Font("Segoe UI", 11,  [System.Drawing.FontStyle]::Bold)
$bFont   = New-Object System.Drawing.Font("Segoe UI", 9.5, [System.Drawing.FontStyle]::Regular)
$tipFont = New-Object System.Drawing.Font("Segoe UI", 9)
$textW = $CW - $PAD_L - $PAD_R

# Measure title + (wrapped) body to size the card.
$tb = New-Object System.Drawing.Bitmap(1,1); $tg = [System.Drawing.Graphics]::FromImage($tb)
$titleH = [int][Math]::Ceiling($tg.MeasureString($Title, $tFont, $textW).Height)
$bodyH  = [int][Math]::Ceiling($tg.MeasureString($Body,  $bFont, $textW).Height)
$tg.Dispose(); $tb.Dispose()
$CH = $PAD_T + $titleH + $GAP_TB + $bodyH + $PAD_T

$FORM_W = $CW + $GLOW*2 + $TIP_W
$FORM_H = $CH + $GLOW*2

# Announce ourselves to the other cards NOW - before the window exists. A new card used to appear
# at the top anchor while the card already there was still sitting in that exact spot, so it looked
# like the old one blinked out and reappeared lower. Registering first means they are already
# gliding down by the time we fade in.
[void](Stack-Sync $CH $true)

$script:hover     = $false
$script:fade      = 1.0
$FADE_MS          = 10000       # how long the fade-out itself takes - long and gradual, so a card
                                # drifts out of your attention instead of blinking away, and you have
                                # plenty of time to catch it (hovering brings it straight back)
$script:startTick = [Environment]::TickCount
$script:bornTick  = [Environment]::TickCount
$INTRO_MS         = 320         # slide-in + fade-up, so the card arrives instead of appearing
$script:intro     = 0.0
$script:tick      = 0
$script:closeReq  = $false

$GAP = 8
$script:baseTop   = $screen.Top + 30 - $GLOW
$script:curTop    = $script:baseTop - 22      # start above our slot and glide down into it
$script:targetTop = $script:baseTop
$script:lastTop   = -99999

function CA($a,$c){ [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }
function RoundedPath($x,$y,$w,$h,$rad){       # square left corners, rounded right
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad*2
    $p.AddLine($x, $y, ($x+$w-$rad), $y)
    $p.AddArc(($x+$w-$d), $y, $d,$d, 270, 90)
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d,$d, 0, 90)
    $p.AddLine(($x+$w-$rad), ($y+$h), $x, ($y+$h))
    $p.CloseFigure()
    return $p
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W
$form.Height = $FORM_H
$form.Left   = $screen.Right - $CW - 18 - $GLOW - $TIP_W   # keep the card at the corner; canvas extends left
$form.Top    = [int]$script:curTop

$render = {
    $glowBase = if ($script:hover) { 200 } else { 140 }
    $bgAlpha  = if ($script:hover) { 246 } else { 230 }
    $bgShade  = if ($script:hover) { 40 }  else { 17 }
    $borderA  = if ($script:hover) { 255 } else { 175 }
    $winBase  = if ($script:hover) { 255 } else { 240 }
    $winAlpha = [int]([Math]::Max(0, [Math]::Min(255, $winBase * $script:fade * $script:intro)))

    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    # glow on top/right/bottom; left kept crisp so the accent edge reads as a solid border
    $glowClip = New-Object System.Drawing.RectangleF ([float]($GLOW+$OX), 0, [float]($FORM_W - $GLOW - $OX), [float]$FORM_H)
    $g.SetClip($glowClip)
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int]($glowBase * [Math]::Exp(-$sp * 0.30))
        if ($alpha -lt 4) { continue }
        $gp  = RoundedPath ($GLOW+$OX-$sp) ($GLOW-$sp) ($CW+$sp*2) ($CH+$sp*2) ([Math]::Min($R+$sp,14))
        $pen = New-Object System.Drawing.Pen((CA $alpha $ACCENT), 1.5)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }
    $g.ResetClip()

    $cpath = RoundedPath ($GLOW+$OX) $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($bgAlpha, $bgShade, $bgShade, $bgShade))
    $g.FillPath($bg, $cpath); $bg.Dispose()

    $g.SetClip($cpath)
    $sb = New-Object System.Drawing.SolidBrush $ACCENT
    $g.FillRectangle($sb, ($GLOW+$OX), $GLOW, $BAR_W, $CH); $sb.Dispose()
    $g.ResetClip()

    $bpen = New-Object System.Drawing.Pen((CA $borderA $ACCENT), 1.2)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose(); $cpath.Dispose()

    # title in the session color, body in gray
    $tbrush = New-Object System.Drawing.SolidBrush $ACCENT
    $trect  = New-Object System.Drawing.RectangleF ([float]($GLOW+$OX+$PAD_L), [float]($GLOW+$PAD_T), [float]$textW, [float]$titleH)
    $g.DrawString($Title, $tFont, $tbrush, $trect); $tbrush.Dispose()

    $bbrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(198,198,202))
    $brect  = New-Object System.Drawing.RectangleF ([float]($GLOW+$OX+$PAD_L), [float]($GLOW+$PAD_T+$titleH+$GAP_TB), [float]$textW, [float]$bodyH)
    $g.DrawString($Body, $bFont, $bbrush, $brect); $bbrush.Dispose()

    # Hover hint to the LEFT of the card: how to interact.
    if ($script:hover) {
        # Not every card belongs to a chat - the usage warning belongs to none - and Jump-ToChat
        # re-resolves both of these from the state file at click time, so a card given only a sid
        # can still jump. Promise what this one can actually do rather than what most of them can.
        $jumpable = $Chat -or $Hwnd -or ($Sid -and (Test-Path -LiteralPath (Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice\badges") "$Sid.json")))
        $hint = if ($jumpable) { "Click to jump" } else { "Click to dismiss" }
        $hw   = [int][Math]::Ceiling($g.MeasureString($hint, $tipFont).Width)
        $hbw  = $hw + 18; $hbh = [int]$tipFont.Height + 8
        $hbx  = $GLOW + $OX - 10 - $hbw
        if ($hbx -lt 2) { $hbx = 2 }
        $hby  = $GLOW + [int](($CH - $hbh)/2)
        $hp   = RoundedPath $hbx $hby $hbw $hbh 5
        $hbg  = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(246, 26, 26, 28))
        $g.FillPath($hbg, $hp); $hbg.Dispose()
        $hpen = New-Object System.Drawing.Pen((CA 120 $ACCENT), 1)
        $g.DrawPath($hpen, $hp); $hpen.Dispose(); $hp.Dispose()
        $htb  = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(237,237,241))
        $g.DrawString($hint, $tipFont, $htb, [float]($hbx + 9), [float]($hby + 4)); $htb.Dispose()
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, [byte]$winAlpha)
    $bmp.Dispose()
}

# Left-click -> jump to the chat itself (its window, and its tab within it) and dismiss.
# Right-click -> just dismiss.
$form.Add_MouseDown({
    param($s, $e)
    if ($e.Button -ne [System.Windows.Forms.MouseButtons]::Right) {
        Jump-ToChat $Chat $Hwnd $Sid      # same jump as clicking the chat's tab
    }
    $script:closeReq = $true
})
$form.Add_HandleCreated({ [PerPixelLayered]::NoActivate($form.Handle) })   # never steal focus
$form.Add_Shown({ Assert-Topmost $form })                                   # ...but stay on top
$form.Add_Shown({
    [PerPixelLayered]::Init($form.Handle); & $render
    if ($PidFile) { try { [System.IO.File]::WriteAllText($PidFile, "$PID $($script:SlotFile)") } catch {} }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 20
$timer.Add_Tick({
    $script:tick++
    if ($script:intro -lt 1.0) {
        $script:intro = [Math]::Min(1.0, ([Environment]::TickCount - $script:bornTick) / [double]$INTRO_MS)
        $needIntroRender = $true
    } else { $needIntroRender = $false }
    if (($script:tick % 6) -eq 1) {
        $ordered = Stack-Sync $CH $true
        $script:targetTop = Stack-TargetTop $script:baseTop $GAP $ordered
    }
    $delta = $script:targetTop - $script:curTop
    if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:targetTop } else { $script:curTop += $delta * 0.22 }
    $newTop = [int]$script:curTop

    # hover: light the card up and pause auto-dismiss while the cursor is over it
    $cp = [System.Windows.Forms.Cursor]::Position
    $cardL = $form.Left + $GLOW + $OX; $cardT = $newTop + $GLOW
    $over = ($cp.X -ge $cardL -and $cp.X -lt ($cardL + $CW) -and $cp.Y -ge $cardT -and $cp.Y -lt ($cardT + $CH))
    $needRender = $needIntroRender
    if ($over -ne $script:hover) { $script:hover = $over; $needRender = $true }
    if ($over) { $script:startTick = [Environment]::TickCount }   # keep it up while hovered

    # Follow the chat's colour rather than the one we were spawned with. A card can sit here for
    # many minutes, and a chat's accent can move under it (it shifts to break a clash with another
    # live chat, and the whole palette shifts if the legibility floor changes) - a card still
    # wearing the old colour points at the wrong tab.
    if ($Sid -and (($script:tick % 50) -eq 7)) {
        $st = Read-JsonFile (Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice\badges") "$Sid.json")
        if ($st -and $st.color -and $st.color.Count -ge 3) {
            $c = [System.Drawing.Color]::FromArgb([int]$st.color[0], [int]$st.color[1], [int]$st.color[2])
            if ($c.ToArgb() -ne $ACCENT.ToArgb()) { $script:ACCENT = $c; $needRender = $true }
        }
    }

    if ($newTop -ne $script:lastTop) {
        $script:lastTop = $newTop
        $form.Top = $newTop
        [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
    }

    # Lifecycle: hold for DurationMs, then fade out gently over FADE_MS. The fade is computed from
    # the clock rather than counted down per tick, so it takes the same time however often we run.
    # Hovering restores the card to full strength and restarts the hold - previously it only froze
    # the countdown, so catching a card mid-fade left it sitting there half transparent.
    $elapsed = [Environment]::TickCount - $script:startTick
    if ($over) {
        if ($script:fade -lt 1.0) { $script:fade = 1.0; $needRender = $true }
    }
    elseif ($elapsed -gt $DurationMs) {
        $f = 1.0 - (($elapsed - $DurationMs) / [double]$FADE_MS)
        if ($f -le 0) { $script:closeReq = $true }
        elseif ([Math]::Abs($f - $script:fade) -gt 0.01) { $script:fade = $f; $needRender = $true }
    }
    if ($script:closeReq) { $form.Close(); return }
    if ($needRender) { & $render }
})
$timer.Start()

$form.Add_FormClosed({
    try { Stack-Sync $CH $false } catch {}
    if ($PidFile) { try { Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue } catch {} }
})

[System.Windows.Forms.Application]::Run($form)
