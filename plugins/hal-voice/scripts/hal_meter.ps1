param(
    [string]$AliveFile = ""
)

# Plan usage meter: how much of your rolling limits you have spent, riding just above the tab stack.
#
# Two bars. The SESSION (5-hour) window is the headline - thicker, with the percentage beside it -
# because that is the limit you actually run into during a day's work. The WEEKLY window gets a
# thinner, dimmer bar underneath: rarely the binding constraint, but worth noticing before it is.
# Both come from the real figures Claude reports (hal_usage.py), not a local estimate, and both grey
# out when the reading has gone stale rather than showing an old number as though it were current.
#
# The readout is a button: either mouse button opens a detail panel above the stack with everything
# the bars cannot carry - the burn rate, tokens a minute, where the window lands, and the last
# twenty minutes at a readable size. There is no hover tooltip; the panel says all of it, and saying
# it twice was clutter. Hover only lifts a faint plate behind the readout, so it still looks like
# something you can press.
#
# Only the drawn pixels take a click - a layered window is hit-tested against its alpha - and the
# window never takes focus, so the editor keeps the caret. Hover is found by polling the cursor
# rather than by mouse events, because the window slides under a stationary pointer and enter/leave
# are generated from mouse messages, not from windows moving.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
if (-not $script:PplReady) { exit 1 }   # no drawing type -> exit so the supervisor respawns a working one

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_usage_meter", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$GLOW = 10
# One line - "7% - 4h 47m" - over the two bars. $TIP_W used to be 600px of room for a hover hint;
# now it is just enough slack for the reading to overhang the bar it sits above.
$TIP_W = 80                                        # canvas slack for the text to overhang the bar
$OX = $TIP_W
$UW = 62                                           # bar width
$UPCT_H = 15                                       # room for the reading
$SBAR_H = 6                                        # session bar (the one that matters)
$WBAR_H = 3                                        # weekly bar
$GAP_BARS = 3
$CONTENT_H = $UPCT_H + 2 + $SBAR_H + $GAP_BARS + $WBAR_H
$FORM_W = $UW + $GLOW*2 + $TIP_W
$FORM_H = $CONTENT_H + $GLOW*2
$uFont   = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)

# How many readings before a chart of them means anything. Deliberately hal_usage.MIN_SAMPLES, so
# the panel's sparkline appears at the same moment the burn rate it sits beside does.
$SPARK_MIN = 4

$script:hot = $false; $script:closeReq = $false
$script:sessionPct = -1; $script:weeklyPct = -1
$script:sessionResets = ""; $script:weeklyResets = ""
$script:sessionLeft = ""   # "2h14m" until the session window rolls over
$script:stale = $false
$script:parked = 0         # tabs pushed off the top of the dock, shown as "+N"
$script:hist = @()         # recent [ms, utilization] readings, for the sparkline
$script:histSig = ""       # cheap "has the history changed" key
$script:pace = -1.0        # 0 coasting .. 0.5 lands on the limit .. 1 runs out at once (-1 unknown)
$script:projected = -1     # where the session lands at reset, at the current burn
$script:hitMins = -1       # minutes until the limit at the current burn
$script:burn = -1.0        # utilization points per minute
$script:slide = 0.0        # how far the dock has slid off to the right
$script:lastSlide = -1.0
$script:lastUsage = 0; $script:lastStack = 0; $script:lastPresence = 0
$script:lastDaemon = 0; $script:lastBeat = 0; $script:curInterval = 200
# What the readout currently occupies, in canvas coords - set by the render, read by Over-Bar.
$script:hitL = 0; $script:hitR = 0
$script:inferred = $false  # the window rolled over while we could not ask: worked out, not read
$script:liveUtil = -1.0    # the reading carried forward with locally measured spend
$script:byChat = @()       # which chats are spending it: @(@(sid8, weightedPerMin), ...)
$script:long = @()         # the whole window's readings, for the panel's chart
$script:err = ""           # why the last fetch failed: auth | rate | net
$script:nextTry = 0        # when it will try again
$script:readingTs = 0      # when the reading we are showing was actually taken
$script:weeklyProj = -1    # where the week lands at the rate so far
$script:weeklyHit = 0      # ... and when it would run out, if it would
$script:elsewhere = 0      # burn we measured but cannot account for locally
$script:rates = @()        # API-derived %/min, the coarse fallback for the chart
$script:series = @()       # weighted tokens/min at 5s resolution - what the chart draws
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W; $form.Height = $FORM_H
$DOCK_LANE = 30                                    # room at the edge for the stow handle
$form.Left   = $screen.Right - $UW - $DOCK_LANE - $GLOW - $TIP_W   # right edge lines up with the tabs

$ns = Join-Path $env:USERPROFILE ".claude\hal_voice\badges_stack"
$usageFile  = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "usage.json"
$tokensFile = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "tokens.json"
$badgesDir  = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "badges"
$dockBottom = $screen.Bottom - 44                  # above VS Code's status bar
$GAPB = 8
$script:curTop    = $dockBottom - $CONTENT_H - $GLOW
$script:targetTop = $script:curTop
$script:lastTop   = -99999
$form.Top = $script:curTop

$script:slotSeen = @{}
function StackHeight {
    # How tall the tab stack is, so the meter can ride on top of it. Walks the files on disk UNION
    # the ones we have seen before, and treats an unreadable entry as still live for a grace period:
    # an atomic replace briefly hides a file from the directory listing, and counting that as "gone"
    # made this window lurch down a slot and back. Same reasoning as Stack-Sync.
    # Returns @(visibleCount, totalHeight, parkedCount). A tab that has been pushed off the top of
    # the dock reports zero height but keeps its slot, so it must not contribute a gap here either -
    # otherwise the meter floats a row higher for every tab it cannot see.
    $now = NowMs
    $count = 0; $sum = 0; $parked = 0
    try {
        $paths = New-Object System.Collections.Generic.HashSet[string]
        foreach ($x in [System.IO.Directory]::GetFiles($ns, "*.slot")) { [void]$paths.Add($x) }
        foreach ($x in @($script:slotSeen.Keys)) { [void]$paths.Add($x) }
        foreach ($f in $paths) {
            $e = $null
            try {
                $p = (Read-TextShared $f).Trim() -split ' '
                if ($p.Count -ge 5) { $e = @{ h = [int]$p[3]; beat = [int64]$p[4]; seen = $now }; $script:slotSeen[$f] = $e }
            } catch { }
            if ($null -eq $e) {
                $e = $script:slotSeen[$f]
                if ($null -ne $e -and (($now - $e.seen) -lt 5000)) { $count++; $sum += $e.h; continue }
                if ($null -ne $e) { $script:slotSeen.Remove($f) }
                continue
            }
            if (($now - $e.beat) -lt 2500) {
                if ($e.h -le 0) { $parked++ } else { $count++; $sum += $e.h }
            }
        }
    } catch {}
    return @($count, $sum, $parked)
}

# The meter used to carry its own copy of the daemon watchdog, and was the only thing that had one.
# It now lives in popup_common.ps1 (Ensure-HudDaemon), where the badges and the tint can reach it
# too - so the daemon is watched by everything on screen rather than by one process that the daemon
# was in turn the only watcher of.

