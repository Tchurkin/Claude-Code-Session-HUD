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
# Click-through: this is a readout, not a control, so it never intercepts a click. The hover hint
# still works, because hover is detected by polling the cursor rather than by mouse events.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
if (-not $script:PplReady) { exit 1 }   # no drawing type -> exit so the supervisor respawns a working one

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_usage_meter", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$GLOW = 10
$TIP_W = 600                                       # room to the LEFT for the hover hint
$OX = $TIP_W
$UW = 62                                           # bar width
$UPCT_H = 15                                       # room for the percentage text
$SBAR_H = 6                                        # session bar (the one that matters)
$WBAR_H = 3                                        # weekly bar
$GAP_BARS = 3
$CONTENT_H = $UPCT_H + 2 + $SBAR_H + $GAP_BARS + $WBAR_H
$FORM_W = $UW + $GLOW*2 + $TIP_W
$FORM_H = $CONTENT_H + $GLOW*2
$uFont   = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$tipFont = New-Object System.Drawing.Font("Segoe UI", 9)

# The sparkline in the hover hint: the last twenty minutes of readings, which hal_usage already keeps
# for the burn-rate fit and would otherwise throw away. No axes, no gridlines, no track - just the
# shape, so "on pace for 46%" comes with the thing it was worked out from.
$SPARK_W = 60; $SPARK_H = 14; $SPARK_GAP = 8
$SPARK_MIN = 4          # deliberately hal_usage.MIN_SAMPLES: the chart appears with the pace clause
$SPARK_MIN_SPAN = 2.5   # utilization points; never zoom in past this (see SparkNorm)

$script:hot = $false; $script:closeReq = $false
$script:sessionPct = -1; $script:weeklyPct = -1
$script:sessionResets = ""; $script:weeklyResets = ""
$script:sessionLeft = ""   # "2h14m" until the session window rolls over
$script:stale = $false
$script:hist = @()         # recent [ms, utilization] readings, for the sparkline
$script:histSig = ""       # cheap "has the history changed" key
$script:pace = -1.0        # 0 coasting .. 0.5 lands on the limit .. 1 runs out at once (-1 unknown)
$script:projected = -1     # where the session lands at reset, at the current burn
$script:hitMins = -1       # minutes until the limit at the current burn
$script:lastUsage = 0; $script:lastStack = 0; $script:lastPresence = 0
$script:lastDaemon = 0; $script:lastBeat = 0; $script:curInterval = 200
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W; $form.Height = $FORM_H
$form.Left   = $screen.Right - $UW - 16 - $GLOW - $TIP_W     # right edge lines up with the tabs

$ns = Join-Path $env:USERPROFILE ".claude\hal_voice\badges_stack"
$usageFile  = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "usage.json"
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
    $now = NowMs
    $count = 0; $sum = 0
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
            if (($now - $e.beat) -lt 2500) { $count++; $sum += $e.h }
        }
    } catch {}
    return @($count, $sum)
}

# The meter used to carry its own copy of the daemon watchdog, and was the only thing that had one.
# It now lives in popup_common.ps1 (Ensure-HudDaemon), where the badges and the tint can reach it
# too - so the daemon is watched by everything on screen rather than by one process that the daemon
# was in turn the only watcher of.

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

