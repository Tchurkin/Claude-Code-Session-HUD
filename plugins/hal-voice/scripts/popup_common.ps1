# Shared Win32 / layered-window helpers for the HUD windows (badge.ps1, hal_tint.ps1,
# claude_button.ps1).
#
# 1. PerPixelLayered - the per-pixel-alpha layered-window helper (true transparency + glow),
#    plus window helpers (move, focus, find/rect a window, click-through, no-activate).
# 2. A tiny cross-process "stack" registry so windows from multiple chats don't overlap:
#    each heartbeats {id, ts, h} into per-window files; each reads them every frame and
#    slots itself by birth time - newest at the anchor, older pushed away. Best-effort
#    (each re-asserts itself every beat, so a lost write self-heals on the next frame).

# ── per-pixel-alpha layered window ─────────────────────────────────────────────
$src = @"
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public class PerPixelLayered {
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; public POINT(int x,int y){X=x;Y=y;} }
    [StructLayout(LayoutKind.Sequential)] public struct SIZE  { public int cx, cy; public SIZE(int x,int y){cx=x;cy=y;} }
    [StructLayout(LayoutKind.Sequential, Pack=1)] public struct BLENDFUNCTION { public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat; }
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
    [DllImport("user32.dll", SetLastError=true)] static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll", SetLastError=true)] static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32.dll", SetLastError=true)] static extern bool UpdateLayeredWindow(IntPtr h, IntPtr dst, ref POINT pdst, ref SIZE ps, IntPtr src, ref POINT psrc, int key, ref BLENDFUNCTION bf, int flags);
    [DllImport("user32.dll", SetLastError=true)] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int n);
    delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr h, IntPtr dc);
    [DllImport("gdi32.dll")]  static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")]  static extern IntPtr SelectObject(IntPtr dc, IntPtr o);
    [DllImport("gdi32.dll")]  static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")]  static extern bool DeleteObject(IntPtr o);
    const int GWL_EXSTYLE=-20, WS_EX_LAYERED=0x80000, ULW_ALPHA=2;
    public static void Init(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|WS_EX_LAYERED); }
    // Move the window with no resize/redraw - the layered surface slides with it (cheap).
    public static void Move(IntPtr h, int x, int y){ SetWindowPos(h, IntPtr.Zero, x, y, 0, 0, 0x1|0x4|0x10); }
    // Bring another window (a chat's VS Code window) to the front; restore if minimized.
    public static void FocusWindow(IntPtr h){ if (h == IntPtr.Zero) return; if (IsIconic(h)) ShowWindow(h, 9); SetForegroundWindow(h); }
    // Make THIS window a click-through overlay (layered + transparent + no-activate + no taskbar).
    public static void InitClickThrough(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|WS_EX_LAYERED|0x20|0x08000000|0x80); }
    // Add click-through WITHOUT forcing WS_EX_LAYERED (for Form.Opacity overlays that manage layering themselves).
    public static void AddClickThrough(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|0x20|0x08000000|0x80); }
    // Don't steal foreground when shown/clicked (WS_EX_NOACTIVATE) - so notification popups
    // don't yank focus off the chat window (which would drop the window-tint bar).
    public static void NoActivate(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|0x08000000); }
    public static bool Minimized(IntPtr h){ return IsIconic(h); }
    public static bool WindowExists(IntPtr h){ return h != IntPtr.Zero && IsWindow(h); }
    // Title text of a window (for matching a chat's window by project name when its handle drifts).
    public static string WindowTitle(IntPtr h){
        int len = GetWindowTextLength(h);
        if(len <= 0) return "";
        var sb = new System.Text.StringBuilder(len+1);
        GetWindowText(h, sb, len+1);
        return sb.ToString();
    }
    // Screen rect of a window as [x, y, w, h] (or null if it can't be read).
    public static int[] Rect(IntPtr h){ RECT r; if(!GetWindowRect(h, out r)) return null; return new int[]{ r.L, r.T, r.R-r.L, r.B-r.T }; }
    // Topmost visible window whose title ends with `suffix` (e.g. "Visual Studio Code").
    public static IntPtr FindWindowEndsWith(string suffix){
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr h, IntPtr l){
            if(!IsWindowVisible(h)) return true;
            int len = GetWindowTextLength(h);
            if(len <= 0) return true;
            var sb = new System.Text.StringBuilder(len+1);
            GetWindowText(h, sb, len+1);
            if(sb.ToString().EndsWith(suffix, StringComparison.OrdinalIgnoreCase)){ found = h; return false; }
            return true;
        }, IntPtr.Zero);
        return found;
    }
    public static void SetBitmap(IntPtr h, Bitmap bmp, int left, int top, byte opacity){
        IntPtr screen=GetDC(IntPtr.Zero), mem=CreateCompatibleDC(screen), hbmp=IntPtr.Zero, old=IntPtr.Zero;
        try {
            hbmp=bmp.GetHbitmap(Color.FromArgb(0)); old=SelectObject(mem,hbmp);
            SIZE s=new SIZE(bmp.Width,bmp.Height); POINT psrc=new POINT(0,0); POINT pdst=new POINT(left,top);
            BLENDFUNCTION bf=new BLENDFUNCTION(); bf.BlendOp=0; bf.BlendFlags=0; bf.SourceConstantAlpha=opacity; bf.AlphaFormat=1;
            UpdateLayeredWindow(h,screen,ref pdst,ref s,mem,ref psrc,0,ref bf,ULW_ALPHA);
        } finally {
            ReleaseDC(IntPtr.Zero,screen);
            if(hbmp!=IntPtr.Zero){ SelectObject(mem,old); DeleteObject(hbmp); }
            DeleteDC(mem);
        }
    }
}
"@
try { Add-Type -TypeDefinition $src -ReferencedAssemblies System.Drawing, System.Windows.Forms } catch {}