# The whole readout is the button - the bars AND the text above them, not just the 62px of bar.
# The render works out how far left the text actually reached and records it here, so the target is
# whatever is on screen rather than a guess that goes stale when the countdown gets longer.
function Over-Bar {
    if ($script:slide -gt 2) { return $false }      # slid away: the handle is the only control now
    $cp = [System.Windows.Forms.Cursor]::Position
    $x0 = $form.Left + $script:hitL; $x1 = $form.Left + $script:hitR
    $y0 = $script:lastTop + $GLOW - 3               # a few px of slack: this is a target, not a seal
    return ($cp.X -ge $x0 -and $cp.X -lt $x1 -and $cp.Y -ge $y0 -and $cp.Y -lt ($y0 + $CONTENT_H + 6))
}

function RoundedPath($x, $y, $w, $h, $rad) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad*2
    $p.AddArc($x, $y, $d, $d, 180, 90); $p.AddArc(($x+$w-$d), $y, $d, $d, 270, 90)
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d, $d, 0, 90); $p.AddArc($x, ($y+$h-$d), $d, $d, 90, 90)
    $p.CloseFigure(); return $p
}
function BarColor($pct, $isStale) {
    if ($isStale)    { return [System.Drawing.Color]::FromArgb(120,120,126) }
    if ($pct -ge 85) { return [System.Drawing.Color]::FromArgb(240,80,70) }
    if ($pct -ge 60) { return [System.Drawing.Color]::FromArgb(255,176,0) }
    return [System.Drawing.Color]::FromArgb(0,205,120)
}

# The session bar is coloured by where the window is HEADING, not by how full it is - how full it is
# is what the bar's length already says. hal_usage fits a burn rate to the last twenty minutes of
# readings and works out whether that rate runs the window out before it resets; this paints that.
#
# Blue means the burn does not get you there. Green means you land about on the limit as the window
# rolls over - spending it exactly, which is fine. Past that you run out early, and the further past,
# the earlier. The ramp goes through amber rather than straight from green to red because blending
# those two in RGB passes through a muddy olive; amber is both cleaner and the obvious caution step.
$script:PaceStops = @(
    @(0.00,  60, 150, 255),    # blue
    @(0.50,   0, 205, 120),    # green   (same green the threshold colours use)
    @(0.75, 255, 176,   0),    # amber
    @(1.00, 240,  80,  70)     # red
)
function PaceColor($p, $isStale) {
    if ($isStale) { return [System.Drawing.Color]::FromArgb(120,120,126) }
    if ($p -lt 0) { $p = 0.0 } elseif ($p -gt 1) { $p = 1.0 }
    $i = 0
    while (($i -lt ($script:PaceStops.Count - 2)) -and ($p -gt $script:PaceStops[$i+1][0])) { $i++ }
    $a = $script:PaceStops[$i]; $b = $script:PaceStops[$i+1]
    $span = $b[0] - $a[0]
    $f = if ($span -le 0) { 0.0 } else { ($p - $a[0]) / $span }
    return [System.Drawing.Color]::FromArgb(
        [int][Math]::Round($a[1] + ($b[1] - $a[1]) * $f),
        [int][Math]::Round($a[2] + ($b[2] - $a[2]) * $f),
        [int][Math]::Round($a[3] + ($b[3] - $a[3]) * $f))
}

# Normalise utilization samples to 0..1 for the sparkline.
#
# Spans the samples' own min..max rather than 0..100, because a quiet twenty minutes covers about two
# points and against a full scale would be an invisible straight line pinned to the bottom. But it
# never zooms in past $minSpan: the reading only moves in whole points, so an unfloored auto-scale
# turns a single 38->39 tick into a full-height cliff and the chart screams about a rounding step.
#
# The floor is centred on the data's midpoint, which also disposes of the all-equal case for free -
# the span collapses below $minSpan, the window recentres, every sample comes back 0.5, and a flat
# twenty minutes draws as a flat line down the MIDDLE. Not along the bottom, which is what 0..100
# scaling would give and which reads as "you have used nothing".
#
# `,$out` so PowerShell returns the array whole instead of unrolling it into the pipeline - a
# Compact form for the readout: "43min", "2h14m", "now". The panel spells it out in full.
function ResetShort($iso) {
    if (-not $iso) { return "" }
    try {
        $mins = [int]((([datetime]$iso).ToUniversalTime() - [datetime]::UtcNow).TotalMinutes)
        if ($mins -le 0) { return "now" }
        if ($mins -lt 60) { return "${mins}m" }
        $h = [int][Math]::Floor($mins / 60)              # see MinsLong: [int] would round up
        if ($h -lt 24) { return "{0}h {1}m" -f $h, ($mins % 60) }
        return "{0}d {1}h" -f [int][Math]::Floor($h / 24), ($h % 24)
    } catch { return "" }
}
# Days once there are more than a day of them: the weekly window is often 150-odd hours out, and
# "6d 6h" is a length of time you can picture where "150h 12m" is a number you have to divide.
function MinsLong($mins) {
    if ($mins -le 0) { return "any moment" }
    if ($mins -lt 60) { return "$mins min" }
    # Floor, not [int]: PowerShell's int cast rounds to NEAREST, so [int](100/60) is 2 and an hour
    # and forty minutes rendered as "2h 40m". Wrong for any reading past the half hour.
    $h = [int][Math]::Floor($mins / 60)
    if ($h -lt 24) { return "{0}h {1}m" -f $h, ($mins % 60) }
    return "{0}d {1}h" -f [int][Math]::Floor($h / 24), ($h % 24)
}
function ResetIn($iso) {
    if (-not $iso) { return "" }
    try {
        return MinsLong ([int]((([datetime]$iso).ToUniversalTime() - [datetime]::UtcNow).TotalMinutes))
    } catch { return "" }
}

