param(
    [Parameter(Mandatory=$true)][string]$StateFile,   # JSON {ts, color:[r,g,b], label} written by hooks
    [Parameter(Mandatory=$true)][string]$AliveFile,   # we heartbeat here so the controller won't respawn us
    [int]$IdleMs = 1200000                             # auto-dismiss after this much chat inactivity (20 min)
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
Set-StackNamespace 'badges_stack'                      # stack slots live apart from the controller's state files in 'badges'

# Exactly one badge per chat: if one already owns this chat's mutex (e.g. a spawn race
# during cold start), bail immediately. Held for our lifetime; the OS releases on exit.
$key = ([System.IO.Path]::GetFileNameWithoutExtension($StateFile)) -replace '[^A-Za-z0-9_]',''
$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_badge_$key", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea

function Read-State {
    try { return (Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json) } catch { return $null }
}
function NowMsLocal { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$st = Read-State
$script:R = 0; $script:G = 215; $script:B = 80; $script:Label = ""; $script:Hwnd = [int64]0
$script:State = "done"; $script:phase = 0; $script:Branch = ""; $script:Reason = ""; $script:Proj = ""
if ($st) {
    if ($st.color -and $st.color.Count -ge 3) { $script:R=[int]$st.color[0]; $script:G=[int]$st.color[1]; $script:B=[int]$st.color[2] }
    if ($st.label)  { $script:Label  = [string]$st.label }
    if ($st.hwnd)   { $script:Hwnd   = [int64]$st.hwnd }
    if ($st.state)  { $script:State  = [string]$st.state }
    if ($st.branch) { $script:Branch = [string]$st.branch }
    if ($st.reason) { $script:Reason = [string]$st.reason }
    if ($st.proj)   { $script:Proj   = [string]$st.proj }
}

$GLOW=12; $R_CORNER=5; $PAD_L=12; $PAD_R=12; $BAR_W=6; $DOTSZ=7
$hFont   = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$tipFont = New-Object System.Drawing.Font("Segoe UI", 9)

# The chip text: what it's waiting on (when awaiting input), else the topic + its branch.
function DisplayText {
    if ($script:State -eq 'waiting' -and $script:Reason) { return $script:Reason }
    $t = $script:Label
    if ($script:Branch -and $script:Branch -notin @('main','master')) { $t = "$t  $($script:Branch)" }
    return $t
}

# Measure label to size the chip.
function Measure-Width($text) {
    $tb = New-Object System.Drawing.Bitmap(1,1); $tg = [System.Drawing.Graphics]::FromImage($tb)
    $w = if ($text) { [int][Math]::Ceiling($tg.MeasureString($text, $hFont).Width) } else { 0 }
    $tg.Dispose(); $tb.Dispose(); return $w
}
$script:CW = 0; $script:CH = 28
function Recalc {
    $lw = Measure-Width (DisplayText)
    $script:CW = $PAD_L + $BAR_W + 8 + $DOTSZ + 8 + $lw + $PAD_R
    if ($script:CW -lt 96) { $script:CW = 96 }
}
Recalc
$FORM_W = 520 + $GLOW*2     # generous canvas; we blit only the chip and move it
$FORM_H = $script:CH + $GLOW*2
$SLIVER = 9                 # width of the colored left edge left showing when a tab is stowed

# A tab is never destroyed. Right-click STOWS it: it slides right into a drawer, leaving only its
# colored edge peeking at the screen's right; clicking that edge slides it back out. The stow state
# is remembered (a marker file) so the drawer stays where you left it across respawns.
$script:stowMarker = [System.IO.Path]::ChangeExtension($StateFile, ".stow")
$script:stowed = [System.IO.File]::Exists($script:stowMarker)

# Drag-to-reorder: the tab's position in the stack is a persisted order key (lower = higher up).
# Defaults to birth time so new tabs land at the bottom; dragging rewrites it.
$script:ordMarker = [System.IO.Path]::ChangeExtension($StateFile, ".ord")
$script:ord = try { [double]([System.IO.File]::ReadAllText($script:ordMarker)) } catch { [double]$script:BornMs }
$script:StackOrd = $script:ord

$GAP = 8
$script:bottomAnchor = $screen.Bottom - 44 - $GLOW       # sit above VS Code's status bar, bottom-right
$script:curTop  = $script:bottomAnchor - $script:CH
$script:target  = $script:curTop
$script:lastTop = -99999
$script:chipX   = if ($script:stowed) { $FORM_W - $GLOW - $SLIVER } else { $FORM_W - $GLOW - $script:CW }  # drawer pos
$script:tick = 0
$script:closeReq = $false
$script:hover = $false
$script:active = $false       # our chat's window is focused -> keep the tab lit (the tab you're on)
$script:presentTs = 0         # last time the user was actively present in this chat (from state)
$script:missCount = 0         # consecutive missing state reads (hysteresis, so a blip doesn't flicker)
$script:winMiss   = 0         # consecutive window-not-found checks (same idea)
$script:maybeDrag = $false    # left button is down; still deciding click-vs-drag
$script:dragging  = $false    # actively dragging this tab to reorder it
$script:dragStartY = 0
$script:grabOffset = 0        # cursor-to-form-top offset captured at grab, so the tab tracks smoothly

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W
$form.Height = $FORM_H
$form.Left   = $screen.Right - $FORM_W + $GLOW - 16   # fixed canvas; the chip is right-aligned inside it
$form.Top    = [int]$script:curTop

function CA($a,$c){ [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }
function RoundedPath($x,$y,$w,$h,$rad){
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad*2
    $p.AddLine($x, $y, ($x+$w-$rad), $y)
    $p.AddArc(($x+$w-$d), $y, $d,$d, 270, 90)
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d,$d, 0, 90)
    $p.AddLine(($x+$w-$rad), ($y+$h), $x, ($y+$h))
    $p.CloseFigure()
    return $p
}

$render = {
    $accent = [System.Drawing.Color]::FromArgb($script:R, $script:G, $script:B)
    # The chip lights up when hovered OR when its own chat window is focused (the tab you're on).
    $lit = ($script:hover -or $script:active)
    $glowBase = if ($lit) { 205 } else { 120 }
    $bgAlpha  = if ($lit) { 246 } else { 228 }
    $bgShade  = if ($lit) { 44 }  else { 17 }
    $borderA  = if ($lit) { 255 } else { 200 }
    $borderW  = if ($lit) { 1.9 } else { 1.2 }
    $winAlpha = if ($lit) { 255 } else { 240 }
    # The chip's left edge inside the canvas; eases right (into the drawer) when stowed, so only
    # its colored edge shows. Content past the canvas edge is clipped, leaving just that sliver.
    $cx = [int]$script:chipX
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    # subtle glow (top/right/bottom), left kept crisp
    $glowClip = New-Object System.Drawing.RectangleF ([float]$cx, 0, [float]($FORM_W - $cx), [float]$FORM_H)
    $g.SetClip($glowClip)
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int]($glowBase * [Math]::Exp(-$sp * 0.34))
        if ($alpha -lt 4) { continue }
        $gp = RoundedPath ($cx-$sp) ($GLOW-$sp) ($script:CW+$sp*2) ($script:CH+$sp*2) ([Math]::Min($R_CORNER+$sp,12))
        $pen = New-Object System.Drawing.Pen((CA $alpha $accent), 1.4)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }
    $g.ResetClip()

    $cpath = RoundedPath $cx $GLOW $script:CW $script:CH $R_CORNER
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($bgAlpha, $bgShade, $bgShade, $bgShade))
    $g.FillPath($bg, $cpath); $bg.Dispose()

    $g.SetClip($cpath)
    $sb = New-Object System.Drawing.SolidBrush $accent
    $g.FillRectangle($sb, $cx, $GLOW, $BAR_W, $script:CH); $sb.Dispose()
    $g.ResetClip()

    $bpen = New-Object System.Drawing.Pen((CA $borderA $accent), $borderW)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose(); $cpath.Dispose()

    # state indicator: done = check, working = breathing dot, waiting = blinking ring
    $dotX = $cx + $BAR_W + 8
    $dotY = $GLOW + [int](($script:CH - $DOTSZ)/2)
    if ($script:State -eq "working") {
        $pph = (1 + [Math]::Sin($script:phase * 0.28)) / 2
        $db = New-Object System.Drawing.SolidBrush (CA ([int](95 + 160*$pph)) $accent)
        $g.FillEllipse($db, $dotX, $dotY, $DOTSZ, $DOTSZ); $db.Dispose()
    } elseif ($script:State -eq "waiting") {
        $pph = (1 + [Math]::Sin($script:phase * 0.55)) / 2
        $pen = New-Object System.Drawing.Pen((CA ([int](55 + 200*$pph)) $accent), 2.0)
        $g.DrawEllipse($pen, $dotX, $dotY, ($DOTSZ-1), ($DOTSZ-1)); $pen.Dispose()
    } else {
        $pen = New-Object System.Drawing.Pen($accent, 2.2)
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
        $p1 = New-Object System.Drawing.PointF ([float]$dotX,                [float]($dotY + $DOTSZ*0.55))
        $p2 = New-Object System.Drawing.PointF ([float]($dotX + $DOTSZ*0.4), [float]($dotY + $DOTSZ))
        $p3 = New-Object System.Drawing.PointF ([float]($dotX + $DOTSZ),     [float]$dotY)
        $g.DrawLines($pen, @($p1,$p2,$p3)); $pen.Dispose()
    }

    # text in the chat color: awaiting-input reason, else "topic  branch"
    $disp = DisplayText
    if ($disp) {
        $tb = New-Object System.Drawing.SolidBrush $accent
        $ty = $GLOW + [int](($script:CH - $hFont.Height)/2)
        $g.DrawString($disp, $hFont, $tb, [float]($dotX + $DOTSZ + 8), [float]$ty); $tb.Dispose()
    }

    # Hover hint: a small how-to-interact chip to the LEFT of the tab (only on real mouse hover).
    if ($script:hover) {
        $tip = if ($script:stowed) { "Click to open" } else { "Left-click: jump      Right-click: stow" }
        $tw  = [int][Math]::Ceiling($g.MeasureString($tip, $tipFont).Width)
        $tbw = $tw + 18; $tbh = [int]$tipFont.Height + 8
        $tbx = $cx - 8 - $tbw
        if ($tbx -lt 2) { $tbx = 2 }
        $tby = $GLOW + [int](($script:CH - $tbh)/2)
        $tpath = RoundedPath $tbx $tby $tbw $tbh 5
        $tbg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(246, 26, 26, 28))
        $g.FillPath($tbg, $tpath); $tbg.Dispose()
        $tpen = New-Object System.Drawing.Pen((CA 120 $accent), 1)
        $g.DrawPath($tpen, $tpath); $tpen.Dispose(); $tpath.Dispose()
        $ttb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(237,237,241))
        $g.DrawString($tip, $tipFont, $ttb, [float]($tbx + 9), [float]($tby + 4)); $ttb.Dispose()
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, $winAlpha)
    $bmp.Dispose()
}

