param(
    [string]$AliveFile = ""
)

# A small always-on-top "new chat" button (a Claude-style spark). Left-click focuses a
# VS Code window and sends Ctrl+Alt+N (bound to "Claude Code: Open in New Window"), so a
# new chat opens in its own window without you remembering the shortcut. Right-click hides.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_claude_button", [ref]$created)
if (-not $created) { exit }

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$CW = 22; $CH = 22; $GLOW = 10; $R = 6
$TIP_W = 150                                                 # room to the LEFT for the hover hint
$OX = $TIP_W                                                 # button x-origin inside the (wider) canvas
$ACCENT = [System.Drawing.Color]::FromArgb(217, 119, 87)     # Claude clay/orange
$FORM_W = $CW + $GLOW*2 + $TIP_W; $FORM_H = $CH + $GLOW*2
$tipFont = New-Object System.Drawing.Font("Segoe UI", 9)

$script:hot = $false; $script:closeReq = $false; $script:tick = 0
$script:pendLeaf = ''; $script:pendSrcH = 0; $script:pendUntil = 0; $script:pendSend = 0   # deferred "open Claude in the new window"
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W; $form.Height = $FORM_H
# The button rides just above the badge ("chat tab") stack; at the corner when there are none.
$ns = Join-Path $env:USERPROFILE ".claude\hal_voice\badges_stack"
$badgeDir = Join-Path $env:USERPROFILE ".claude\hal_voice\badges"   # per-chat state files (for the focus watcher)
$badgePs1 = Join-Path $PSScriptRoot 'badge.ps1'
$dockBottom = $screen.Bottom - 44               # above VS Code's status bar; button rides atop the tab stack
$GAPB = 8
$script:curTop    = $dockBottom - $GLOW - $CH
$script:targetTop = $script:curTop
$script:lastTop   = -99999
$form.Left = $screen.Right - $CW - 16 - $GLOW - $TIP_W    # keep the button at the corner; canvas extends left
$form.Top  = $script:curTop

function StackHeight {
    $now = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
    $count = 0; $sum = 0
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($ns, "*.json")) {
            try { $d = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json } catch { continue }
            if ($d -and $d.beat -and (($now - [int64]$d.beat) -lt 1500)) { $count++; $sum += [int]$d.h }
        }
    } catch {}
    return @($count, $sum)
}

# When you focus a VS Code window, make sure the chat in it has a tab. Tabs are normally created
# by Claude Code hooks, and just focusing a window fires no hook - so an idle chat (or one whose
# badge was killed) wouldn't get a tab. This bridges that: if the focused window's chat has saved
# state but no live badge, (re)spawn its badge. Matches by window handle, else by project title.
function Ensure-FocusedTab {
    $fg = [PerPixelLayered]::GetForegroundWindow()
    if ($fg -eq [IntPtr]::Zero) { return }
    $title = ""
    try { $title = [PerPixelLayered]::WindowTitle($fg) } catch {}
    if (-not ($title -and $title.EndsWith("Visual Studio Code"))) { return }
    $fgL = $fg.ToInt64(); $now = NowMs
    $anyAlive = $false; $bestFile = $null; $bestAp = $null; $bestTs = -1
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($badgeDir, "*.json")) {
            $d = $null
            try { $d = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json } catch { continue }
            if (-not $d) { continue }
            $mh = ($d.hwnd -and ([int64]$d.hwnd -eq $fgL))
            $mp = ($d.proj -and $title.Contains([string]$d.proj))
            if (-not ($mh -or $mp)) { continue }
            $sid8 = [System.IO.Path]::GetFileNameWithoutExtension($f)
            $ap = Join-Path $badgeDir ($sid8 + ".alive")
            $fresh = $false
            try { $fresh = ($now - [int64]([System.IO.File]::ReadAllText($ap).Trim())) -lt 4000 } catch {}
            if ($fresh) { $anyAlive = $true; continue }        # a tab for this window is already up
            $ts = 0; try { $ts = [int64]$d.ts } catch {}
            if ($ts -gt $bestTs) { $bestTs = $ts; $bestFile = $f; $bestAp = $ap }
        }
    } catch {}
    if (-not $anyAlive -and $bestFile) {
        try { [System.IO.File]::WriteAllText($bestAp, $now.ToString()) } catch {}   # pre-mark; badge mutex guards doubles
        try {
            $a = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -StateFile "{1}" -AliveFile "{2}" -IdleMs 1200000' -f $badgePs1, $bestFile, $bestAp
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = "powershell"; $psi.Arguments = $a
            $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
            $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
            [System.Diagnostics.Process]::Start($psi) | Out-Null
        } catch {}
    }
}