# ── cross-process stacking registry ────────────────────────────────────────────
# Each popup owns ONE tiny file (popups\<id>.json) that it alone writes - so there is no
# read-modify-write contention between processes (the old single shared file clobbered
# itself). Readers just glob the folder. Files whose heartbeat went stale = crashed popups.
$script:PopupId  = [Guid]::NewGuid().ToString()
$script:PopupDir = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "popups"
$script:SlotFile = Join-Path $script:PopupDir "$($script:PopupId).json"
try { [System.IO.Directory]::CreateDirectory($script:PopupDir) | Out-Null } catch {}
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }
$script:BornMs = NowMs   # stable birth time = our sort key (newest on top)

# Heartbeat our own slot file, then return all live popups (newest first). When not alive,
# delete our slot so the others close the gap.
function Stack-Sync($height, $alive) {
    $now = NowMs
    if (-not $alive) {
        try { [System.IO.File]::Delete($script:SlotFile) } catch {}
        return @()
    }
    try {
        $me = [pscustomobject]@{ id = $script:PopupId; ts = $script:BornMs; h = [int]$height; beat = $now }
        [System.IO.File]::WriteAllText($script:SlotFile, ($me | ConvertTo-Json -Compress))
    } catch {}

    $live = New-Object System.Collections.ArrayList
    try {
        foreach ($f in [System.IO.Directory]::GetFiles($script:PopupDir, "*.json")) {
            try {
                $o = ([System.IO.File]::ReadAllText($f) | ConvertFrom-Json)
                if ($o -and $o.id -and (($now - [int64]$o.beat) -lt 1500)) { [void]$live.Add($o) }
                elseif (($now - [int64]$o.beat) -ge 5000) { [System.IO.File]::Delete($f) }  # GC a crashed popup
            } catch {}
        }
    } catch {}
    return @($live | Sort-Object -Property @{ Expression = { [int64]$_.ts } } -Descending)
}

# Target top for THIS popup: base anchor + heights of every newer popup above it.
function Stack-TargetTop($baseTop, $gap, $ordered) {
    $offset = 0
    foreach ($e in $ordered) {
        if ($e.id -eq $script:PopupId) { break }
        $offset += [int]$e.h + $gap
    }
    return [int]($baseTop + $offset)
}

# Switch this process's stacking namespace (its own folder of slot files), so badges
# stack among themselves without interfering with the transient popups.
function Set-StackNamespace($name) {
    $script:PopupDir = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") $name
    $script:SlotFile = Join-Path $script:PopupDir "$($script:PopupId).json"
    try { [System.IO.Directory]::CreateDirectory($script:PopupDir) | Out-Null } catch {}
}

# Bottom-anchored variant: newest sits AT the bottom anchor, older stack upward above it.
function Stack-TargetBottom($bottomAnchor, $gap, $ordered, $selfHeight) {
    $below = 0
    foreach ($e in $ordered) {
        if ($e.id -eq $script:PopupId) { break }
        $below += [int]$e.h + $gap
    }
    return [int]($bottomAnchor - $below - [int]$selfHeight)
}