# Stowed: any click slides it back out. Out: left-click jumps to the chat's window, right-click
# stows it into the drawer. Tabs are never destroyed either way.
$form.Add_MouseDown({
    param($s, $e)
    if ($script:stowed) {
        $script:stowed = $false                          # open the drawer back up
        try { Remove-Item -LiteralPath $script:stowMarker -ErrorAction SilentlyContinue } catch {}
        return
    }
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Right) {
        $script:stowed = $true                           # slide into the drawer (only its edge stays)
        try { [System.IO.File]::WriteAllText($script:stowMarker, "1") } catch {}
        return
    }
    # left button: a click jumps to the chat; a drag reorders the tab (decided on release)
    $script:maybeDrag = $true; $script:dragging = $false
    $script:dragStartY = [System.Windows.Forms.Cursor]::Position.Y
    $script:grabOffset = [System.Windows.Forms.Cursor]::Position.Y - $form.Top
})

# On drop, set our order key so we sort into the gap where we were released; the others reflow.
$dropReorder = {
    try {
        $ordered = Stack-Sync $script:CH $true
        $myY = $form.Top + $script:CH/2
        $slots = @(); $below = 0
        foreach ($e in $ordered) {
            $top = $script:bottomAnchor - $below - [int]$e.h
            if ($e.id -ne $script:PopupId) {
                $o = if ($null -ne $e.ord) { [double]$e.ord } else { [double]$e.ts }
                $slots += [pscustomobject]@{ ord = $o; top = $top }
            }
            $below += [int]$e.h + $GAP
        }
        $aboveOrd = $null; $belowOrd = $null
        foreach ($sl in ($slots | Sort-Object top)) {
            if ($sl.top -lt $myY) { $aboveOrd = $sl.ord } elseif ($null -eq $belowOrd) { $belowOrd = $sl.ord }
        }
        if     ($null -ne $aboveOrd -and $null -ne $belowOrd) { $script:ord = ($aboveOrd + $belowOrd) / 2 }
        elseif ($null -ne $belowOrd) { $script:ord = $belowOrd - 1000000 }   # dropped above everything
        elseif ($null -ne $aboveOrd) { $script:ord = $aboveOrd + 1000000 }   # dropped below everything
        $script:StackOrd = $script:ord
        try { [System.IO.File]::WriteAllText($script:ordMarker, [string]$script:ord) } catch {}
    } catch {}
}
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

