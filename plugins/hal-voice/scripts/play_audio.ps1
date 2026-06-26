param(
    [Parameter(Mandatory = $true)][string]$Path,
    [int]$FallbackMs = 6000   # used only if the real duration can't be determined
)

# Play an mp3/wav to completion with NO external dependency (no ffplay/ffprobe).
# Primary: WPF MediaPlayer (ships with .NET on Windows). We poll NaturalDuration
# instead of using events so it works without a WPF Dispatcher / message pump.
# Fallback: Windows Media Player COM object.

$ErrorActionPreference = "Stop"
try { $abs = (Resolve-Path -LiteralPath $Path).Path } catch { exit 1 }

function Play-WithMediaPlayer($file) {
    Add-Type -AssemblyName PresentationCore
    $player = New-Object System.Windows.Media.MediaPlayer
    try {
        $player.Volume = 1.0
        $player.Open([System.Uri]$file)
        $player.Play()

        # Wait (≤5s) for the media to load so NaturalDuration is populated.
        $waited = 0
        while (-not $player.NaturalDuration.HasTimeSpan -and $waited -lt 5000) {
            Start-Sleep -Milliseconds 50
            $waited += 50
        }
        if ($player.NaturalDuration.HasTimeSpan) {
            $durMs = [int]$player.NaturalDuration.TimeSpan.TotalMilliseconds
        } else {
            $durMs = $FallbackMs
        }
        # +250ms tail so the very end isn't clipped.
        Start-Sleep -Milliseconds ([Math]::Max(500, $durMs - $waited + 250))
        return $true
    } finally {
        try { $player.Stop() } catch {}
        try { $player.Close() } catch {}
    }
}

function Play-WithWmp($file) {
    $wmp = New-Object -ComObject WMPlayer.OCX
    try {
        $media = $wmp.newMedia($file)
        $wmp.currentPlaylist.appendItem($media)
        $wmp.controls.play()
        # Wait for the duration to resolve, then for playback to finish.
        $waited = 0
        while ($media.duration -le 0 -and $waited -lt 5000) { Start-Sleep -Milliseconds 50; $waited += 50 }
        $durMs = if ($media.duration -gt 0) { [int]($media.duration * 1000) } else { $FallbackMs }
        Start-Sleep -Milliseconds ($durMs + 250)
        return $true
    } finally {
        try { $wmp.close() } catch {}
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($wmp)
    }
}

try {
    if (Play-WithMediaPlayer $abs) { exit 0 }
} catch {
    try { if (Play-WithWmp $abs) { exit 0 } } catch { exit 1 }
}
exit 0