$render = {
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    if ($script:sessionPct -ge 0) {
        $sl = [int]$script:slide
        $barR = $GLOW + $OX + $UW + $sl
        $barL = $barR - $UW
        $sBarY = $GLOW + $UPCT_H + 2
        $wBarY = $sBarY + $SBAR_H + $GAP_BARS
        # The bar fills from the carried-forward value, so it creeps as you work rather than
        # jumping a whole point a minute; the number beside it is that same value, rounded.
        $live = if ($script:liveUtil -ge 0) { $script:liveUtil } else { [double]$script:sessionPct }
        $sp = [int][Math]::Floor($live + 0.5)
        if ($sp -gt 100) { $sp = 100 }
        $wp = if ($script:weeklyPct -ge 0) { [Math]::Min(100, $script:weeklyPct) } else { -1 }

        $trk = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(215, 38, 38, 42))
        $g.FillRectangle($trk, $barL, $sBarY, $UW, $SBAR_H)
        if ($wp -ge 0) { $g.FillRectangle($trk, $barL, $wBarY, $UW, $WBAR_H) }
        $trk.Dispose()

        $sw = [int]($UW * $live / 100.0)
        if ($sw -gt 0) {
            # Pace when we have enough readings to fit a rate to; the plain how-full thresholds for
            # the first few minutes after a cold start, when a made-up rate would be worse than none.
            $sc = if ($script:pace -ge 0) { PaceColor $script:pace $script:stale }
                  else { BarColor $sp $script:stale }
            $fb = New-Object System.Drawing.SolidBrush $sc
            $g.FillRectangle($fb, $barL, $sBarY, $sw, $SBAR_H); $fb.Dispose()
        }
        if ($wp -ge 0) {
            $ww = [int]($UW * $wp / 100.0)
            if ($ww -gt 0) {
                $wc = BarColor $wp $script:stale
                $wb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(170, $wc.R, $wc.G, $wc.B))
                $g.FillRectangle($wb, $barL, $wBarY, $ww, $WBAR_H); $wb.Dispose()
            }
        }
        $ptc = if ($script:stale) { [System.Drawing.Color]::FromArgb(170,170,176) }
               else { [System.Drawing.Color]::FromArgb(244,244,248) }
        # "7% - 4h 47m": where you stand, and how long you have to spend it. The percentage on its
        # own tells you the first and not the second.
        $head = if ($script:sessionLeft) { "$sp% - $($script:sessionLeft)" }
                elseif ($script:inferred) { "$sp% - new" }
                else { "$sp%" }
        if ($script:parked -gt 0) { $head = "+$($script:parked)  $head" }
        $hw = [int][Math]::Ceiling($g.MeasureString($head, $uFont).Width)
        $script:hitL = $barR - [Math]::Max($UW, $hw) - 4
        $script:hitR = $barR + 4

        # A layered window is hit-tested on alpha, so the gaps BETWEEN glyphs are holes a click falls
        # straight through - the readout would look like a button and behave like a colander. An
        # alpha of 3 over the whole thing is invisible on any display and makes every pixel live.
        # It doubles as the hover shape: at 26 it is a faint plate, which is the only affordance
        # left now that the tooltip is gone.
        $ha = if ($script:hot) { 26 } else { 3 }
        $hit = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($ha, 210, 210, 220))
        $hp = RoundedPath ($script:hitL) ($GLOW - 4) ($script:hitR - $script:hitL) ($CONTENT_H + 8) 4
        $g.FillPath($hit, $hp); $hit.Dispose(); $hp.Dispose()

        Draw-OutlinedText $g $head $uFont $ptc $barR ($GLOW + 1) 3 'right'
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 255)
    Assert-Topmost $form
    $bmp.Dispose()
}

# ── the detail panel ───────────────────────────────────────────────────────────────────────────
# Click the meter and this opens above it: the same numbers the bars encode, plus the ones they
# cannot - how fast tokens are actually going out, where the window lands, and the shape of the last
# twenty minutes at a size you can read.
#
# A second Form in this process rather than another overlay script. Application.Run pumps the
# THREAD, so a second borderless form on it is driven by the same loop, dies with its owner, and -
# the reason that matters - can just read $script:sessionPct and friends, which are already
# refreshed here every two seconds. A separate process would have to duplicate the whole formatting
# layer, invent a protocol to follow a window that eases every frame, and pay a PowerShell cold
# start on every click.
# Narrow and tall rather than wide and short. The three rate figures used to sit in three columns,
# which is what forced the width; as label-left / value-right rows they read at least as well and
# let the whole thing come in by nearly a hundred pixels.
$PANEL_W = 264; $PGLOW = 14; $PAD = 14; $COL = 236
# The panel is exactly as tall as what it has to show. Its content grows with the number of chats
# spending - four of them plus an Elsewhere row is five - and against a fixed height the chart simply
# ran off the bottom: drawn past the rounded background and clipped away by the edge of the canvas.
$PANEL_ROW_Y = 258       # first row of the spending list
$PANEL_ROW_H = 21
$PANEL_BARE_Y = 234      # where the chart's label goes when nothing is listed
$PANEL_FOOT = 12         # breathing room under the chart
$PANEL_SPEND_H = 40      # the master spend slider, under the chart
$script:budgetPath = Join-Path (Join-Path $env:USERPROFILE ".claude\shared") "budget.json"
$script:levels = @()     # ordered level keys from budget.json
$script:levelLabels = @()
$script:level = ""       # the one currently set
$script:spendRects = @() # where each segment was drawn, for the hit test
$script:badgePy = Join-Path $PSScriptRoot "hal_badge.py"
$script:panelH = 400
$PSPARK_W = 236; $PSPARK_H = 42; $PSPARK_MIN_SPAN = 5.0
$PSPARK_RATE_TOP = 0.4   # a quiet window still reads as quiet, not as amplified noise
$fHero  = New-Object System.Drawing.Font("Segoe UI", 26, [System.Drawing.FontStyle]::Bold)
$fBig   = New-Object System.Drawing.Font("Segoe UI", 14, [System.Drawing.FontStyle]::Bold)
$fVal   = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$fBody  = New-Object System.Drawing.Font("Segoe UI", 9)
$fEye   = New-Object System.Drawing.Font("Segoe UI", 8, [System.Drawing.FontStyle]::Bold)
$fMicro = New-Object System.Drawing.Font("Segoe UI", 8)
$DIM    = [System.Drawing.Color]::FromArgb(150,150,158)
$DIMMER = [System.Drawing.Color]::FromArgb(128,128,136)
$INK    = [System.Drawing.Color]::FromArgb(237,237,241)
$INKHI  = [System.Drawing.Color]::FromArgb(244,244,248)

$script:panelOpen = $false
$script:tpm = -1; $script:opm = -1        # weighted tokens/min, output tokens/min
$script:lbWas = $false                    # left button state last tick, for click-away

$panel = New-Object System.Windows.Forms.Form
$panel.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$panel.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$panel.ShowInTaskbar   = $false
$panel.TopMost         = $true
$panel.Width = $PANEL_W + $PGLOW*2; $panel.Height = $script:panelH + $PGLOW*2
$panel.Left = -20000; $panel.Top = -20000          # off-screen until first opened
$panel.Add_HandleCreated({ [PerPixelLayered]::NoActivate($panel.Handle) })
# Clicking a spend segment sets the master level for every session. The write goes through
# hal_badge.py rather than being done here: that file is mostly the coordinating session's, and a
# read-modify-write in PowerShell is one mistake away from discarding the parts that are not ours.
$panel.Add_MouseDown({
    param($sender, $e)
    $cp = [System.Windows.Forms.Cursor]::Position
    foreach ($r in @($script:spendRects)) {
        $x = $panel.Left + [int]$r[0]; $y = $panel.Top + $PGLOW + [int]$r[1]
        if ($cp.X -ge $x -and $cp.X -lt ($x + [int]$r[2]) -and
            $cp.Y -ge $y -and $cp.Y -lt ($y + [int]$r[3])) {
            $lvl = [string]$r[4]
            if ($lvl -ne $script:level) {
                $script:level = $lvl                      # optimistic, so the press feels immediate
                try {
                    $psi = New-Object System.Diagnostics.ProcessStartInfo
                    $psi.FileName = "python"
                    $psi.Arguments = '"{0}" --budget-level {1}' -f $script:badgePy, $lvl
                    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
                    [System.Diagnostics.Process]::Start($psi) | Out-Null
                } catch {}
                & $renderPanel
            }
            return
        }
    }
})

