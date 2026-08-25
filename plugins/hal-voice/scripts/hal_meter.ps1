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
$TIP_W = 300                                       # room to the LEFT for the hover hint
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

$script:hot = $false; $script:closeReq = $false
$script:sessionPct = -1; $script:weeklyPct = -1
$script:sessionResets = ""; $script:weeklyResets = ""
$script:sessionLeft = ""   # "2h14m" until the session window rolls over
$script:stale = $false
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
$badgeDir   = Join-Path $env:USERPROFILE ".claude\hal_voice\badges"
$usageFile  = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "usage.json"
$sessionsPy = Join-Path $PSScriptRoot 'hal_sessions.py'
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

# Keep the session reconciler alive. This is the HUD's other always-on process, so it makes a good
# watchdog: if the daemon's heartbeat has gone stale, start it again from the interpreter it
# recorded for us (the daemon holds a named mutex, so a double-start resolves to one).
function Ensure-SessionDaemon {
    $ap  = Join-Path $badgeDir "sessions_daemon.alive"
    $now = NowMs
    try {
        $beat = [int64](((Read-TextShared $ap).Trim() -split '\s+')[0])
        if (($now - $beat) -lt 9000) { return }
    } catch {}
    $exe = ""
    try { $exe = (Read-TextShared (Join-Path $badgeDir "sessions_daemon.exe")).Trim() } catch {}
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return }
    try { [PerPixelLayered]::AtomicWrite($ap, "$now 0") } catch {}
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = '"{0}" --daemon' -f $sessionsPy
        $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        [System.Diagnostics.Process]::Start($psi) | Out-Null
    } catch {}
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
# Compact form for the headline: "43min", "2h14m", "now". The long form goes in the hover hint.
function ResetShort($iso) {
    if (-not $iso) { return "" }
    try {
        $mins = [int]((([datetime]$iso).ToUniversalTime() - [datetime]::UtcNow).TotalMinutes)
        if ($mins -le 0) { return "now" }
        if ($mins -lt 60) { return "${mins}min" }
        return "{0}h{1:00}m" -f [int]($mins/60), ($mins % 60)
    } catch { return "" }
}
function ResetIn($iso) {
    if (-not $iso) { return "" }
    try {
        $mins = [int]((([datetime]$iso).ToUniversalTime() - [datetime]::UtcNow).TotalMinutes)
        if ($mins -le 0) { return "any moment" }
        if ($mins -lt 60) { return "$mins min" }
        return "{0}h {1}m" -f [int]($mins/60), ($mins % 60)
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
            $sc = BarColor $sp $script:stale
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
        $sIn = ResetIn $script:sessionResets
        $wIn = ResetIn $script:weeklyResets
        $tip = "session $($script:sessionPct)%"
        if ($sIn) { $tip += " - resets in $sIn" }
        if ($script:weeklyPct -ge 0) {
            $tip += "     weekly $($script:weeklyPct)%"
            if ($wIn) { $tip += " - $wIn" }
        }
        if ($script:stale) { $tip = "(last known) " + $tip }
        $tw = [int][Math]::Ceiling($g.MeasureString($tip, $tipFont).Width)
        $tbw = $tw + 18; $tbh = [int]$tipFont.Height + 8
        $tbx = $GLOW + $OX - 10 - $tbw
        if ($tbx -lt 2) { $tbx = 2 }
        $tby = $GLOW + [int](($CONTENT_H - $tbh)/2)
        $tp = RoundedPath $tbx $tby $tbw $tbh 5
        $tbg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(246, 26, 26, 28))
        $g.FillPath($tbg, $tp); $tbg.Dispose()
        $tpen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(120, 200, 200, 210)), 1
        $g.DrawPath($tpen, $tp); $tpen.Dispose(); $tp.Dispose()
        $ttb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(237,237,241))
        $g.DrawString($tip, $tipFont, $ttb, [float]($tbx + 9), [float]($tby + 4)); $ttb.Dispose()
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
        if ($j) {
            if ($null -ne $j.session_pct) { $s = [int]$j.session_pct; $sr = [string]$j.session_resets }
            if ($null -ne $j.weekly_pct)  { $w = [int]$j.weekly_pct;  $wr = [string]$j.weekly_resets }
            try { $st = ($nowMs - [int64]$j.ts) -gt 900000 } catch { $st = $true }
        }
        $left = if ($st) { "" } else { ResetShort $sr }     # a stale reading has no honest countdown
        if ($s -ne $script:sessionPct -or $w -ne $script:weeklyPct -or $st -ne $script:stale -or
            $sr -ne $script:sessionResets -or $wr -ne $script:weeklyResets -or
            $left -ne $script:sessionLeft) {
            $script:sessionPct = $s; $script:weeklyPct = $w; $script:stale = $st
            $script:sessionResets = $sr; $script:weeklyResets = $wr
            $script:sessionLeft = $left
            & $render
        }
    }

    if ($nowMs - $script:lastPresence -ge 1000) {
        $script:lastPresence = $nowMs
        if (-not (Hud-Enabled)) { $form.Close(); return }
        if ([PerPixelLayered]::FindWindowEndsWith("Visual Studio Code") -ne [IntPtr]::Zero) { $script:lastVs = NowMs }
        elseif (($nowMs - $script:lastVs) -gt 30000) { $form.Close(); return }
    }
    if ($nowMs - $script:lastDaemon -ge 3000) { $script:lastDaemon = $nowMs; Ensure-SessionDaemon; Assert-Topmost $form }

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
