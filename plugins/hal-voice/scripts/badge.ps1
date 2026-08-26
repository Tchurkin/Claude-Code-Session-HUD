param(
    [Parameter(Mandatory=$true)][string]$StateFile,   # JSON {ts, color:[r,g,b], label} written by hooks
    [Parameter(Mandatory=$true)][string]$AliveFile,   # we heartbeat here so the controller won't respawn us
    [int]$IdleMs = 1200000                             # vestigial: a tab now lives exactly as long as
                                                       # its chat is open (see hal_sessions.py)
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
if (-not $script:PplReady) { exit 1 }   # no drawing type -> exit so the supervisor respawns a working one
Set-StackNamespace 'badges_stack'                      # stack slots live apart from the controller's state files in 'badges'

# Exactly one badge per chat: if one already owns this chat's mutex (e.g. a spawn race
# during cold start), bail immediately. Held for our lifetime; the OS releases on exit.
$key = ([System.IO.Path]::GetFileNameWithoutExtension($StateFile)) -replace '[^A-Za-z0-9_]',''
$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_badge_$key", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea

$script:stMt = [datetime]::MinValue
$script:stCache = $null
function Read-State {
    # Cached on the file's timestamp: parsing this costs ~3ms and it is read many times a second,
    # while the reconciler only rewrites it when something about the chat has actually changed.
    try {
        $mt = [System.IO.File]::GetLastWriteTimeUtc($StateFile)
        if ($mt -eq $script:stMt) { return $script:stCache }
        $o = Read-TextShared $StateFile | ConvertFrom-Json
        $script:stMt = $mt; $script:stCache = $o
        return $o
    } catch { $script:stMt = [datetime]::MinValue; $script:stCache = $null; return $null }
}
function NowMsLocal { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$st = Read-State
$script:R = 0; $script:G = 215; $script:B = 80; $script:Label = ""; $script:Hwnd = [int64]0
$script:State = "done"; $script:phase = 0; $script:Branch = ""; $script:Reason = ""; $script:Proj = ""
$script:Showing = $true       # is our chat the tab its window is currently on? (assume yes if unknown)
$script:baseBmp = $null       # cached chip surface (everything but the state dot)
$script:Title = ""            # the chat's own title
$script:Tab   = ""            # the exact label of its editor tab, when the extension has told us
$script:BranchShow = $true    # does its branch distinguish it from another open chat?
if ($st) {
    if ($st.color -and $st.color.Count -ge 3) { $script:R=[int]$st.color[0]; $script:G=[int]$st.color[1]; $script:B=[int]$st.color[2] }
    if ($st.label)  { $script:Label  = [string]$st.label }
    if ($st.hwnd)   { $script:Hwnd   = [int64]$st.hwnd }
    if ($st.state)  { $script:State  = [string]$st.state }
    if ($st.branch) { $script:Branch = [string]$st.branch }
    if ($st.reason) { $script:Reason = [string]$st.reason }
    if ($st.proj)   { $script:Proj   = [string]$st.proj }
    if ($st.title)  { $script:Title  = [string]$st.title }
    if ($st.tab)    { $script:Tab    = [string]$st.tab }
    if ($null -ne $st.branch_show) { $script:BranchShow = [bool]$st.branch_show }
    if ($null -ne $st.showing) { $script:Showing = [bool]$st.showing }
}

$GLOW=12; $R_CORNER=5; $PAD_L=12; $PAD_R=12; $BAR_W=6; $DOTSZ=7
$hFont   = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$tipFont = New-Object System.Drawing.Font("Segoe UI", 9)

# The chip text: what it's waiting on (when awaiting input), else the topic - plus the git branch,
# but only when the branch actually tells this chat apart from another one you have open (see
# hal_sessions._branch_shows). The same branch repeated across every tab in a repo is just noise.
function DisplayText {
    if ($script:State -eq 'waiting' -and $script:Reason) { return $script:Reason }
    $t = $script:Label
    if ($script:Branch -and $script:BranchShow -and $script:Branch -notin @('main','master')) {
        $t = "$t  $($script:Branch)"
    }
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
$script:parked = $false; $script:wasParked = $false      # pushed off the top of the dock (see the poll)
# 37 = the usage meter's own height plus its gap; it rides above the stack and must stay on screen.
$script:stackCap = Stack-Capacity $script:bottomAnchor ($script:CH + $GAP) 37
$script:curTop  = $script:bottomAnchor - $script:CH
$script:target  = $script:curTop
$script:lastTop = -99999
$script:chipX   = if ($script:stowed) { $FORM_W - $GLOW - $SLIVER } else { $FORM_W - $GLOW - $script:CW }  # drawer pos
$script:drawX   = $script:chipX     # drawer position PLUS the shared dock offset
$script:tick = 0
$script:closeReq = $false
$script:hover = $false
$script:active = $false       # our chat's window is focused -> keep the tab lit (the tab you're on)
$script:presentTs = 0         # last time the user was actively present in this chat (from state)
$script:missCount = 0         # consecutive missing state reads (hysteresis, so a blip doesn't flicker)
$script:maybeDrag = $false    # left button is down; still deciding click-vs-drag
$script:dragging  = $false    # actively dragging this tab to reorder it
$script:lastDragPub = 0       # last time we published a provisional position mid-drag
$script:dragNear  = $false    # someone else is dragging: watch the order closely
$script:lastStack = 0         # last time we re-read the shared stack order
$script:lastFrame = 0         # frame clock, so easing is a speed rather than a per-frame step
$script:lastStamp = 0         # directory timestamp: ticks when a tab appears or leaves
$script:lastFullRead = 0      # last time we actually opened everyone else's slot file
$script:dragStartY = 0
$script:grabOffset = 0        # cursor-to-form-top offset captured at grab, so the tab tracks smoothly

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W
$form.Height = $FORM_H
# 30, not 16: the dock keeps a lane at the right edge for the stow handle. A badge's glow reaches
# 12px past its chip and glow is not transparent, so a narrower lane would have the tabs quietly
# swallowing clicks meant for the handle - a layered window is hit-tested on alpha, not on ink.
$DOCK_LANE = 30
$form.Left   = $screen.Right - $FORM_W + $GLOW - $DOCK_LANE   # canvas fixed; chip right-aligned in it
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

# Paint a frame: blit the cached chip and draw the live state indicator on top. The indicator is
# the only thing that changes while a chat works, and rebuilding the whole chip for it was the
# single most expensive thing the HUD did.
# Paint a frame: blit the cached chip and draw the live state indicator over it. While a chat works
# the indicator is the ONLY thing that changes, and rebuilding the whole chip for it - a 12-pass glow,
# a rounded path per pass - cost 12ms a frame. Blitting the cached surface costs about one.
$paint = {
    if ($null -eq $script:baseBmp) { return }
    $accent = $script:accent
    $dotX = $script:dotX; $dotY = $script:dotY
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
    $g.DrawImageUnscaled($script:baseBmp, 0, 0)          # verbatim, alpha included
    $g.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceOver
    # state indicator: done = check, working = breathing dot, waiting = blinking ring
    if ($script:State -eq "working") {
        $pph = (1 + [Math]::Sin($script:phase * 0.28)) / 2
        $db = New-Object System.Drawing.SolidBrush (CA ([int](95 + 160*$pph)) $accent)
        $g.FillEllipse($db, $dotX, $dotY, $DOTSZ, $DOTSZ); $db.Dispose()
    } elseif ($script:State -eq "asking") {
        # A question mark: this chat is waiting on YOUR answer, which is a different thing from busy
        # and from blocked-on-a-permission. Pulses so it catches the eye in a stack of quiet tabs.
        $pph = (1 + [Math]::Sin($script:phase * 0.42)) / 2
        $qb = New-Object System.Drawing.SolidBrush (CA ([int](150 + 105*$pph)) $accent)
        $qf = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
        $g.DrawString("?", $qf, $qb, [float]($dotX - 3), [float]($GLOW + ($script:CH - 17)/2))
        $qb.Dispose(); $qf.Dispose()
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


    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, $script:winAlpha)
    Assert-Topmost $form
    $bmp.Dispose()
}

$render = {
    if ($script:parked) {
        # Off the top of the dock: draw nothing at all rather than a chip nobody can reach. The
        # process stays alive and keeps beating, so it comes straight back when there is room.
        $blank = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        [PerPixelLayered]::SetBitmap($form.Handle, $blank, $form.Left, $form.Top, 255)
        $blank.Dispose()
        return
    }
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
    $cx = [int]$script:drawX          # drawer position plus the shared dock offset
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

    $dotX = $cx + $BAR_W + 8
    $dotY = $GLOW + [int](($script:CH - $DOTSZ)/2)
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
    if ($script:baseBmp) { $script:baseBmp.Dispose() }
    $script:baseBmp = $bmp            # the chip minus its indicator, reused by $paint
    $script:accent = $accent; $script:winAlpha = $winAlpha
    $script:dotX = $cx + $BAR_W + 8
    $script:dotY = $GLOW + [int](($script:CH - $DOTSZ)/2)
    & $paint
}

# Stowed: any click slides it back out. Out: left-click jumps to the chat's window, right-click
# stows it into the drawer. Tabs are never destroyed either way.
$form.Add_MouseDown({
    param($s, $e)
    if ($script:stowed) {
        $script:stowed = $false                          # open the drawer back up
        try { $timer.Interval = 30; $script:curInterval = 30 } catch {}
        try { Remove-Item -LiteralPath $script:stowMarker -ErrorAction SilentlyContinue } catch {}
        return
    }
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Right) {
        $script:stowed = $true                           # slide into the drawer (only its edge stays)
        try { $timer.Interval = 30; $script:curInterval = 30 } catch {}
        try { [System.IO.File]::WriteAllText($script:stowMarker, "1") } catch {}
        return
    }
    # left button: a click jumps to the chat; a drag reorders the tab (decided on release)
    $script:maybeDrag = $true; $script:dragging = $false
    try { $timer.Interval = 30; $script:curInterval = 30 } catch {}   # respond at full rate now
    $script:dragStartY = [System.Windows.Forms.Cursor]::Position.Y
    $script:grabOffset = [System.Windows.Forms.Cursor]::Position.Y - $form.Top
})

# The height this tab claims in the stack. Zero while parked, so it holds its place in the
# order without taking any room. One definition, because it is written from four places.
function SlotHeight { if ($script:parked) { return 0 } return $script:CH }

# Where would this tab sort if it were released right now? Sorts into the gap it is hovering over,
# by comparing its middle against where every other tab currently sits. Used continuously during the
# drag so the rest of the stack gets out of the way as you move, and once more on release.
$provisionalOrd = {
    param($ordered)
    $myY = $form.Top + $script:CH / 2
    $slots = @(); $below = 0
    foreach ($e in $ordered) {
        $top = $script:bottomAnchor - $below - [int]$e.h
        if ($e.id -ne $script:PopupId) {
            $o = if ($null -ne $e.ord) { [double]$e.ord } else { [double]$e.ts }
            $slots += [pscustomobject]@{ ord = $o; top = $top }
        }
        if ([int]$e.h -gt 0) { $below += [int]$e.h + $GAP }     # parked tabs occupy no slot
    }
    $aboveOrd = $null; $belowOrd = $null
    foreach ($sl in ($slots | Sort-Object top)) {
        if ($sl.top -lt $myY) { $aboveOrd = $sl.ord } elseif ($null -eq $belowOrd) { $belowOrd = $sl.ord }
    }
    if     ($null -ne $aboveOrd -and $null -ne $belowOrd) { return ($aboveOrd + $belowOrd) / 2 }
    elseif ($null -ne $belowOrd) { return $belowOrd - 1000000 }   # above everything
    elseif ($null -ne $aboveOrd) { return $aboveOrd + 1000000 }   # below everything
    return $script:ord
}

# On drop, keep wherever we had provisionally sorted to and make it durable.
$dropReorder = {
    try {
        $script:ord = & $provisionalOrd (Stack-Sync (SlotHeight) $true)
        $script:StackOrd = $script:ord
        try { [System.IO.File]::WriteAllText($script:ordMarker, [string]$script:ord) } catch {}
    } catch {}
}
# Clicking a tab should land you IN the chat, not merely in front of its window - a window can hold
# several chats and only one is in front. We raise the window ourselves (Win32) and drop a request
# for the companion VS Code extension, which is inside that window and can switch to the chat's tab
# and put the cursor in its input. Without the extension installed you still get the window.
$jumpToChat = {
    # Ask for the tab by the label the editor actually shows when we know it; the chat title is the
    # fallback (matched loosely, since VS Code truncates tab labels).
    $want = if ($script:Tab) { $script:Tab } else { $script:Title }
    Jump-ToChat $want $script:Hwnd $key
}

$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

# One timer: ease position every frame (cheap), but only hit the shared registry / state
# file / lifecycle ~1.6x/sec - a persistent window must not thrash the disk for hours.
$timer = New-Object System.Windows.Forms.Timer
$script:lastPoll = 0; $script:lastPulse = 0; $script:lastBeat = 0
$script:curInterval = 30
$timer.Interval = 30
$timer.Add_Tick({
    $script:tick++
    $nowMs = NowMsLocal
    if ($nowMs - $script:lastPoll -ge 600) {
        $script:lastPoll = $nowMs
        $now = $nowMs
        # One badge per open chat, and only the daemon can retire one - so badges outlive it and
        # make the natural watchers. Self-throttled and jittered; see Poll-HudDaemon.
        Poll-HudDaemon
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
            $nbs = ($null -eq $st.branch_show) -or [bool]$st.branch_show
            if ($nbs -ne $script:BranchShow) { $script:BranchShow = $nbs; Recalc; $changed = $true }
            $nl  = if ($st.label)  { [string]$st.label }  else { "" }
            $nbr = if ($st.branch) { [string]$st.branch } else { "" }
            $nrs = if ($st.reason) { [string]$st.reason } else { "" }
            $nstate = if ($st.state) { [string]$st.state } else { "done" }
            if ($nl -ne $script:Label -or $nbr -ne $script:Branch -or $nrs -ne $script:Reason -or $nstate -ne $script:State) {
                $script:Label = $nl; $script:Branch = $nbr; $script:Reason = $nrs; $script:State = $nstate
                Recalc; $changed = $true    # any of these can change the chip's displayed text/width
            }
            if ($st.hwnd) { $script:Hwnd = [int64]$st.hwnd }   # may be rebound as the user revisits the chat
            if ($st.proj) { $script:Proj = [string]$st.proj }
            $script:Showing = ($null -eq $st.showing) -or [bool]$st.showing
            if ($st.title) { $script:Title = [string]$st.title }
            if ($st.tab)   { $script:Tab   = [string]$st.tab }
            if ($st.present_ts) { $script:presentTs = [int64]$st.present_ts }
            # A dead window handle is NOT a dead chat - VS Code hands out a new handle on reload, and
            # a chat can outlive the window we happened to bind it to. Closing on that is what used to
            # make tabs vanish from under open chats. Whether this chat still exists is decided in one
            # place (hal_sessions, against Claude Code's session registry) and told to us by the state
            # file disappearing; here we just stop pointing at a handle that's gone.
            if ($script:Hwnd -ne 0 -and -not [PerPixelLayered]::WindowExists([IntPtr]$script:Hwnd)) {
                $script:Hwnd = 0                              # reconciler rebinds us on its next pass
            }
            if ($changed) { & $render }
        }
        # A parked tab reports zero height but keeps its slot, so it still holds its place in the
        # order. That is deliberate: the ranking below is computed over every tab, parked or not, so
        # parking one can never change which others are parked. Were parked tabs to drop out of the
        # list, the tab below the cut would rise above it, un-park, push the first back out, and the
        # dock would flicker between two states forever.
        # Beat every pass, but only re-read everyone else's slot when the set of them might have
        # changed - the directory's timestamp ticks on a create or delete and costs one stat, where
        # reading five slots costs five file opens at ~135us each. A tab appearing or leaving is
        # caught immediately; an ord or height changing inside a file is caught by the fallback,
        # and while a drag is in progress the block below re-reads at frame rate anyway.
        Stack-Write (SlotHeight)
        $stamp = Stack-DirStamp
        if ($stamp -ne $script:lastStamp -or ($nowMs - $script:lastFullRead) -ge 2400) {
            $script:lastStamp = $stamp; $script:lastFullRead = $nowMs
            $ordered = Stack-Peek $true
            $limit = Stack-VisibleLimit @($ordered).Count (Hud-ConfigNum 'max_tabs' 0) $script:stackCap
            $script:parked = ((Stack-RankOf $ordered $script:PopupId) -ge $limit)
            if ($script:parked -ne $script:wasParked) { $script:wasParked = $script:parked; & $render }
            $script:lastStack = $nowMs
            $script:target = Stack-TargetBottom $script:bottomAnchor $GAP $ordered $script:CH
        }
    }

    # While a tab is being dragged, re-read the order several times a second rather than waiting for
    # the 600ms poll - otherwise the other tabs would shuffle in visible 600ms steps instead of
    # sliding out of the way. Costs one file stat per tick the rest of the time, and the flag is a
    # timestamp, so noticing a drag never means reading anything.
    if (-not $script:dragging -and ($nowMs - $script:lastStack -ge 70)) {
        $script:dragNear = Stack-DragActive
        if ($script:dragNear) {
            $script:lastStack = $nowMs
            # Peek, not Sync: we only want to know where everyone is, and rewriting our own slot
            # file every frame while somebody drags is I/O on the paint thread for no reason.
            $script:target = Stack-TargetBottom $script:bottomAnchor $GAP (Stack-Peek) $script:CH
        }
    }
    if ($script:closeReq) { $form.Close(); return }

    # "The tab you're on": our chat's window is focused AND that window is showing OUR chat rather
    # than one of its neighbours (hal_sessions works out which, by window title). Matching on the
    # project name instead - as this did - lit up every tab in the folder at once, which is exactly
    # the confusion the HUD exists to remove.
    $fg = ([PerPixelLayered]::GetForegroundWindow()).ToInt64()
    $isOwn = ($script:Hwnd -ne 0 -and $fg -eq [int64]$script:Hwnd -and $script:Showing)

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
                # Publish where we WOULD land, several times a second, so the rest of the stack can
                # ease out of the way while you are still holding the tab rather than all at once
                # when you let go. Everything needed to move them already existed - they just had
                # nothing to react to until the drop.
                if ($nowMs - $script:lastDragPub -ge 70) {
                    $script:lastDragPub = $nowMs
                    Stack-SignalDrag                       # tells the others to look more often
                    try {
                        $script:StackOrd = & $provisionalOrd (Stack-Sync (SlotHeight) $true)
                    } catch {}
                }
            }
        } else {
            $script:maybeDrag = $false
            if ($script:dragging) { $script:dragging = $false; & $dropReorder }
            else { & $jumpToChat }
        }
    }

    # How long the last frame actually took. The ease used to be a flat fraction per frame, which
    # ties how fast a tab moves to how often the timer happens to fire - raise the frame rate to
    # smooth the motion and everything also gets twice as fast, and a stalled frame teleports it.
    # Converting the fraction to a rate keeps the speed identical at any frame rate.
    $dt = $nowMs - $script:lastFrame
    if ($dt -le 0) { $dt = $script:curInterval }
    if ($dt -gt 120) { $dt = 120 }              # after a real stall, catch up smoothly, not instantly
    $script:lastFrame = $nowMs
    $ease = 1 - [Math]::Pow(1 - 0.22, $dt / 30.0)     # 0.22 per 30ms, however the frames land

    # vertical: ease into the stack slot (slides the already-blitted surface, no redraw)
    if (-not $script:dragging) {
        $delta = $script:target - $script:curTop
        if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:target } else { $script:curTop += $delta * $ease }
        $newTop = [int]$script:curTop
        if ($newTop -ne $script:lastTop) {
            $script:lastTop = $newTop
            $form.Top = $newTop
            [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
        }
    }

    # horizontal: ease the chip toward its drawer position (out = fully shown, stowed = only the edge)
    # Two independent horizontal movements, and they must not be confused. The DRAWER (right-click
    # stow of one tab) is this tab's own business and keeps its own spring. The DOCK slide is shared:
    # every tab and the meter add the same offset at the same instant, so the whole thing leaves as
    # one panel. Easing that locally is what made them scatter - each tab travels a different
    # distance, because a chip is only as wide as its label, so the narrow ones arrived first.
    $tgtX = if ($script:stowed) { $FORM_W - $GLOW - $SLIVER } else { $FORM_W - $GLOW - $script:CW }
    $dx = $tgtX - $script:chipX
    if ([Math]::Abs($dx) -lt 0.5) { if ($script:chipX -ne $tgtX) { $script:chipX = $tgtX; $needRender = $true } }
    else { $script:chipX += $dx * (1 - [Math]::Pow(1 - 0.25, $dt / 30.0)); $needRender = $true }
    # Where the chip is actually drawn: its drawer position plus wherever the dock currently is.
    $newDraw = $script:chipX + (Dock-Offset)
    if ([Math]::Abs($newDraw - $script:drawX) -ge 0.5) { $script:drawX = $newDraw; $needRender = $true }

    # Hover over the VISIBLE part of the tab (cursor-rect poll; MouseLeave is unreliable here).
    $visL = $form.Left + [int]$script:drawX
    $visR = $form.Left + $FORM_W - $GLOW
    $chipT = $form.Top + $GLOW
    $cp = [System.Windows.Forms.Cursor]::Position
    $over = ($cp.X -ge $visL -and $cp.X -lt $visR -and $cp.Y -ge $chipT -and $cp.Y -lt ($chipT + $script:CH))
    if ($over -ne $script:hover) { $script:hover = $over; $needRender = $true }

    if ($needRender) { & $render }

    # Animate the indicator (~11 fps) while working/awaiting; 'done' stays static.
    if (($script:State -in @("working", "waiting", "asking")) -and ($nowMs - $script:lastPulse -ge 90)) {
        $script:lastPulse = $nowMs
        $script:phase++
        & $paint                      # cached chip + fresh indicator
    }
    if ($nowMs - $script:lastBeat -ge 600) { $script:lastBeat = $nowMs; Write-Beat $AliveFile }

    # Adaptive cadence. 30ms while something moves or the cursor is on us, ~11fps while a chat is
    # working (just the indicator), otherwise a slow idle poll - a tab that is simply sitting there
    # has nothing to redraw and shouldn't cost anything to keep on screen.
    $moving = $script:dragging -or $script:maybeDrag -or $script:dragNear -or (Dock-Moving) -or
              ([Math]::Abs($script:target - $script:curTop) -ge 0.5) -or
              ([Math]::Abs($tgtX - $script:chipX) -ge 0.5)
    # 15ms is the practical floor for a WinForms timer (the message clock ticks ~15.6ms), and
    # it is what makes a drag look continuous rather than stepped. Only while something moves.
    $want = if ($moving) { 15 }
            elseif ($script:hover) { 30 }
            elseif ($script:State -in @("working", "waiting", "asking")) { 90 }
            else { 200 }
    if ($want -ne $script:curInterval) { $script:curInterval = $want; $timer.Interval = $want }
})
$timer.Start()

$form.Add_FormClosed({
    try { Stack-Sync $script:CH $false } catch {}
    try { Remove-Item -LiteralPath $AliveFile -ErrorAction SilentlyContinue } catch {}
})

[System.Windows.Forms.Application]::Run($form)