# Plain English for the colour. The bands are the ramp's own corners, so the words and the hue can
# never disagree about which side of "you will run out" you are on.
# Why the reading is not current, in the same slot the pace verdict uses. The HUD knew all of this
# already - which failure, how long until the next attempt - and made you go and read a cache file
# to find out. Every one of these is a state the data distinguishes; none of them is a guess.
function StateWords {
    $inS = ""
    if ($script:nextTry -gt 0) {
        $secs = [int][Math]::Ceiling(($script:nextTry - (NowMs)) / 1000.0)
        if ($secs -gt 0) { $inS = " - retrying in $(MinsLong ([int][Math]::Ceiling($secs / 60.0)))" }
    }
    if ($script:inferred) { return "New window - nothing spent in it yet." }
    if (-not $script:stale) { return "" }
    switch ($script:err) {
        "auth" { return "Signed out - renews the next time you use Claude." }
        "rate" { return "Rate limited$inS." }
        "net"  { return "Can't reach the endpoint$inS." }
    }
    $age = [int][Math]::Floor(((NowMs) - $script:readingTs) / 60000.0)
    return "Last known reading, $(MinsLong $age) old."
}

function PaceWords($p, $proj, $hit) {
    if ($p -lt 0) { return "Not enough readings yet to say." }
    if ($p -gt 0.5) {
        if ($hit -ge 0) { return "Running out early - limit in about $(MinsLong $hit)." }
        return "Running out before this window resets."
    }
    if ($p -ge 0.42) { return "Right on the line - set to use just about all of it." }
    if ($proj -ge 0) { return "Coasting - on pace for $proj% by reset." }
    return "Coasting."
}

$script:chatMeta = @{}
$script:chatMetaAt = 0
function Chat-Meta($sid) {
    # A chat's name and accent come from the same state file its tab draws from, so the panel and
    # the dock can never disagree about which colour is which chat. Cached for a few seconds: the
    # panel repaints whenever a value moves, and this is a file read per chat.
    $now = NowMs
    if (($now - $script:chatMetaAt) -gt 4000) { $script:chatMeta = @{}; $script:chatMetaAt = $now }
    if ($script:chatMeta.ContainsKey($sid)) { return $script:chatMeta[$sid] }
    $m = @{ label = $sid; color = [System.Drawing.Color]::FromArgb(150,150,158) }
    # Everything without a tab, summed: a session run from a terminal, a chat closed since it wrote,
    # a scratch run in a temp folder. Named rather than shown as hex nobody can act on, and left
    # deliberately colourless - it is a remainder, not a chat you can click through to.
    if ($sid -eq "other") {
        $m.label = "Other"
        $m.color = [System.Drawing.Color]::FromArgb(128,128,136)
        $script:chatMeta[$sid] = $m
        return $m
    }
    # Not a chat at all: spend the plan charged for that nothing on this machine wrote down.
    if ($sid -eq "elsewhere") {
        $m.label = "Elsewhere"
        $m.color = [System.Drawing.Color]::FromArgb(150,140,120)
        $script:chatMeta[$sid] = $m
        return $m
    }
    try {
        $st = Read-JsonFile (Join-Path $badgesDir "$sid.json")
        if ($st) {
            if ($st.label) { $m.label = [string]$st.label }
            if ($st.color -and $st.color.Count -ge 3) {
                $m.color = [System.Drawing.Color]::FromArgb([int]$st.color[0], [int]$st.color[1], [int]$st.color[2])
            }
        }
    } catch { }
    $script:chatMeta[$sid] = $m
    return $m
}

# "Thu", or "Thu 4th" if it is more than a week out - a weekday alone would be ambiguous.
function Day-Of($ms) {
    try {
        $d = [System.DateTimeOffset]::FromUnixTimeMilliseconds([int64]$ms).LocalDateTime
        if (($d - [datetime]::Now).TotalDays -ge 6) { return $d.ToString("ddd d MMM") }
        return $d.ToString("ddd")
    } catch { return "" }
}

function Fmt-Count($n) {
    if ($n -lt 0) { return "-" }
    if ($n -ge 1000000) { return "{0:N1}M" -f ($n / 1000000.0) }
    if ($n -ge 1000)    { return "{0:N0}k" -f ($n / 1000.0) }
    return "$n"
}

# How many rows the spending list will draw, and therefore where the chart starts and how tall the
# whole thing has to be. Worked out before the bitmap exists, because the bitmap's size depends on it.
function Panel-Rows {
    $n = @($script:byChat).Count
    if ($script:elsewhere -gt 0) { $n++ }
    return $n
}
function Panel-ChartY {
    $n = Panel-Rows
    if ($n -gt 0) { return $PANEL_ROW_Y + $PANEL_ROW_H * $n + 14 }
    return $PANEL_BARE_Y + 14
}
function Panel-Height {
    $h = (Panel-ChartY) + 14 + $PSPARK_H + $PANEL_FOOT
    if ($script:levels.Count -gt 0) { $h += $PANEL_SPEND_H }
    return $h
}

