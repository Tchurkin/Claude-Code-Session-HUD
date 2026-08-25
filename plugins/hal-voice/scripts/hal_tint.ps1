param(
    [string]$AliveFile = "",
    [int]   $Thickness = 8
)

# Window color coding: a colored bar riding the top edge of the focused chat window, in
# that chat's color, fading from bright at the very top to nothing lower down. Single
# layered window (so the timer/message-loop is reliable); the fade is drawn per-row with
# PREMULTIPLIED alpha (what UpdateLayeredWindow needs). Click-through - never intercepts.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'popup_common.ps1')
if (-not $script:PplReady) { exit 1 }   # no drawing type -> exit so the supervisor respawns a working one

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_window_tint", [ref]$created)
if (-not $created) { exit }

$badgeDir = Join-Path $env:USERPROFILE ".claude\hal_voice\badges"
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }
$script:stateCache = @{}

# The accent for a window is the colour of the chat that window is CURRENTLY SHOWING. Several chats
# share a window, so "first state file that names this handle" gave every one of them the same
# banner - whichever chat happened to sort first. `showing` is worked out by the reconciler from
# what the window has in front (see hal_sessions._bind_windows); a chat sitting in a background tab
# is only used as a fallback, for when the window is on something that isn't a chat at all.
function ColorForHwnd([int64]$hwnd) {
    if ($hwnd -eq 0) { return $null }
    $fallback = $null
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($badgeDir, "*.json")) {
            # Cached per file on its timestamp: this runs ~8x a second and the states rarely change.
            $d = $null
            try {
                $mt = [System.IO.File]::GetLastWriteTimeUtc($f)
                $c  = $script:stateCache[$f]
                if ($c -and $c.mt -eq $mt) { $d = $c.d }
                else { $d = Read-TextShared $f | ConvertFrom-Json
                       $script:stateCache[$f] = @{ mt = $mt; d = $d } }
            } catch { continue }
            if ($d -and $d.hwnd -and ([int64]$d.hwnd -eq $hwnd) -and $d.color -and $d.color.Count -ge 3) {
                $col = @([int]$d.color[0], [int]$d.color[1], [int]$d.color[2])
                if ($null -eq $d.showing -or [bool]$d.showing) { return $col }
                if (-not $fallback) { $fallback = $col }
            }
        }
    } catch {}
    return $fallback
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.SetBounds(-32000, -32000, 4, $Thickness)
$form.Add_Shown({ [PerPixelLayered]::InitClickThrough($form.Handle); Assert-Topmost $form })

$script:cur = ""
$script:lastSeen = NowMs
$script:tintTick = 0

function Draw-Bar($x, $y, $w, $col) {
    if ($w -lt 8) { Hide-Bar; return }
    $sig = "$x,$y,$w,$($col -join '-')"
    if ($sig -eq $script:cur) { return }
    $script:cur = $sig
    $bmp = New-Object System.Drawing.Bitmap($w, $Thickness, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    for ($ry = 0; $ry -lt $Thickness; $ry++) {
        $a = [int](250 * (1.0 - ($ry / [double]$Thickness)))
        if ($a -le 2) { continue }
        $pr = [int]($col[0] * $a / 255); $pg = [int]($col[1] * $a / 255); $pb = [int]($col[2] * $a / 255)
        $rb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb($a, $pr, $pg, $pb))
        $g.FillRectangle($rb, 0, $ry, $w, 1); $rb.Dispose()
    }
    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $x, $y, 255)
    Assert-Topmost $form
    $bmp.Dispose()
}

function Hide-Bar {
    if ($script:cur -eq "HIDDEN") { return }
    $script:cur = "HIDDEN"
    $bmp = New-Object System.Drawing.Bitmap(1, 1, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, -32000, -32000, 255)
    $bmp.Dispose()
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 120
$timer.Add_Tick({
    if (($script:tintTick++ % 8) -eq 0 -and -not (Hud-Enabled)) { $form.Close(); return }   # HUD off -> retire
    if (($script:tintTick % 8) -eq 3) { Assert-Topmost $form }   # another app can steal the band
    $fg  = [PerPixelLayered]::GetForegroundWindow()
    $col = ColorForHwnd ([int64]$fg)
    $drew = $false
    if ($col -and -not [PerPixelLayered]::Minimized($fg)) {
        $r = [PerPixelLayered]::Rect($fg)
        if ($r) {
            $bx = $r[0]; $by = $r[1]; $bw = $r[2]
            $scr = [System.Windows.Forms.Screen]::FromHandle([IntPtr]$fg).Bounds
            if ($bx -lt $scr.Left) { $bw -= ($scr.Left - $bx); $bx = $scr.Left }
            if ($by -lt $scr.Top)  { $by = $scr.Top }
            if (($bx + $bw) -gt $scr.Right) { $bw = $scr.Right - $bx }
            Draw-Bar $bx $by $bw $col; $script:lastSeen = NowMs; $drew = $true
        }
    }
    if (-not $drew) {
        Hide-Bar
        if ((NowMs) - $script:lastSeen -gt 60000) {
            try { if (([System.IO.Directory]::GetFiles($badgeDir, "*.json")).Count -eq 0) { $form.Close() } } catch {}
        }
    }
    Write-Beat $AliveFile        # last: a frame that threw leaves the beat stale, so we get replaced
})
$timer.Start()

[System.Windows.Forms.Application]::Run($form)