function Draw-Spark($g, $hist, $x, $y) {
    $n = @($hist).Count
    if ($n -lt 2) { return }                 # DrawLines throws on a single point
    try {
        $ts = New-Object 'double[]' $n; $us = New-Object 'double[]' $n
        for ($i = 0; $i -lt $n; $i++) { $ts[$i] = [double]$hist[$i][0]; $us[$i] = [double]$hist[$i][1] }
        $f = SparkNorm $us $SPARK_MIN_SPAN
        $t0 = $ts[0]; $dt = $ts[$n - 1] - $t0
        $pts = New-Object 'System.Drawing.PointF[]' $n
        for ($i = 0; $i -lt $n; $i++) {
            # x by timestamp, not index. The poll cadence is adaptive, so a four-minute gap and a
            # forty-five-second one are not the same amount of time and must not draw the same width.
            $fx = if ($dt -le 0) { $i / [double]($n - 1) } else { ($ts[$i] - $t0) / $dt }
            $pts[$i] = [System.Drawing.PointF]::new(
                [single]($x + $fx * ($SPARK_W - 1)),
                [single]($y + ($SPARK_H - 1) - $f[$i] * ($SPARK_H - 1)))   # inverted: more is higher
        }
        $col = if ($script:pace -ge 0) { PaceColor $script:pace $script:stale }
               else { BarColor $script:sessionPct $script:stale }
        $pen = New-Object System.Drawing.Pen $col, 1.4
        $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
        $g.DrawLines($pen, $pts); $pen.Dispose()
        $dot = New-Object System.Drawing.SolidBrush $col      # mark "now", legible at 60x14
        $g.FillEllipse($dot, [single]($pts[$n - 1].X - 2.0), [single]($pts[$n - 1].Y - 2.0), 4.0, 4.0)
        $dot.Dispose()
    } catch { }        # a torn read must never take the whole paint loop down with it
}
# Compact form for the headline: "43min", "2h14m", "now". The long form goes in the hover hint.
function ResetShort($iso) {
    if (-not $iso) { return "" }
    try {
        $mins = [int]((([datetime]$iso).ToUniversalTime() - [datetime]::UtcNow).TotalMinutes)
        if ($mins -le 0) { return "now" }
        if ($mins -lt 60) { return "${mins}min" }
        $h = [int][Math]::Floor($mins / 60)              # see MinsLong: [int] would round up
        if ($h -lt 24) { return "{0}h{1:00}m" -f $h, ($mins % 60) }
        return "{0}d{1}h" -f [int][Math]::Floor($h / 24), ($h % 24)
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
        $barR = $GLOW + $OX + $UW
        $barL = $barR - $UW
        $sBarY = $GLOW + $UPCT_H + 2
        $wBarY = $sBarY + $SBAR_H + $GAP_BARS
        $sp = [Math]::Min(100, $script:sessionPct)
        $wp = if ($script:weeklyPct -ge 0) { [Math]::Min(100, $script:weeklyPct) } else { -1 }

        $trk = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(215, 38, 38, 42))
        $g.FillRectangle($trk, $barL, $sBarY, $UW, $SBAR_H)
        if ($wp -ge 0) { $g.FillRectangle($trk, $barL, $wBarY, $UW, $WBAR_H) }
        $trk.Dispose()

        $sw = [int]($UW * $sp / 100.0)
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
        # "7% / 2h14m" - how much of the window is gone AND how long until it rolls over. The
        # percentage on its own tells you where you stand but not how long you have to spend it.
        $head = if ($script:sessionLeft) { "$sp% / $($script:sessionLeft)" } else { "$sp%" }
        Draw-OutlinedText $g $head $uFont $ptc $barR ($GLOW + 1) 3 'right'
    }

    if ($script:hot -and $script:sessionPct -ge 0) {
        # Built as clauses in order of importance, then trimmed from the least important end until
        # what is left fits the room beside the bar. Measuring beats sizing it by eye: the same
        # string is a different width in a different font or at a different DPI, and the old fixed
        # box quietly cut the end off rather than telling anyone. The weekly window goes first - it
        # is the least binding of the three - and the session line always survives.
        $lead = "session $($script:sessionPct)%"
        $sIn = ResetIn $script:sessionResets
        if ($sIn) { $lead += " - resets in $sIn" }
        if ($script:stale) { $lead = "(last known) " + $lead }
        $parts = @($lead)
        # The bar's colour is a glance; this is the sentence behind it.
        if ($script:pace -ge 0) {
            if ($script:hitMins -ge 0 -and $script:pace -gt 0.5) {
                $parts += "limit in ~$(MinsLong $script:hitMins)"
            } elseif ($script:projected -ge 0) {
                $parts += "on pace for $($script:projected)%"
            }
        }
        if ($script:weeklyPct -ge 0) {
            $wk = "weekly $($script:weeklyPct)%"
            $wIn = ResetIn $script:weeklyResets
            if ($wIn) { $wk += " - $wIn" }
            $parts += $wk
        }
        $room = $GLOW + $OX - 12                       # all the space left of the bar, less a margin
        # The chart shows up exactly when the pace clause does - same sample threshold - so the
        # picture and the sentence worked out from it arrive and leave together.
        $chart = $null
        if ($script:pace -ge 0 -and @($script:hist).Count -ge $SPARK_MIN) { $chart = @($script:hist) }

        if ($null -eq $chart) {
            $tip = $parts -join "     "
            $tw = [int][Math]::Ceiling($g.MeasureString($tip, $tipFont).Width)
            while ($parts.Count -gt 1 -and ($tw + 18) -gt $room) {
                $parts = @($parts[0..($parts.Count - 2)])
                $tip = $parts -join "     "
                $tw = [int][Math]::Ceiling($g.MeasureString($tip, $tipFont).Width)
            }
            $tbw = $tw + 18
        } else {
            # [9][ lead ][gap][ chart ][gap][ rest ][9] - the chart sits between "session 39%..." and
            # "on pace for 46%", next to the number it explains. Measured segment by segment rather
            # than as one string: GDI+ trims trailing whitespace, so the joined width of a run that
            # ends in a separator is not the width of its parts.
            $lead0 = $parts[0]
            $wL = [int][Math]::Ceiling($g.MeasureString($lead0, $tipFont).Width)
            while ($true) {
                $rest = if ($parts.Count -gt 1) { $parts[1..($parts.Count - 1)] -join "     " } else { "" }
                $wR = if ($rest) { [int][Math]::Ceiling($g.MeasureString($rest, $tipFont).Width) } else { 0 }
                $chartW = $SPARK_GAP + $SPARK_W + $(if ($rest) { $SPARK_GAP } else { 0 })
                $tbw = 18 + $wL + $chartW + $wR
                if ($parts.Count -le 1 -or $tbw -le $room) { break }
                $parts = @($parts[0..($parts.Count - 2)])
            }
        }
        $tbh = [Math]::Max(([int]$tipFont.Height + 8), ($SPARK_H + 10))
        $tbx = $GLOW + $OX - 10 - $tbw
        if ($tbx -lt 2) { $tbx = 2 }
        $tby = $GLOW + [int][Math]::Floor(($CONTENT_H - $tbh)/2)
        $tp = RoundedPath $tbx $tby $tbw $tbh 5
        $tbg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(246, 26, 26, 28))
        $g.FillPath($tbg, $tp); $tbg.Dispose()
        $tpen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(120, 200, 200, 210)), 1
        $g.DrawPath($tpen, $tp); $tpen.Dispose(); $tp.Dispose()
        $ttb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(237,237,241))
        $ty = [float]($tby + [Math]::Floor(($tbh - [int]$tipFont.Height) / 2))
        if ($null -eq $chart) {
            $g.DrawString($tip, $tipFont, $ttb, [float]($tbx + 9), $ty)
        } else {
            $g.DrawString($lead0, $tipFont, $ttb, [float]($tbx + 9), $ty)
            $cx = $tbx + 9 + $wL + $SPARK_GAP
            Draw-Spark $g $chart $cx ($tby + [int][Math]::Floor(($tbh - $SPARK_H) / 2))
            if ($rest) {
                $g.DrawString($rest, $tipFont, $ttb, [float]($cx + $SPARK_W + $SPARK_GAP), $ty)
            }
        }
        $ttb.Dispose()
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 255)
    Assert-Topmost $form
    $bmp.Dispose()
}