$renderPanel = {
    $script:panelH = Panel-Height
    PanelPlace                                       # the top moves when the height does

    $bmp = New-Object System.Drawing.Bitmap(($PANEL_W + $PGLOW*2), ($script:panelH + $PGLOW*2),
                                            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)
    $ox = $PGLOW; $oy = $PGLOW                       # panel-local (0,0) in bitmap coords

    $chip = RoundedPath $ox $oy $PANEL_W $script:panelH 8
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(246,26,26,28))
    $g.FillPath($bg, $chip); $bg.Dispose()
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(120,200,200,210)), 1
    $g.DrawPath($pen, $chip); $pen.Dispose(); $chip.Dispose()

    $sp = [Math]::Max(0, $script:sessionPct)
    $accent = if ($script:pace -ge 0) { PaceColor $script:pace $script:stale }
              else { BarColor $sp $script:stale }
    $L = $ox + $PAD; $R = $ox + $PANEL_W - $PAD

    function TxtL($s, $f, $c, $x, $inkTop) {
        $b = New-Object System.Drawing.SolidBrush $c
        $g.DrawString($s, $f, $b, [float]$x, [float]($oy + $inkTop - 3)); $b.Dispose()
    }
    function TxtR($s, $f, $c, $xr, $inkTop) {
        $w = [int][Math]::Ceiling($g.MeasureString($s, $f).Width)
        $b = New-Object System.Drawing.SolidBrush $c
        $g.DrawString($s, $f, $b, [float]($xr - $w + 3), [float]($oy + $inkTop - 3)); $b.Dispose()
    }

    # header
    TxtL "SESSION - 5 HOURS" $fEye $DIM $L 12
    $age = if ($script:stale) { "reading is stale" } else { "" }
    if ($age) { TxtR $age $fMicro ([System.Drawing.Color]::FromArgb(240,80,70)) $R 12 }

    # the one number a glance should land on
    Draw-OutlinedText $g "$sp" $fHero $INKHI ($L) ($oy + 30) 3 'left'
    $hw = [int][Math]::Ceiling($g.MeasureString("$sp", $fHero).Width)
    Draw-OutlinedText $g "%" $fBig $INKHI ($L + $hw - 4) ($oy + 46) 2 'left'
    TxtR "resets in" $fMicro $DIM $R 34
    $left = if ($script:stale) { "-" } else { ResetIn $script:sessionResets }
    if (-not $left) { $left = "-" }
    Draw-OutlinedText $g $left $fBig $INK $R ($oy + 46) 2 'right'

    # the bar, with a ghost showing where the burn lands it by reset
    $trk = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(215,38,38,42))
    $bar = RoundedPath $L ($oy + 68) $COL 10 5
    $g.FillPath($trk, $bar); $trk.Dispose()
    if ($script:projected -gt $sp -and -not $script:stale) {
        $gw = [int]($COL * [Math]::Min(100, $script:projected) / 100.0)
        if ($gw -gt 0) {
            $gb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(70, $accent.R, $accent.G, $accent.B))
            $old = $g.Clip; $g.SetClip($bar)
            $g.FillRectangle($gb, $L, ($oy + 68), $gw, 10); $gb.Dispose(); $g.Clip = $old
        }
    }
    $fw = [int]($COL * [Math]::Min(100, $sp) / 100.0)
    if ($fw -gt 0) {
        $fb = New-Object System.Drawing.SolidBrush $accent
        $old = $g.Clip; $g.SetClip($bar)
        $g.FillRectangle($fb, $L, ($oy + 68), $fw, 10); $fb.Dispose(); $g.Clip = $old
    }
    $bar.Dispose()

    # what that means, in words
    $db = New-Object System.Drawing.SolidBrush $accent
    $g.FillEllipse($db, [float]$L, [float]($oy + 89), 7, 7); $db.Dispose()
    # Wraps rather than running off the edge - the panel is narrower than the sentence can be.
    $verdict = StateWords
    if (-not $verdict) { $verdict = PaceWords $script:pace $script:projected $script:hitMins }
    $words = $verdict -split ' '
    $line = ""; $ly = 87; $wrapW = $COL - 13
    foreach ($w in $words) {
        $try = if ($line) { "$line $w" } else { $w }
        if ([int][Math]::Ceiling($g.MeasureString($try, $fBody).Width) -gt $wrapW -and $line) {
            TxtL $line $fBody $INK ($L + 13) $ly; $ly += 14; $line = $w
        } else { $line = $try }
    }
    if ($line) { TxtL $line $fBody $INK ($L + 13) $ly }

    $hair = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(30,200,200,210)), 1
    $g.DrawLine($hair, $L, ($oy + 113), $R, ($oy + 113))
    $g.DrawLine($hair, $L, ($oy + 155), $R, ($oy + 155))
    $hair.Dispose()

    # weekly: rarely the binding limit, so it gets a line and a sliver
    TxtL "WEEKLY" $fEye $DIM $L 122
    if ($script:weeklyPct -ge 0) {
        $wIn = ResetIn $script:weeklyResets
        $wtxt = if ($wIn) { "$($script:weeklyPct)%   -   $wIn" } else { "$($script:weeklyPct)%" }
        TxtR $wtxt $fBody ([System.Drawing.Color]::FromArgb(188,188,196)) $R 122
        # Where the week lands at the rate so far. Averaged over days, which is the one case where a
        # plain average is a fair projection - nothing dominates a week the way one burst dominates
        # a five-hour window. Silent for the window's first few hours, when it would be noise.
        if ($script:weeklyHit -gt 0) {
            TxtL ("out " + (Day-Of $script:weeklyHit)) $fMicro ([System.Drawing.Color]::FromArgb(240,150,90)) ($L + 50) 123
        } elseif ($script:weeklyProj -ge 0) {
            TxtL "-> $($script:weeklyProj)%" $fMicro $DIMMER ($L + 50) 123
        }
        $wt = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(215,38,38,42))
        $wbar = RoundedPath $L ($oy + 136) $COL 5 2
        $g.FillPath($wt, $wbar); $wt.Dispose()
        $ww = [int]($COL * [Math]::Min(100, $script:weeklyPct) / 100.0)
        if ($ww -gt 0) {
            $wc = BarColor $script:weeklyPct $script:stale
            $wb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(170, $wc.R, $wc.G, $wc.B))
            $old = $g.Clip; $g.SetClip($wbar)
            $g.FillRectangle($wb, $L, ($oy + 136), $ww, 5); $wb.Dispose(); $g.Clip = $old
        }
        $wbar.Dispose()
    }

    # Three rows, label left and value right. In a narrow panel that reads better than columns and
    # each value gets the whole width it needs instead of a third of it.
    $burnTxt = if ($script:burn -ge 0) { "{0:N2} %/min" -f $script:burn } else { "-" }
    $tokTxt  = if ($script:tpm -ge 0) { (Fmt-Count $script:tpm) + " /min" } else { "-" }
    $projTxt = if ($script:projected -ge 0 -and -not $script:stale) { "$($script:projected)%" } else { "-" }
    $rows = @(@("BURN", $burnTxt, $INK), @("TOKENS", $tokTxt, $INK), @("AT RESET", $projTxt, $accent))
    $ry = 164
    foreach ($row in $rows) {
        TxtL $row[0] $fEye $DIM $L ($ry + 2)
        Draw-OutlinedText $g $row[1] $fVal $row[2] $R ($oy + $ry) 1.5 'right'
        $ry += 20
    }

    # Which chats are actually spending it. The HUD has always known both halves of this - what each
    # tab is, and what the window costs - and never put them next to each other.
    # Anything the plan charged for that no transcript here explains - the desktop app, the web app,
    # another machine. Without it the panel shows a red-hot bar over a list of chats that plainly are
    # not doing it, which looks like the panel is broken rather than like usage happening elsewhere.
    $rows2 = @($script:byChat)
    if ($script:elsewhere -gt 0) {
        $rows2 = @($rows2) + @(, @("elsewhere", $script:elsewhere))
        $rows2 = @($rows2 | Sort-Object -Property @{ Expression = { [double]$_[1] } } -Descending)
    }
    if ($rows2.Count -gt 0) {
        TxtL "SPENDING IT" $fEye $DIM $L 240
        $topRate = [double]$rows2[0][1]
        $yy = $PANEL_ROW_Y
        foreach ($row in $rows2) {
            $sid = [string]$row[0]; $rate = [double]$row[1]
            $meta = Chat-Meta $sid
            $dot = New-Object System.Drawing.SolidBrush $meta.color
            $g.FillEllipse($dot, [float]$L, [float]($oy + $yy - 7), 6, 6); $dot.Dispose()
            $nm = [string]$meta.label
            while ($nm.Length -gt 1 -and
                   [int][Math]::Ceiling($g.MeasureString($nm, $fBody).Width) -gt ($COL - 100)) {
                $nm = $nm.Substring(0, $nm.Length - 1)
            }
            TxtL $nm $fBody $INK ($L + 12) ($yy - 8)
            TxtR ((Fmt-Count ([int]$rate)) + " /min") $fMicro $DIMMER $R ($yy - 7)
            # A bar apiece, relative to the greediest, so the split reads at a glance.
            if ($topRate -gt 0) {
                $bw = [int]($COL * $rate / $topRate)
                if ($bw -lt 2) { $bw = 2 }
                $sb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(90, $meta.color.R, $meta.color.G, $meta.color.B))
                $g.FillRectangle($sb, $L, ($oy + $yy + 2), $bw, 2); $sb.Dispose()
            }
            $yy += $PANEL_ROW_H
        }
    } else { $yy = $PANEL_BARE_Y }

    # The whole window, not the twenty minutes the burn fit happens to need - that span was never a
    # display decision. Falls back to the fit's own samples before the first minute has elapsed.
    # The rate, not the total. How full the window is is what the bar's length says; drawing it
    # again here answered the wrong question, and read as "still climbing" through an idle hour
    # because a cumulative line never comes back down.
    # Drawn from the token tailer, which records every call with its real timestamp and sees it
    # within five seconds - so a question you just asked actually appears. The plan's utilization
    # cannot do this: whole points, no oftener than 45s, which is minutes of latency in a staircase.
    # It falls back to that coarse series only when the tailer has nothing to say.
    #
    # This is spend measured on THIS machine. Anything running elsewhere is in the Elsewhere row
    # above rather than in the line, which is why the two can disagree.
    $h = @($script:series); $tokUnits = $true
    if ($h.Count -lt $SPARK_MIN) { $h = @($script:rates); $tokUnits = $false }
    $span = ""
    if ($h.Count -ge 2) {
        try { $span = MinsLong ([int](([double]$h[$h.Count-1][0] - [double]$h[0][0]) / 60000.0)) } catch { }
    }
    $chartLbl = if ($span) { "LAST $span" } else { "THIS SESSION" }
    $chartY = Panel-ChartY
    TxtL $chartLbl $fEye $DIM $L $chartY
    if ($h.Count -ge $SPARK_MIN) {
        $hi = 0.0
        foreach ($p in $h) { $v = [double]$p[1]; if ($v -gt $hi) { $hi = $v } }
        # "peak", not a range: the line is an instantaneous rate and its high point is many times
        # the ten-minute average in the TOKENS row above. Saying which is which stops the two
        # numbers looking like they contradict each other.
        $unit = if ($tokUnits) { "peak " + (Fmt-Count ([int]$hi)) + " /min" } else { "0 - {0:N1} %/min" -f $hi }
        TxtR $unit $fMicro $DIMMER $R $chartY
        Draw-Spark2 $g $h $L ($oy + $chartY + 14) $PSPARK_W $PSPARK_H $accent $true
    } else {
        TxtL "not enough readings yet" $fMicro $DIMMER ($L + 92) $chartY
    }

    # Master spend level, one control for every session. It lives here rather than on the dock
    # because it is a setting you change occasionally, not a reading you watch - and the dock is
    # already carrying everything that has to be glanceable.
    $script:spendRects = @()
    if ($script:levels.Count -gt 0) {
        $sy = (Panel-ChartY) + 14 + $PSPARK_H + 14
        TxtL "SPEND" $fEye $DIM $L $sy
        $segY = $sy + 13
        $segH = 17
        $segW = [int]($COL / $script:levels.Count)
        for ($i = 0; $i -lt $script:levels.Count; $i++) {
            $on = ($script:levels[$i] -eq $script:level)
            $sx = $L + $i * $segW
            $w = if ($i -eq $script:levels.Count - 1) { $COL - $i * $segW } else { $segW - 2 }
            $sp2 = RoundedPath $sx ($oy + $segY) $w $segH 3
            $fill = if ($on) { [System.Drawing.Color]::FromArgb(235, $accent.R, $accent.G, $accent.B) }
                    else { [System.Drawing.Color]::FromArgb(70, 90, 90, 100) }
            $sb2 = New-Object System.Drawing.SolidBrush $fill
            $g.FillPath($sb2, $sp2); $sb2.Dispose(); $sp2.Dispose()
            $ink2 = if ($on) { [System.Drawing.Color]::FromArgb(16,16,18) } else { $DIM }
            $tw2 = [int][Math]::Ceiling($g.MeasureString($script:levelLabels[$i], $fMicro).Width)
            Draw-OutlinedText $g $script:levelLabels[$i] $fMicro $ink2 `
                              ([int]($sx + ($w - $tw2) / 2)) ($oy + $segY + 3) 0 'left'
            $script:spendRects += ,@($sx, $segY, $w, $segH, $script:levels[$i])
        }
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($panel.Handle, $bmp, $panel.Left, $panel.Top, 255)
    $bmp.Dispose()
}

# The panel's chart. Same normalisation as the meter's, but wider, with the area under the line
# filled - at 42px tall the shape reads better as a mass than as a stroke - and a marked "now".
# Normalise utilization samples to 0..1 for the sparkline.
#
# Spans the samples' own min..max rather than 0..100, because a quiet twenty minutes covers about two
# points and against a full scale would be an invisible straight line pinned to the bottom. But it
# never zooms in past $minSpan: the reading only moves in whole points, so an unfloored auto-scale
# turns a single 38->39 tick into a full-height cliff and the chart screams about a rounding step.
#
# The floor is centred on the data's midpoint, which also disposes of the all-equal case for free -
# the span collapses below $minSpan, the window recentres, every sample comes back 0.5, and a flat
# twenty minutes draws as a flat line down the MIDDLE. Not along the bottom, which is what 0..100
# scaling would give and which reads as "you have used nothing".
#
# `,$out` so PowerShell returns the array whole instead of unrolling it into the pipeline - a
# one-element result would otherwise come back as a bare double.
function SparkNorm($us, $minSpan) {
    $n = @($us).Count
    if ($n -lt 1) { return ,@() }
    $lo = [double]$us[0]; $hi = $lo
    foreach ($u in $us) { $v = [double]$u; if ($v -lt $lo) { $lo = $v }; if ($v -gt $hi) { $hi = $v } }
    if (($hi - $lo) -lt $minSpan) {
        $mid = ($hi + $lo) / 2.0
        $lo = $mid - $minSpan / 2.0; $hi = $mid + $minSpan / 2.0
    }
    $span = $hi - $lo
    $out = New-Object 'double[]' $n
    for ($i = 0; $i -lt $n; $i++) {
        $f = ([double]$us[$i] - $lo) / $span
        if ($f -lt 0) { $f = 0.0 } elseif ($f -gt 1) { $f = 1.0 }
        $out[$i] = $f
    }
    return ,$out
}

function SparkNormZero($us, $minTop) {
    # Floored at zero, because for a rate zero is a real value and the whole point of the chart:
    # idle has to sit flat on the baseline, not be stretched to fill the box like everything else.
    $n = @($us).Count
    if ($n -lt 1) { return ,@() }
    $hi = [double]$minTop
    foreach ($u in $us) { $v = [double]$u; if ($v -gt $hi) { $hi = $v } }
    if ($hi -le 0) { $hi = 1.0 }
    $out = New-Object 'double[]' $n
    for ($i = 0; $i -lt $n; $i++) {
        $f = [double]$us[$i] / $hi
        if ($f -lt 0) { $f = 0.0 } elseif ($f -gt 1) { $f = 1.0 }
        $out[$i] = $f
    }
    return ,$out
}

function Draw-Spark2($g, $hist, $x, $y, $w, $h, $col, $fromZero = $false) {
    $n = @($hist).Count
    if ($n -lt 2) { return }
    try {
        $ts = New-Object 'double[]' $n; $us = New-Object 'double[]' $n
        for ($i = 0; $i -lt $n; $i++) { $ts[$i] = [double]$hist[$i][0]; $us[$i] = [double]$hist[$i][1] }
        $f = if ($fromZero) { SparkNormZero $us $PSPARK_RATE_TOP } else { SparkNorm $us $PSPARK_MIN_SPAN }
        $t0 = $ts[0]; $dt = $ts[$n-1] - $t0
        $pts = New-Object 'System.Drawing.PointF[]' $n
        for ($i = 0; $i -lt $n; $i++) {
            $fx = if ($dt -le 0) { $i / [double]($n - 1) } else { ($ts[$i] - $t0) / $dt }
            $pts[$i] = [System.Drawing.PointF]::new([single]($x + $fx * ($w - 1)),
                                                    [single]($y + ($h - 1) - $f[$i] * ($h - 1)))
        }
        $poly = New-Object 'System.Drawing.PointF[]' ($n + 2)
        for ($i = 0; $i -lt $n; $i++) { $poly[$i] = $pts[$i] }
        $poly[$n]     = [System.Drawing.PointF]::new([single]($x + $w - 1), [single]($y + $h))
        $poly[$n + 1] = [System.Drawing.PointF]::new([single]$x, [single]($y + $h))
        $fill = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(30, $col.R, $col.G, $col.B))
        $g.FillPolygon($fill, $poly); $fill.Dispose()
        $pen = New-Object System.Drawing.Pen $col, 1.8
        $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
        $g.DrawLines($pen, $pts); $pen.Dispose()
        $ring = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255,26,26,28))
        $g.FillEllipse($ring, [single]($pts[$n-1].X - 5), [single]($pts[$n-1].Y - 5), 10, 10); $ring.Dispose()
        $dot = New-Object System.Drawing.SolidBrush $col
        $g.FillEllipse($dot, [single]($pts[$n-1].X - 3.5), [single]($pts[$n-1].Y - 3.5), 7, 7); $dot.Dispose()
    } catch { }
}

# Sits directly above the meter, right edges aligned, clamped on screen.
function PanelPlace {
    $barR = $form.Left + $GLOW + $OX + $UW
    $panel.Left = [int]($barR - $PANEL_W - $PGLOW)
    $t = [int]($script:lastTop + $GLOW - 8 - $script:panelH - $PGLOW)
    if ($t -lt 4) { $t = 4 }
    $panel.Top = $t
}

$openPanel = {
    try {
        PanelPlace
        $script:panelOpen = $true
        $panel.Show()
        [PerPixelLayered]::InitClickable($panel.Handle)   # after Show: it drops TOPMOST, so re-assert
        Assert-Topmost $panel
        & $renderPanel
    } catch { $script:panelOpen = $false }
}
$closePanel = {
    $script:panelOpen = $false
    try { $panel.Hide() } catch {}
}

$form.Add_HandleCreated({ [PerPixelLayered]::NoActivate($form.Handle) })   # focus stays in the editor
$form.Add_Shown({ [PerPixelLayered]::InitClickable($form.Handle); Assert-Topmost $form; & $render })
# The meter is a button now. Only the drawn pixels are clickable - a layered window is
# hit-tested against its alpha - so gate on the readout's own rect.
$form.Add_MouseDown({
    param($sender, $e)
    # Either button. A tab's right-click stows it, so that one is spoken for - the readout has no
    # second meaning to collide with, and a control that ignores half your clicks is just annoying.
    if (-not (Over-Bar)) { return }
    $script:lbWas = $true            # this press must not also read as a click-away
    if ($script:panelOpen) { & $closePanel } else { & $openPanel }
})

$script:lastVs = NowMs
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 200
$timer.Add_Tick({
    if ($script:closeReq) { $form.Close(); return }
    $nowMs = NowMs

    if ($nowMs - $script:lastStack -ge 200) {
        $script:lastStack = $nowMs
        $info = StackHeight; $cnt = $info[0]; $sum = $info[1]
        $bBottom = if ($cnt -eq 0) { $dockBottom } else { $dockBottom - ($sum + ($cnt - 1) * $GAPB) - $GAPB }
        $script:targetTop = [int]($bBottom - $CONTENT_H - $GLOW)
        if ($info[2] -ne $script:parked) { $script:parked = $info[2]; & $render }   # "+N" tabs hidden
    }

    if ($nowMs - $script:lastUsage -ge 2000) {
        $script:lastUsage = $nowMs
        $j = Read-JsonFile $usageFile
        $s = -1; $w = -1; $sr = ""; $wr = ""; $st = $false
        $pc = -1.0; $pj = -1; $hm = -1; $hs = @(); $bn = -1.0; $inf = $false
        if ($j) {
            if ($null -ne $j.session_pct) { $s = [int]$j.session_pct; $sr = [string]$j.session_resets }
            if ($null -ne $j.weekly_pct)  { $w = [int]$j.weekly_pct;  $wr = [string]$j.weekly_resets }
            try { $st = ($nowMs - [int64]$j.ts) -gt 900000 } catch { $st = $true }
            # null until there are enough readings to fit a rate to - the meter falls back rather
            # than inventing a trend from two samples.
            try { if ($null -ne $j.pace)      { $pc = [double]$j.pace } }     catch { $pc = -1.0 }
            try { if ($null -ne $j.projected) { $pj = [int]$j.projected } }   catch { $pj = -1 }
            try { if ($null -ne $j.hit_mins)  { $hm = [int]$j.hit_mins } }    catch { $hm = -1 }
            try { if ($null -ne $j.burn)      { $bn = [double]$j.burn } }     catch { $bn = -1.0 }
            try { $inf = [bool]$j.inferred } catch { $inf = $false }
            try { $script:err = [string]$j.err } catch { $script:err = "" }
            try { $script:nextTry = [int64]$j.next_try } catch { $script:nextTry = 0 }
            try { $script:readingTs = [int64]$j.ts } catch { $script:readingTs = 0 }
            try { $script:weeklyProj = if ($null -ne $j.weekly_projected) { [int]$j.weekly_projected } else { -1 } } catch { $script:weeklyProj = -1 }
            try { $script:weeklyHit = if ($null -ne $j.weekly_hit) { [int64]$j.weekly_hit } else { 0 } } catch { $script:weeklyHit = 0 }
            try { $script:elsewhere = if ($null -ne $j.elsewhere) { [int]$j.elsewhere } else { 0 } } catch { $script:elsewhere = 0 }
            # The readings behind the burn rate, for the sparkline. Guard the null explicitly:
            # @($null).Count is 1 in PowerShell 5.1, so the usual @(...) idiom would report one
            # phantom sample and the draw would then die indexing into it.
            try {
                if ($null -ne $j.history) {
                    $hs = @(@($j.history) | Where-Object { $null -ne $_ -and @($_).Count -ge 2 })
                }
            } catch { $hs = @() }
        }
        # A run of identical readings - 38, 38, 38 - moves none of the values above while the history
        # keeps growing, and a flat window is exactly when that happens. So the chart needs its own
        # change signal, or it would freeze precisely when it has something to say.
        $hsig = ""
        if ($hs.Count -gt 0) { $hsig = "$($hs.Count):$($hs[$hs.Count - 1][0])" }
        $script:hist = $hs
        # Tokens come from the transcripts on their own, much faster clock, so they are their own
        # file and their own staleness - a frozen OAuth reading does not make them wrong.
        $tp = -1; $op = -1; $tot = $null; $bc = @()
        $tj = Read-JsonFile $tokensFile
        if ($tj) {
            try {
                if (($nowMs - [int64]$tj.ts) -lt 60000) {
                    $tp = [int]$tj.tpm; $op = [int]$tj.opm; $tot = [double]$tj.total
                    if ($null -ne $tj.by_chat) { $bc = @($tj.by_chat) }
                    if ($null -ne $tj.series) { $script:series = @($tj.series) }
                }
            } catch {}
        }
        # The endpoint answers in whole points every 45s at best. Between answers we know exactly
        # what has been spent locally, so carry the last real reading forward with it - and snap
        # back to the truth the moment a fetch lands. Falls back to the plain reading when there is
        # no calibration yet, rather than to a guess.
        $lu = -1.0
        if ($null -ne $tot -and $j) {
            try {
                # Both of these come from hal_usage rather than being copied here. A constant kept
                # in two languages is a constant that will disagree with itself eventually.
                $per = 0.0
                try { if ($null -ne $j.per_pt_default) { $per = [double]$j.per_pt_default } } catch {}
                try { if ($null -ne $j.per_pt -and [double]$j.per_pt -gt 0) { $per = [double]$j.per_pt } } catch {}
                $au = [double]$j.anchor_util; $at = [double]$j.anchor_tok
                if ($per -gt 0) {
                    $extra = ($tot - $at) / $per
                    if ($extra -lt 0) { $extra = 0.0 }
                    $lu = [Math]::Max(0.0, [Math]::Min(100.0, $au + $extra))
                }
            } catch { $lu = -1.0 }
        }
        # The shared spend contract, if this machine has one. Read only; the slider writes it back
        # through hal_badge.py so the pm-managed parts of the document cannot be clobbered here.
        $bj = Read-JsonFile $script:budgetPath
        if ($bj -and $bj.levels) {
            try {
                $ordered = @($bj.levels.PSObject.Properties | Sort-Object { [int]$_.Value.rank })
                $script:levels = @($ordered | ForEach-Object { $_.Name })
                $script:levelLabels = @($ordered | ForEach-Object { [string]$_.Value.label })
                $script:level = [string]$bj.level
            } catch { $script:levels = @(); $script:levelLabels = @() }
        }
        $lg = @(); $rt = @()
        try { if ($j -and $null -ne $j.long)  { $lg = @($j.long) } }  catch { $lg = @() }
        try { if ($j -and $null -ne $j.rates) { $rt = @($j.rates) } } catch { $rt = @() }
        $script:byChat = $bc; $script:long = $lg; $script:rates = $rt
        if ($tp -ne $script:tpm -or $op -ne $script:opm -or
            [Math]::Abs($lu - $script:liveUtil) -ge 0.05) {
            $script:tpm = $tp; $script:opm = $op; $script:liveUtil = $lu
            if ($script:panelOpen) { try { & $renderPanel } catch {} }
        }
        $left = if ($st) { "" } else { ResetShort $sr }     # a stale reading has no honest countdown
        if ($s -ne $script:sessionPct -or $w -ne $script:weeklyPct -or $st -ne $script:stale -or
            $sr -ne $script:sessionResets -or $wr -ne $script:weeklyResets -or
            $left -ne $script:sessionLeft -or $pc -ne $script:pace -or
            $pj -ne $script:projected -or $hm -ne $script:hitMins -or $bn -ne $script:burn -or
            $inf -ne $script:inferred -or
            ($script:hot -and $hsig -ne $script:histSig)) {   # a chart nobody is looking at can wait
            $script:histSig = $hsig
            $script:sessionPct = $s; $script:weeklyPct = $w; $script:stale = $st
            $script:sessionResets = $sr; $script:weeklyResets = $wr
            $script:sessionLeft = $left
            $script:pace = $pc; $script:projected = $pj; $script:hitMins = $hm
            $script:burn = $bn; $script:inferred = $inf
            & $render
            if ($script:panelOpen) { try { & $renderPanel } catch {} }
        }
    }

    if ($nowMs - $script:lastPresence -ge 1000) {
        $script:lastPresence = $nowMs
        if (-not (Hud-Enabled)) { $form.Close(); return }
        if ([PerPixelLayered]::FindWindowEndsWith("Visual Studio Code") -ne [IntPtr]::Zero) { $script:lastVs = NowMs }
        elseif (($nowMs - $script:lastVs) -gt 30000) { $form.Close(); return }
    }
    if ($nowMs - $script:lastDaemon -ge 3000) { $script:lastDaemon = $nowMs; Ensure-HudDaemon; Assert-Topmost $form }

    # The dock slide is not ours to ease - every tab and this meter take the same number from the
    # same shared clock, which is the only way separate processes leave the screen together.
    $script:slide = Dock-Offset
    if ([Math]::Abs($script:slide - $script:lastSlide) -ge 0.5) { $script:lastSlide = $script:slide; & $render }
    if ($script:slide -gt 2 -and $script:panelOpen) { & $closePanel }

    $over = Over-Bar
    if ($over -ne $script:hot) { $script:hot = $over; & $render }

    # The panel rides above the meter, which itself eases as the tab stack changes height, so it has
    # to follow. Click anywhere that is neither the button nor the panel and it goes away; the press
    # that opened it is marked so the same press cannot immediately close it again.
    $lb = ([System.Windows.Forms.Control]::MouseButtons -band
           ([System.Windows.Forms.MouseButtons]::Left -bor [System.Windows.Forms.MouseButtons]::Right)) -ne 0
    if ($script:panelOpen) {
        try {
            $wasL = $panel.Left; $wasT = $panel.Top
            PanelPlace
            if ($panel.Left -ne $wasL -or $panel.Top -ne $wasT) { & $renderPanel }
        } catch {}
        if ($lb -and -not $script:lbWas) {
            $cp = [System.Windows.Forms.Cursor]::Position
            $inP = ($cp.X -ge ($panel.Left + $PGLOW) -and $cp.X -lt ($panel.Left + $PGLOW + $PANEL_W) -and
                    $cp.Y -ge ($panel.Top + $PGLOW)  -and $cp.Y -lt ($panel.Top + $PGLOW + $script:panelH))
            if (-not $inP -and -not $over) { & $closePanel }
        }
    }
    $script:lbWas = $lb

    $delta = $script:targetTop - $script:curTop
    if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:targetTop } else { $script:curTop += $delta * 0.22 }
    $newTop = [int]$script:curTop
    if ($newTop -ne $script:lastTop) {
        $script:lastTop = $newTop
        $form.Top = $newTop
        [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
    }

    $want = if (Dock-Moving) { 15 }
            elseif (([Math]::Abs($script:targetTop - $script:curTop) -ge 0.5) -or $script:hot -or $script:panelOpen) { 30 }
            else { 200 }
    if ($want -ne $script:curInterval) { $script:curInterval = $want; $timer.Interval = $want }
    if ($nowMs - $script:lastBeat -ge 600) { $script:lastBeat = $nowMs; Write-Beat $AliveFile }
})
$timer.Start()

[System.Windows.Forms.Application]::Run($form)