function RoundedPath($x, $y, $w, $h, $rad) {
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $rad*2
    $p.AddArc($x, $y, $d, $d, 180, 90)
    $p.AddArc(($x+$w-$d), $y, $d, $d, 270, 90)
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d, $d, 0, 90)
    $p.AddArc($x, ($y+$h-$d), $d, $d, 90, 90)
    $p.CloseFigure(); return $p
}
function CA($a, $c) { [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }

$render = {
    $acc = if ($script:hot) { [System.Drawing.Color]::FromArgb(240, 150, 120) } else { $ACCENT }
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    $g.Clear([System.Drawing.Color]::Transparent)

    $gbase = if ($script:hot) { 170 } else { 120 }
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int]($gbase * [Math]::Exp(-$sp * 0.34))
        if ($alpha -lt 4) { continue }
        $gp = RoundedPath ($GLOW+$OX-$sp) ($GLOW-$sp) ($CW+$sp*2) ($CH+$sp*2) ([Math]::Min($R+$sp,16))
        $pen = New-Object System.Drawing.Pen((CA $alpha $acc), 1.5)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }

    $cpath = RoundedPath ($GLOW+$OX) $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(232, 20, 18, 17))
    $g.FillPath($bg, $cpath); $bg.Dispose()
    $bpen = New-Object System.Drawing.Pen((CA 210 $acc), 1.3)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose(); $cpath.Dispose()

    # A simple plus (new chat).
    $cx = $GLOW + $OX + $CW/2; $cy = $GLOW + $CH/2
    $penS = New-Object System.Drawing.Pen($acc, 2.2)
    $penS.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $penS.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
    $arm = 5
    $g.DrawLine($penS, [float]($cx-$arm), [float]$cy, [float]($cx+$arm), [float]$cy)
    $g.DrawLine($penS, [float]$cx, [float]($cy-$arm), [float]$cx, [float]($cy+$arm))
    $penS.Dispose()

    # Hover hint to the LEFT of the button, so it's clear this opens a NEW chat window.
    if ($script:hot) {
        $tip = "New chat window"
        $tw  = [int][Math]::Ceiling($g.MeasureString($tip, $tipFont).Width)
        $tbw = $tw + 16; $tbh = 22
        $tbx = $GLOW + $OX - 10 - $tbw
        if ($tbx -lt 2) { $tbx = 2 }
        $tby = $GLOW + [int](($CH - $tbh)/2)
        $tpath = RoundedPath $tbx $tby $tbw $tbh 5
        $tbg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(238, 20, 18, 17))
        $g.FillPath($tbg, $tpath); $tbg.Dispose()
        $tpen = New-Object System.Drawing.Pen((CA 150 $acc), 1)
        $g.DrawPath($tpen, $tpath); $tpen.Dispose(); $tpath.Dispose()
        $ttb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(240,236,234))
        $g.DrawString($tip, $tipFont, $ttb, [float]($tbx + 9), [float]($tby + 4)); $ttb.Dispose()
    }

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 245)
    $bmp.Dispose()
}

# Which folder+source-window to open: the chat whose window is focused, else the most recent.
$getFolder = {
    $fgL = 0; try { $fgL = ([PerPixelLayered]::GetForegroundWindow()).ToInt64() } catch {}
    $best = $null; $bestTs = -1; $fgSel = $null
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($badgeDir, "*.json")) {
            try { $d = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json } catch { continue }
            if (-not $d.cwd) { continue }
            $sel = [pscustomobject]@{ cwd = [string]$d.cwd; hwnd = [int64]$d.hwnd }
            if ($d.hwnd -and ([int64]$d.hwnd -eq $fgL)) { $fgSel = $sel }
            $ts = 0; try { $ts = [int64]$d.ts } catch {}
            if ($ts -gt $bestTs) { $bestTs = $ts; $best = $sel }
        }
    } catch {}
    if ($fgSel) { return $fgSel } else { return $best }
}

# The real Code.exe - launching the code.cmd shim mangles folder paths that contain spaces.
$codeCmd = (Get-Command code -ErrorAction SilentlyContinue).Source
$codeExe = $null
if ($codeCmd) {
    $cand = Join-Path (Split-Path -Parent (Split-Path -Parent $codeCmd)) 'Code.exe'
    $codeExe = if (Test-Path -LiteralPath $cand) { $cand } else { $codeCmd }
}