# One timer: ease position every frame (cheap), but only hit the shared registry / state
# file / lifecycle ~1.6x/sec - a persistent window must not thrash the disk for hours.
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 30
$timer.Add_Tick({
    $script:tick++
    if (($script:tick % 20) -eq 1) {
        $now = NowMsLocal
        try { [System.IO.File]::WriteAllText($AliveFile, "$now") } catch {}   # heartbeat (even while stowed)
        if (-not (Hud-Enabled)) { $script:closeReq = $true }                  # HUD switched off -> retire
        $st = Read-State
        if ($null -eq $st) {
            $script:missCount++                              # tolerate a transient missing read (avoids flicker)
            if ($script:missCount -ge 3) { $script:closeReq = $true }
        }
        else {
            $script:missCount = 0
            $changed = $false
            if ($st.color -and $st.color.Count -ge 3) {
                $nr=[int]$st.color[0]; $ng=[int]$st.color[1]; $nb=[int]$st.color[2]
                if ($nr -ne $script:R -or $ng -ne $script:G -or $nb -ne $script:B) { $script:R=$nr;$script:G=$ng;$script:B=$nb;$changed=$true }
            }
            $nl  = if ($st.label)  { [string]$st.label }  else { "" }
            $nbr = if ($st.branch) { [string]$st.branch } else { "" }
            $nrs = if ($st.reason) { [string]$st.reason } else { "" }
            $nstate = if ($st.state) { [string]$st.state } else { "done" }
            if ($nl -ne $script:Label -or $nbr -ne $script:Branch -or $nrs -ne $script:Reason -or $nstate -ne $script:State) {
                $script:Label = $nl; $script:Branch = $nbr; $script:Reason = $nrs; $script:State = $nstate
                Recalc; $changed = $true    # any of these can change the chip's displayed text/width
            }
            if ($st.hwnd) { $script:Hwnd = [int64]$st.hwnd }   # may be recaptured as the user revisits the chat
            if ($st.proj) { $script:Proj = [string]$st.proj }
            if ($st.present_ts) { $script:presentTs = [int64]$st.present_ts }
            # Retire only when the window is really gone, and only after a few consecutive misses,
            # so a transient handle-lookup blip doesn't make the tab flicker out and back.
            if ($script:Hwnd -ne 0 -and -not [PerPixelLayered]::WindowExists([IntPtr]$script:Hwnd)) {
                $script:winMiss++
                if ($script:winMiss -ge 3) { $script:closeReq = $true }
            } else {
                $script:winMiss = 0
            }
            if ($changed) { & $render }
        }
        $ordered = Stack-Sync $script:CH $true                # stowed tabs still hold their stack slot (as slivers)
        $script:target = Stack-TargetBottom $script:bottomAnchor $GAP $ordered $script:CH
    }
    if ($script:closeReq) { $form.Close(); return }

    # The focused window drives the "tab you're on" highlight and un-hiding a dismissed tab. Match
    # by handle first, then (handles drift) by the chat's project name in the window title.
    $fg = ([PerPixelLayered]::GetForegroundWindow()).ToInt64()
    $isOwn = ($script:Hwnd -ne 0 -and $fg -eq [int64]$script:Hwnd)
    if (-not $isOwn -and $script:Proj -and $fg -ne 0) {
        try {
            $ft = [PerPixelLayered]::WindowTitle([IntPtr]$fg)
            if ($ft -and $ft.EndsWith("Visual Studio Code") -and $ft.Contains($script:Proj)) { $isOwn = $true }
        } catch {}
    }

    if ($isOwn -ne $script:active) { $script:active = $isOwn; & $render }                 # active-tab highlight

    $needRender = $false

    # Drag to reorder: with the left button held, once you move, the tab follows the cursor; on
    # release, a small move = a click (jump to the chat), a real drag = drop into the new slot.
    if ($script:maybeDrag) {
        $leftDown = ([System.Windows.Forms.Control]::MouseButtons -band [System.Windows.Forms.MouseButtons]::Left) -ne 0
        $cy = [System.Windows.Forms.Cursor]::Position.Y
        if ($leftDown) {
            if (-not $script:dragging -and [Math]::Abs($cy - $script:dragStartY) -gt 5) { $script:dragging = $true }
            if ($script:dragging) {
                $script:curTop = $cy - $script:grabOffset
                $nt = [int]$script:curTop
                if ($nt -ne $script:lastTop) { $script:lastTop = $nt; $form.Top = $nt; [PerPixelLayered]::Move($form.Handle, $form.Left, $nt) }
            }
        } else {
            $script:maybeDrag = $false
            if ($script:dragging) { $script:dragging = $false; & $dropReorder }
            elseif ($script:Hwnd -ne 0) { try { [PerPixelLayered]::FocusWindow([IntPtr]$script:Hwnd) } catch {} }
        }
    }

    # vertical: ease into the stack slot (slides the already-blitted surface, no redraw)
    if (-not $script:dragging) {
        $delta = $script:target - $script:curTop
        if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:target } else { $script:curTop += $delta * 0.22 }
        $newTop = [int]$script:curTop
        if ($newTop -ne $script:lastTop) {
            $script:lastTop = $newTop
            $form.Top = $newTop
            [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
        }
    }

    # horizontal: ease the chip toward its drawer position (out = fully shown, stowed = only the edge)
    $tgtX = if ($script:stowed) { $FORM_W - $GLOW - $SLIVER } else { $FORM_W - $GLOW - $script:CW }
    $dx = $tgtX - $script:chipX
    if ([Math]::Abs($dx) -lt 0.5) { if ($script:chipX -ne $tgtX) { $script:chipX = $tgtX; $needRender = $true } }
    else { $script:chipX += $dx * 0.25; $needRender = $true }

    # Hover over the VISIBLE part of the tab (cursor-rect poll; MouseLeave is unreliable here).
    $visL = $form.Left + [int]$script:chipX
    $visR = $form.Left + $FORM_W - $GLOW
    $chipT = $form.Top + $GLOW
    $cp = [System.Windows.Forms.Cursor]::Position
    $over = ($cp.X -ge $visL -and $cp.X -lt $visR -and $cp.Y -ge $chipT -and $cp.Y -lt ($chipT + $script:CH))
    if ($over -ne $script:hover) { $script:hover = $over; $needRender = $true }

    if ($needRender) { & $render }

    # Animate the indicator (~11 fps) while working/awaiting; 'done' stays static.
    if ((($script:State -eq "working") -or ($script:State -eq "waiting")) -and (($script:tick % 3) -eq 0)) {
        $script:phase++
        & $render
    }
})
$timer.Start()

$form.Add_FormClosed({
    try { Stack-Sync $script:CH $false } catch {}
    try { Remove-Item -LiteralPath $AliveFile -ErrorAction SilentlyContinue } catch {}
})

[System.Windows.Forms.Application]::Run($form)
