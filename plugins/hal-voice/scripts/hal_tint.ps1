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

$created = $false
$script:mutex = New-Object System.Threading.Mutex($true, "hal_window_tint", [ref]$created)
if (-not $created) { exit }

$badgeDir = Join-Path $env:USERPROFILE ".claude\hal_voice\badges"
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

function ColorForHwnd([int64]$hwnd) {
    if ($hwnd -eq 0) { return $null }
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($badgeDir, "*.json")) {
            try { $d = [System.IO.File]::ReadAllText($f) | ConvertFrom-Json } catch { continue }
            if ($d -and $d.hwnd -and ([int64]$d.hwnd -eq $hwnd) -and $d.color -and $d.color.Count -ge 3) {
                return @([int]$d.color[0], [int]$d.color[1], [int]$d.color[2])
            }
        }
    } catch {}
    return $null
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.SetBounds(-32000, -32000, 4, $Thickness)
$form.Add_Shown({ [PerPixelLayered]::InitClickThrough($form.Handle) })

$script:cur = ""
$script:lastSeen = NowMs

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
    if ($AliveFile) { try { [System.IO.File]::WriteAllText($AliveFile, (NowMs).ToString()) } catch {} }
    $fg  = [PerPixelLayered]::GetForegroundWindow()
    $col = ColorForHwnd ([int64]$fg)
    if ($col -and -not [PerPixelLayered]::Minimized($fg)) {
        $r = [PerPixelLayered]::Rect($fg)
        if ($r) {
            $bx = $r[0]; $by = $r[1]; $bw = $r[2]
            $scr = [System.Windows.Forms.Screen]::FromHandle([IntPtr]$fg).Bounds
            if ($bx -lt $scr.Left) { $bw -= ($scr.Left - $bx); $bx = $scr.Left }
            if ($by -lt $scr.Top)  { $by = $scr.Top }
            if (($bx + $bw) -gt $scr.Right) { $bw = $scr.Right - $bx }
            Draw-Bar $bx $by $bw $col; $script:lastSeen = NowMs; return
        }
    }
    Hide-Bar
    if ((NowMs) - $script:lastSeen -gt 60000) {
        try { if (([System.IO.Directory]::GetFiles($badgeDir, "*.json")).Count -eq 0) { $form.Close() } } catch {}
    }
})
$timer.Start()

[System.Windows.Forms.Application]::Run($form)