# Open the SAME workspace folder in a new VS Code window; then, once that new window (a DIFFERENT
# VS Code window showing this folder) has focus, drop Claude into it as an editor tab via F13.
# The waiting + F13 send is handled in the timer, so it targets the real new window, not a guess.
$openNew = {
    $sel = & $getFolder
    if ($codeExe -and $sel -and $sel.cwd -and (Test-Path -LiteralPath $sel.cwd)) {
        try { Start-Process -FilePath $codeExe -ArgumentList @('--new-window', $sel.cwd) -WindowStyle Hidden } catch {}
        $script:pendLeaf = Split-Path -Leaf $sel.cwd
        $script:pendSrcH = [int64]$sel.hwnd
        $script:pendUntil = (NowMs) + 20000
    } else {
        # fallback: Claude's own "Open in New Window" (Ctrl+Alt+N binding)
        $h = [PerPixelLayered]::FindWindowEndsWith("Visual Studio Code")
        if ($h -ne [IntPtr]::Zero) { [PerPixelLayered]::FocusWindow($h); Start-Sleep -Milliseconds 150; [System.Windows.Forms.SendKeys]::SendWait("^%n") }
    }
}

$form.Add_MouseDown({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Right) { $script:closeReq = $true }
    else { & $openNew }
})
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

$script:lastVs = NowMs
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 30
$timer.Add_Tick({
    if ($script:closeReq) { $form.Close(); return }
    $script:tick++

    # Deferred "open Claude in the new window": wait until a DIFFERENT VS Code window showing this
    # folder is foreground, give the extension a moment to init, then send F13 (-> Open in New Tab).
    if ($script:pendUntil -gt 0) {
        if ((NowMs) -gt $script:pendUntil) { $script:pendUntil = 0 }
        else {
            $fh = [PerPixelLayered]::GetForegroundWindow()
            if ($fh -ne [IntPtr]::Zero -and $fh.ToInt64() -ne $script:pendSrcH) {
                $ttl = ""; try { $ttl = [PerPixelLayered]::WindowTitle($fh) } catch {}
                if ($ttl -and $ttl.EndsWith("Visual Studio Code") -and $ttl.Contains($script:pendLeaf)) {
                    $script:pendUntil = 0; $script:pendSend = (NowMs) + 1200
                }
            }
        }
    } elseif ($script:pendSend -gt 0 -and (NowMs) -ge $script:pendSend) {
        try { [System.Windows.Forms.SendKeys]::SendWait('{F13}') } catch {}
        $script:pendSend = 0
    }
    if (($script:tick % 4) -eq 1) {                  # ~every 120ms: recompute where the stack tops out
        $info = StackHeight; $cnt = $info[0]; $sum = $info[1]
        if ($cnt -eq 0) { $bBottom = $dockBottom }
        else { $bBottom = $dockBottom - ($sum + ($cnt - 1) * $GAPB) - $GAPB }
        $script:targetTop = [int]($bBottom - $GLOW - $CH)
    }
    if (($script:tick % 33) -eq 0) {                 # ~every 1s: heartbeat + VS Code presence
        if (-not (Hud-Enabled)) { $form.Close(); return }   # HUD switched off -> retire (toggle stays)
        if ($AliveFile) { try { [System.IO.File]::WriteAllText($AliveFile, (NowMs).ToString()) } catch {} }
        if ([PerPixelLayered]::FindWindowEndsWith("Visual Studio Code") -ne [IntPtr]::Zero) { $script:lastVs = NowMs }
        elseif ((NowMs) - $script:lastVs -gt 30000) { $form.Close(); return }   # VS Code gone -> retire
    }
    if (($script:tick % 17) -eq 5) { Ensure-FocusedTab }   # ~every 0.5s: focus a window -> surface its tab
    $delta = $script:targetTop - $script:curTop
    if ([Math]::Abs($delta) -lt 0.5) { $script:curTop = $script:targetTop } else { $script:curTop += $delta * 0.22 }
    $newTop = [int]$script:curTop
    if ($newTop -ne $script:lastTop) {
        $script:lastTop = $newTop
        $form.Top = $newTop
        [PerPixelLayered]::Move($form.Handle, $form.Left, $newTop)
    }

    # Hover (cursor-rect poll; reliable on layered windows): light up + show the hint.
    $bl = $form.Left + $GLOW + $OX
    $bt = $form.Top + $GLOW
    $cp = [System.Windows.Forms.Cursor]::Position
    $over = ($cp.X -ge $bl -and $cp.X -lt ($bl + $CW) -and $cp.Y -ge $bt -and $cp.Y -lt ($bt + $CH))
    if ($over -ne $script:hot) { $script:hot = $over; & $render }
})
$timer.Start()

$form.Add_FormClosed({ if ($AliveFile) { try { Remove-Item -LiteralPath $AliveFile -ErrorAction SilentlyContinue } catch {} } })

[System.Windows.Forms.Application]::Run($form)