$form.Add_Shown({ [PerPixelLayered]::InitClickThrough($form.Handle); Assert-Topmost $form; & $render })

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
    }

    if ($nowMs - $script:lastUsage -ge 2000) {
        $script:lastUsage = $nowMs
        $j = Read-JsonFile $usageFile
        $s = -1; $w = -1; $sr = ""; $wr = ""; $st = $false
        $pc = -1.0; $pj = -1; $hm = -1; $hs = @()
        if ($j) {
            if ($null -ne $j.session_pct) { $s = [int]$j.session_pct; $sr = [string]$j.session_resets }
            if ($null -ne $j.weekly_pct)  { $w = [int]$j.weekly_pct;  $wr = [string]$j.weekly_resets }
            try { $st = ($nowMs - [int64]$j.ts) -gt 900000 } catch { $st = $true }
            # null until there are enough readings to fit a rate to - the meter falls back rather
            # than inventing a trend from two samples.
            try { if ($null -ne $j.pace)      { $pc = [double]$j.pace } }     catch { $pc = -1.0 }
            try { if ($null -ne $j.projected) { $pj = [int]$j.projected } }   catch { $pj = -1 }
            try { if ($null -ne $j.hit_mins)  { $hm = [int]$j.hit_mins } }    catch { $hm = -1 }
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
        $left = if ($st) { "" } else { ResetShort $sr }     # a stale reading has no honest countdown
        if ($s -ne $script:sessionPct -or $w -ne $script:weeklyPct -or $st -ne $script:stale -or
            $sr -ne $script:sessionResets -or $wr -ne $script:weeklyResets -or
            $left -ne $script:sessionLeft -or $pc -ne $script:pace -or
            $pj -ne $script:projected -or $hm -ne $script:hitMins -or
            ($script:hot -and $hsig -ne $script:histSig)) {   # a chart nobody is looking at can wait
            $script:histSig = $hsig
            $script:sessionPct = $s; $script:weeklyPct = $w; $script:stale = $st
            $script:sessionResets = $sr; $script:weeklyResets = $wr
            $script:sessionLeft = $left
            $script:pace = $pc; $script:projected = $pj; $script:hitMins = $hm
            & $render
        }
    }

    if ($nowMs - $script:lastPresence -ge 1000) {
        $script:lastPresence = $nowMs
        if (-not (Hud-Enabled)) { $form.Close(); return }
        if ([PerPixelLayered]::FindWindowEndsWith("Visual Studio Code") -ne [IntPtr]::Zero) { $script:lastVs = NowMs }
        elseif (($nowMs - $script:lastVs) -gt 30000) { $form.Close(); return }
    }
    if ($nowMs - $script:lastDaemon -ge 3000) { $script:lastDaemon = $nowMs; Ensure-HudDaemon; Assert-Topmost $form }

    $cp = [System.Windows.Forms.Cursor]::Position
    $mx = $form.Left + $GLOW + $OX; $my = $script:lastTop + $GLOW
    $over = ($cp.X -ge $mx -and $cp.X -lt ($mx + $UW) -and $cp.Y -ge $my -and $cp.Y -lt ($my + $CONTENT_H))
    if ($over -ne $script:hot) { $script:hot = $over; & $render }

    $delta = $script:targetTop - $script:curTop
    if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:targetTop } else { $script:curTop += $delta * 0.22 }
    $newTop = [int]$script:curTop
    if ($newTop -ne $script:lastTop) {
        $script:lastTop = $newTop
        $form.Top = $newTop
        [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
    }

    $want = if (([Math]::Abs($script:targetTop - $script:curTop) -ge 0.5) -or $script:hot) { 30 } else { 200 }
    if ($want -ne $script:curInterval) { $script:curInterval = $want; $timer.Interval = $want }
    if ($nowMs - $script:lastBeat -ge 600) { $script:lastBeat = $nowMs; Write-Beat $AliveFile }
})
$timer.Start()

[System.Windows.Forms.Application]::Run($form)
