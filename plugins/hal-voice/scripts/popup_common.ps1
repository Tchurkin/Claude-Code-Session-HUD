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
$script:PplSource = @"
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
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
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
    // Bring another window (a chat's VS Code window) to the front; restore it if minimized, and
    // un-hide it if it has been hidden. A window can end up hidden-but-alive (VS Code hides one
    // mid-reload, say) - the editor then considers that folder open and quietly focuses something
    // you cannot see, so the folder appears impossible to open. Clicking its tab should always get
    // you there, whatever state the window is in.
    public static void FocusWindow(IntPtr h){
        if (h == IntPtr.Zero) return;
        if (IsIconic(h)) ShowWindow(h, 9);                    // SW_RESTORE
        else if (!IsWindowVisible(h)) ShowWindow(h, 5);       // SW_SHOW
        SetForegroundWindow(h);
    }
    // Force a window to the true foreground, bypassing Windows' foreground lock (a background
    // process's plain SetForegroundWindow just flashes the taskbar). Attaches our input thread to
    // the current foreground window's thread so the OS treats us as "the foreground process" for the
    // duration, making SetForegroundWindow actually take. Returns whether the window is now foreground.
    public static bool ForceForeground(IntPtr h){
        if (h == IntPtr.Zero) return false;
        if (IsIconic(h)) ShowWindow(h, 9);                       // SW_RESTORE
        IntPtr fgw = GetForegroundWindow();
        uint fgThread = GetWindowThreadProcessId(fgw, IntPtr.Zero);
        uint me = GetCurrentThreadId();
        bool attached = false;
        if (fgThread != 0 && fgThread != me) attached = AttachThreadInput(fgThread, me, true);
        BringWindowToTop(h);
        bool ok = SetForegroundWindow(h);
        ShowWindow(h, 5);                                        // SW_SHOW
        if (attached) AttachThreadInput(fgThread, me, false);
        return GetForegroundWindow() == h;
    }
    // Put (and keep) a window in the always-on-top band. Rewriting GWL_EXSTYLE drops the topmost
    // state that Form.TopMost established - the window stays exactly where it was drawn but sits
    // BEHIND everything, which looks precisely like "the overlay isn't being drawn at all". Every
    // style change below re-asserts it, and overlays re-assert periodically: other apps going
    // full-screen or topmost can push us out of that band later.
    public static void MakeTopmost(IntPtr h){ SetWindowPos(h, new IntPtr(-1), 0,0,0,0, 0x1|0x2|0x10); }  // NOSIZE|NOMOVE|NOACTIVATE
    public static bool IsTopmost(IntPtr h){ return (GetWindowLong(h, GWL_EXSTYLE) & 0x8) != 0; }
    // Make THIS window a click-through overlay (layered + transparent + no-activate + no taskbar).
    public static void InitClickThrough(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|WS_EX_LAYERED|0x20|0x08000000|0x80); MakeTopmost(h); }
    // Clickable overlay: layered + no-activate + out of alt-tab, but WITHOUT WS_EX_TRANSPARENT.
    // Dropping only 0x20 is the whole difference between an overlay you can click and one you
    // cannot - WS_EX_NOACTIVATE is a separate bit and is what keeps focus in the editor either way.
    // Hit-testing then falls to the layered surface's alpha, so only the pixels actually drawn take
    // a click and the transparent canvas around them still passes clicks straight through.
    public static void InitClickable(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|WS_EX_LAYERED|0x08000000|0x80); MakeTopmost(h); }
    // Don't steal foreground when shown/clicked (WS_EX_NOACTIVATE) - so notification popups
    // don't yank focus off the chat window (which would drop the window-tint bar).
    public static void NoActivate(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|0x08000000); MakeTopmost(h); }
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
    // ALL visible windows whose title ends with `suffix`. Used to snapshot which VS Code windows
    // exist before launching a new one, so the freshly-created window (a handle not in the snapshot)
    // can be identified unambiguously even when it shows the same folder (identical title).
    public static IntPtr[] ListWindowsEndsWith(string suffix){
        var list = new System.Collections.Generic.List<IntPtr>();
        EnumWindows(delegate(IntPtr h, IntPtr l){
            if(!IsWindowVisible(h)) return true;
            int len = GetWindowTextLength(h);
            if(len <= 0) return true;
            var sb = new System.Text.StringBuilder(len+1);
            GetWindowText(h, sb, len+1);
            if(sb.ToString().EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) list.Add(h);
            return true;
        }, IntPtr.Zero);
        return list.ToArray();
    }
    // Publish a small file so readers see either the old contents or the new, never a torn read.
    // These files are written several times a second and read by every other overlay; a plain
    // WriteAllText truncates first, and a reader landing in that window sees an empty file.
    // One short line, written in place, readable while it is being written.
    //
    // This used to write a temp file and rename it over the target. That cost 7.6ms a call on this
    // machine, against 0.6ms for the write below - the rename churns directory metadata, which is
    // the most expensive thing you can ask a filesystem with a virus scanner attached to do. Every
    // overlay writes two of these a second, so it was most of what the HUD cost while idle.
    //
    // The weaker guarantee is fine here, and arguably better. The file is padded to a fixed width
    // and so never changes length: there is no truncation window, and a reader gets the old line or
    // the new one. Every reader already tolerates a bad parse anyway - the slot walker holds its
    // last good value through a grace period, and an unparseable heartbeat reads as "no beat",
    // which needs nine seconds of agreement before anything acts on it. Python writes the same
    // heartbeat files with a plain truncating open() and always has.
    //
    // And the rename was itself the cause of a bug: a file being renamed briefly vanishes from the
    // directory listing, which made tabs slide up a slot and back. Writing in place cannot do that.
    // Read a file another process may be writing. In here rather than in PowerShell because the
    // PowerShell version needed two New-Object calls, and New-Object is a cmdlet: it cost 2.1ms a
    // call against 0.05ms for this. Every overlay reads every other overlay's slot file more than
    // once a second, so that difference was most of what the tab dock cost while doing nothing.
    // Throws exactly as before when the file is missing or briefly locked; callers already catch.
    public static string ReadText(string path){
        using (System.IO.FileStream fs = new System.IO.FileStream(
                   path, System.IO.FileMode.Open, System.IO.FileAccess.Read,
                   System.IO.FileShare.ReadWrite))
        using (System.IO.StreamReader sr = new System.IO.StreamReader(fs))
            return sr.ReadToEnd();
    }
    // Every slot file in a directory, read in one call: each entry is "path|contents". A pipe
    // cannot occur in a Windows path, and slot contents are a GUID and four numbers, so it is an
    // unambiguous separator without resorting to an invisible control character.
    //
    // The walk used to live in PowerShell: a function call per file, plus a function call per read.
    // A PowerShell function invocation costs ~300us whatever it does, so a five-tab dock spent most
    // of its idle budget on call overhead rather than on the eight hundred bytes it was reading.
    // This is the single hottest thing the HUD does - every overlay does it more than once a second.
    public static string[] ReadDir(string dir, string pattern){
        string[] files;
        try { files = System.IO.Directory.GetFiles(dir, pattern); }
        catch { return new string[0]; }
        System.Collections.Generic.List<string> outp = new System.Collections.Generic.List<string>(files.Length);
        foreach (string f in files) {
            try { outp.Add(f + "|" + ReadText(f)); }
            catch { }                       // being replaced, or gone since the listing: skip it
        }
        return outp.ToArray();
    }
    const int LINE_W = 128;
    public static void AtomicWrite(string path, string text){
        byte[] b = System.Text.Encoding.UTF8.GetBytes(text.Length < LINE_W ? text.PadRight(LINE_W) : text);
        using (System.IO.FileStream fs = new System.IO.FileStream(
                   path, System.IO.FileMode.OpenOrCreate, System.IO.FileAccess.Write,
                   System.IO.FileShare.ReadWrite)) {
            fs.Write(b, 0, b.Length);
            if (fs.Length != b.Length) { fs.SetLength(b.Length); }   // only ever on the first write
            fs.Flush();
        }
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
try { Add-Type -TypeDefinition $script:PplSource -ReferencedAssemblies System.Drawing, System.Windows.Forms } catch {}
if (-not ('PerPixelLayered' -as [type])) {          # several helpers can compile this at once and lose
    Start-Sleep -Milliseconds 400
    try { Add-Type -TypeDefinition $script:PplSource -ReferencedAssemblies System.Drawing, System.Windows.Forms } catch {}
}
# Every overlay draws through this type. When the compile loses, the failure used to be swallowed:
# the helper still ran, still heartbeated, and every frame threw where nobody could see it - a window
# tint that looked perfectly alive and never painted anything. Helpers check this and exit instead,
# so the supervisor notices the stale heartbeat and starts a working one.
$script:PplReady = [bool]('PerPixelLayered' -as [type])

# ── cross-process stacking registry ────────────────────────────────────────────
# Each popup owns ONE tiny file (popups\<id>.json) that it alone writes - so there is no
# read-modify-write contention between processes (the old single shared file clobbered
# itself). Readers just glob the folder. Files whose heartbeat went stale = crashed popups.
$script:PopupId  = [Guid]::NewGuid().ToString()
$script:PopupDir = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "popups"
$script:SlotFile = Join-Path $script:PopupDir "$($script:PopupId).slot"
try { [System.IO.Directory]::CreateDirectory($script:PopupDir) | Out-Null } catch {}
function NowMs { [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) }

# Heartbeat for a supervised overlay: timestamp AND pid. Write it at the END of a frame, never the
# start - a helper whose drawing throws every frame would otherwise keep reporting itself healthy
# while painting nothing, and since each holds a named mutex, the replacements the supervisor
# spawned would exit on startup and the HUD would stay broken forever. The pid lets the supervisor
# clear a wedged incumbent out of the way.
# Keep an overlay in the always-on-top band.
#
# Setting Form.TopMost before Show() does NOT reliably produce WS_EX_TOPMOST - a form created
# off-screen comes up without it - and a bare SetWindowPos(HWND_TOPMOST) doesn't take on these
# windows either. Re-assigning the WinForms property does, because the setter re-applies the z-order
# through WinForms' own bookkeeping. The failure is invisible: the overlay draws perfectly, at the
# right size, in the right place, just *underneath* the window it is supposed to sit on - which
# looks exactly like it was never drawn.
function Assert-Topmost($form) {
    try {
        if (-not [PerPixelLayered]::IsTopmost($form.Handle)) { $form.TopMost = $false; $form.TopMost = $true }
    } catch {}
}

# Clicking ANY of the HUD's surfaces for a chat - its tab, its "working on" card, its "needs you"
# card - should land you in that chat, not merely in front of the window that contains it. A window
# holds several chats and only one is in front. We raise the window ourselves (Win32) and leave a
# request for the companion VS Code extension, which is inside that window and can bring the chat's
# tab forward and focus its input. Without the extension installed you still get the window.
function Jump-ToChat($chat, $hwnd, $sid) {
    # Resolve where to go NOW, not from what was true when the caller was drawn. A status card can
    # sit on screen for many minutes and its target is a snapshot: if the chat's window was rebound
    # in the meantime - which happens as soon as the HUD learns which window really holds it - the
    # card would still send you to the window it was bound to when the card appeared. The tabs never
    # had this problem because they re-read this file constantly; now the cards do too.
    if ($sid) {
        try {
            $st = Read-JsonFile (Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice\badges") "$sid.json")
            if ($st) {
                if ($st.tab)       { $chat = [string]$st.tab }
                elseif ($st.title) { $chat = [string]$st.title }
                if ($st.hwnd)      { $hwnd = [int64]$st.hwnd }
            }
        } catch {}
    }
    if ($chat) {
        try {
            $req  = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "focus.json"
            $body = [pscustomobject]@{ title = [string]$chat; sid = [string]$sid; ts = (NowMs) } | ConvertTo-Json -Compress
            $tmp  = $req + ".tmp"
            [System.IO.File]::WriteAllText($tmp, $body)
            [System.IO.File]::Copy($tmp, $req, $true)     # every window sees it; only the owner acts
            [System.IO.File]::Delete($tmp)
        } catch {}
    }
    if ($hwnd -and ([int64]$hwnd) -ne 0) {
        try { [PerPixelLayered]::FocusWindow([IntPtr][int64]$hwnd) } catch {}
    }
}

# Read a file WITHOUT locking out whoever is writing it. These little state files are rewritten
# several times a second by other processes; a plain ReadAllText opens with FileShare.Read, which
# denies writers for the duration. The visible symptom was tabs twitching: a reader and a writer
# collide, the reader sees an error, that tab drops out of the stack for one pass and everything
# below it slides up and then back down. (It can also make the Python side's state write fail.)
function Read-TextShared($path) { return [PerPixelLayered]::ReadText($path) }

# Draw text with a crisp dark outline behind it. The overlays float over whatever happens to be on
# screen, so text that isn't sitting on one of our own dark chips has no idea what colour its
# background is - light grey on a white editor is invisible.
#
# Stroked from the glyph OUTLINE, not by stamping the string at a ring of offsets: repeated
# anti-aliased draws pile up soft edges and read as a smudge rather than an outline, which is
# exactly how the first attempt looked. One path, stroked with a round-joined pen and then filled,
# gives an even border at any size.
#
# $align 'right' puts the text's right edge at $x - handy for right-aligned readouts, and exact,
# because it measures the path's real bounds instead of trusting string metrics to agree.
function Draw-OutlinedText($g, $text, $font, $color, $x, $y, [single]$weight = 3, [string]$align = 'left') {
    if (-not $text) { return }
    $path = $null; $pen = $null; $br = $null
    try {
        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $em = [single]($font.SizeInPoints * $g.DpiY / 72.0)
        $sf = [System.Drawing.StringFormat]::GenericTypographic
        $path.AddString($text, $font.FontFamily, [int]$font.Style, $em,
                        (New-Object System.Drawing.PointF 0, 0), $sf)
        $b = $path.GetBounds()
        $dx = if ($align -eq 'right') { $x - ($b.X + $b.Width) } else { $x - $b.X }
        $m = New-Object System.Drawing.Drawing2D.Matrix
        $m.Translate([single]$dx, [single]($y - $b.Y))
        $path.Transform($m); $m.Dispose()
        $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(235, 0, 0, 0)), $weight
        $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
        $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
        $pen.EndCap   = [System.Drawing.Drawing2D.LineCap]::Round
        $g.DrawPath($pen, $path)
        $br = New-Object System.Drawing.SolidBrush $color
        $g.FillPath($br, $path)
    } catch {
    } finally {
        if ($br) { $br.Dispose() }; if ($pen) { $pen.Dispose() }; if ($path) { $path.Dispose() }
    }
}

function Write-Beat($path) {
    if (-not $path) { return }
    try { [PerPixelLayered]::AtomicWrite($path, "$(NowMs) $PID") } catch {}
}

# ── keeping the reconciler alive ───────────────────────────────────────────────────────────────
# The daemon is the root of the whole HUD: it is what gives every open chat a tab, what retires the
# ones that closed, and the only thing that refreshes the usage figures. Until now it was watched by
# exactly one process - the usage meter - which the daemon in turn watched. Two processes minding
# each other is not supervision: kill both inside a few seconds and the HUD stays down until a hook
# happens to fire. That is not hypothetical, it is what we watched happen.
#
# So every overlay that dot-sources this file can now revive it. Badges are the important ones -
# there is one per open chat, so the watcher count scales with what there is to lose, and they
# survive everything the daemon does because only the daemon can retire them.
$script:HalScriptsDir = $PSScriptRoot
$script:HalBadgeDir   = Join-Path $env:USERPROFILE ".claude\hal_voice\badges"
$script:DaemonStaleMs = 9000        # keep in step with hal_sessions.DAEMON_STALE_MS
$script:lastDaemonChk = 0
$script:daemonJitter  = Get-Random -Minimum 0 -Maximum 2500   # de-phase the herd

function Resolve-HalPython {
    param([string]$BadgeDir = $script:HalBadgeDir)
    try {
        $e = (Read-TextShared (Join-Path $BadgeDir "sessions_daemon.exe")).Trim()
        if ($e -and (Test-Path -LiteralPath $e)) { return $e }
    } catch {}
    foreach ($n in @('pythonw.exe', 'python.exe')) {   # the note said python, the PATH will do
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    return ""
}

function Ensure-HudDaemon {
    param([string]$BadgeDir = $script:HalBadgeDir,
          [string]$SessionsPy = (Join-Path $script:HalScriptsDir 'hal_sessions.py'))
    $ap = Join-Path $BadgeDir "sessions_daemon.alive"
    $now = NowMs
    $beat = 0; $dpid = 0
    try {
        $p = (Read-TextShared $ap).Trim() -split '\s+'
        $beat = [int64]$p[0]
        if ($p.Count -gt 1) { $dpid = [int]$p[1] }
    } catch {}
    if ($beat -gt 0 -and (($now - $beat) -lt $script:DaemonStaleMs)) { return }

    # Whoever takes this mutex is the one that launches. The pre-mark below is the cheap filter and
    # does most of the work, but it is a read-then-write, not a test-and-set: without this gate,
    # eight badges noticing in the same millisecond genuinely start eight interpreters, and seven of
    # them pay a full python startup before losing the daemon's own mutex and exiting.
    $got = $false
    $mx = New-Object System.Threading.Mutex($false, "hal_session_daemon_spawn")
    try {
        try { $got = $mx.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $got = $true }
        if (-not $got) { return }
        try {   # re-read under the gate: the winner may already have pre-marked it
            $b2 = [int64](((Read-TextShared $ap).Trim() -split '\s+')[0])
            if (($now - $b2) -lt $script:DaemonStaleMs) { return }
        } catch {}
        # A daemon that stopped beating but is still running is wedged, and it still owns
        # "hal_session_daemon" - so a replacement would exit on startup and we would respawn forever.
        if ($dpid -gt 0) { try { Stop-Process -Id $dpid -Force -ErrorAction SilentlyContinue } catch {} }
        $exe = Resolve-HalPython $BadgeDir
        if (-not $exe) { return }
        try { [PerPixelLayered]::AtomicWrite($ap, "$now 0") } catch {}
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exe
        $psi.Arguments = '"{0}" --daemon' -f $SessionsPy
        $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
        $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
        [System.Diagnostics.Process]::Start($psi) | Out-Null
    } catch {
    } finally {
        if ($got) { try { $mx.ReleaseMutex() } catch {} }
        try { $mx.Dispose() } catch {}
    }
}

# Call this from a tick; it rate-limits itself, with per-process jitter so N badges do not all check
# on the same frame.
function Poll-HudDaemon {
    $now = NowMs
    if (($now - $script:lastDaemonChk) -lt (3000 + $script:daemonJitter)) { return }
    $script:lastDaemonChk = $now
    Ensure-HudDaemon
}
$script:BornMs  = NowMs             # stable birth time
$script:slotCache = @{}             # last good read per slot file (see Stack-Sync)
$script:SLOT_LIVE_MS = 2500        # beat age at which an overlay counts as gone (beats every 600ms)
$script:SLOT_READ_GRACE = 5000     # how long a last-good entry stands in for unreadable files
$script:StackOrd = $script:BornMs   # stack sort key (defaults to birth time; drag-reorder overrides it)

# Heartbeat our own slot file, then return all live popups (newest first). When not alive,
# delete our slot so the others close the gap.
function Stack-Sync($height, $alive) {
    $now = NowMs
    if (-not $alive) {
        try { [System.IO.File]::Delete($script:SlotFile) } catch {}
        return @()
    }
    try {
        [PerPixelLayered]::AtomicWrite($script:SlotFile,
            ("{0} {1} {2} {3} {4}" -f $script:PopupId, $script:BornMs, $script:StackOrd, [int]$height, $now))
    } catch {}
    return Stack-Peek $true
}

# Write our own slot without reading anybody else's.
#
# Opening a file costs ~135us on Windows however small it is, so a five-tab dock re-reading every
# slot at 600ms was spending most of its idle budget on file opens. But the ORDER almost never
# changes - only the beats inside the files do - so the write and the read want different cadences:
# beat often, re-read rarely, and notice structural change by the directory's own timestamp, which
# ticks when a slot is created or deleted and costs a single stat.
function Stack-Write($height) {
    try {
        [PerPixelLayered]::AtomicWrite($script:SlotFile,
            ("{0} {1} {2} {3} {4}" -f $script:PopupId, $script:BornMs, $script:StackOrd, [int]$height, (NowMs)))
    } catch {}
}

function Stack-DirStamp {
    try { return [System.IO.Directory]::GetLastWriteTimeUtc($script:PopupDir).Ticks } catch { return 0 }
}

# Read the order without writing anything. Split out of Stack-Sync for the drag: while a tab is
# being moved the others need to re-read the order many times a second, and they have no reason to
# rewrite their own slot file that often - an atomic replace apiece, per frame, per badge, is real
# I/O on the UI thread and it showed up as stutter in exactly the moment that wants to be smooth.
# Reaping is the writer's job too, so a peek never deletes anything.
function Stack-Peek($reap = $false) {
    $now = NowMs
    $live = New-Object System.Collections.ArrayList
    try {
        # Every slot on disk, listed and read in ONE compiled call. It used to be a PowerShell
        # function call per file plus another per read, at ~300us of interpreter overhead apiece,
        # which was most of what an idle tab dock cost.
        $fresh = @{}
        foreach ($e in [PerPixelLayered]::ReadDir($script:PopupDir, "*.slot")) {
            $i = $e.IndexOf("|")
            if ($i -lt 1) { continue }
            $f = $e.Substring(0, $i)
            $p = $e.Substring($i + 1).Trim() -split ' '
            if ($p.Count -lt 5) { continue }
            $o = [pscustomobject]@{ id = $p[0]; ts = [double]$p[1]; ord = [double]$p[2]
                                    h = [int]$p[3]; beat = [int64]$p[4]; seen = $now }
            $fresh[$f] = $o
            $script:slotCache[$f] = $o
        }
        # Walk what we just read UNION what we have seen before. A file that is momentarily
        # unreadable, or briefly missing from the listing, says nothing about whether that overlay
        # is alive - so hold the last good entry as live for a grace period instead of letting it
        # drop out. Letting entries vanish is what made every tab below one slide up a slot and back:
        # that was the jitter. Death is still detected honestly, by a readable file whose beat has
        # stopped moving.
        $paths = New-Object System.Collections.Generic.HashSet[string]
        foreach ($x in $fresh.Keys) { [void]$paths.Add($x) }
        foreach ($x in @($script:slotCache.Keys)) { [void]$paths.Add($x) }
        foreach ($f in $paths) {
            $o = $fresh[$f]
            if ($null -eq $o) {
                $o = $script:slotCache[$f]
                if ($null -eq $o) { continue }
                if (($now - $o.seen) -lt $script:SLOT_READ_GRACE) { [void]$live.Add($o); continue }
                $script:slotCache.Remove($f)          # unreadable for too long: let it go
                continue
            }
            if (($now - $o.beat) -lt $script:SLOT_LIVE_MS) { [void]$live.Add($o) }
            elseif ($reap -and ($now - $o.beat) -ge 5000) {
                try { [System.IO.File]::Delete($f); $script:slotCache.Remove($f) } catch {}
            }
        }
        if ($reap) {
            foreach ($old in [System.IO.Directory]::GetFiles($script:PopupDir, "*.json")) {  # old format
                try { [System.IO.File]::Delete($old) } catch {}      # leftovers from the JSON format
            }
        }
    } catch {}
    return @($live | Sort-Object -Property @{ Expression = { if ($null -ne $_.ord) { [double]$_.ord } else { [double]$_.ts } } } -Descending)
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
    $script:SlotFile = Join-Path $script:PopupDir "$($script:PopupId).slot"
    try { [System.IO.Directory]::CreateDirectory($script:PopupDir) | Out-Null } catch {}
}

# ── master on/off flag ───────────────────────────────────────────────────────
# The HUD's overlays poll Hud-Enabled and close themselves when the HUD is switched off. The flag
# lives in the plugin config; the VS Code status-bar extension flips it (Set-HudEnabled is kept for
# any in-process caller). Stored in the plugin config.
$script:HalCfgPath = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "config.json"

# This file is shared with Python and with the VS Code extension, so it must be plain UTF-8 with NO
# byte-order mark: `JSON.parse` and `json.load` both choke on one. Windows PowerShell's
# `Set-Content -Encoding utf8` writes a BOM and `Get-Content` without -Encoding reads UTF-8 as ANSI
# (mangling any non-ASCII on the way back out), so go through .NET, which does neither.
function Read-JsonFile($path) {
    try { return (Read-TextShared $path).TrimStart([char]0xFEFF) | ConvertFrom-Json } catch { return $null }
}
function Write-JsonFile($path, $obj) {
    $json = $obj | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($path, $json)     # UTF-8, no BOM
}

$script:cfgMt = [datetime]::MinValue
$script:cfgCache = $null
function Hud-Enabled {
    # Polled about once a second by every overlay; re-parsing the config each time is pure waste,
    # so only re-read it when the file's timestamp moves.
    try {
        $mt = [System.IO.File]::GetLastWriteTimeUtc($script:HalCfgPath)
        if ($mt -ne $script:cfgMt) { $script:cfgCache = Read-JsonFile $script:HalCfgPath; $script:cfgMt = $mt }
    } catch { }
    $c = $script:cfgCache
    if ($c -and ($c.PSObject.Properties.Name -contains 'enabled')) { return [bool]$c.enabled }
    return $true
}

# Bottom-anchored variant: newest sits AT the bottom anchor, older stack upward above it.
function Stack-TargetBottom($bottomAnchor, $gap, $ordered, $selfHeight) {
    $below = 0
    foreach ($e in $ordered) {
        if ($e.id -eq $script:PopupId) { break }
        if ([int]$e.h -le 0) { continue }     # parked: still holds its place in the order, takes no room
        $below += [int]$e.h + $gap
    }
    return [int]($bottomAnchor - $below - [int]$selfHeight)
}

# ── how many tabs actually fit ─────────────────────────────────────────────────────────────────
# The stack grows upward from the dock and nothing ever stopped it. Past about twenty chats the
# topmost tabs walk off the top of the screen, where they cannot be clicked and cannot be seen -
# and the usage meter goes first, because it rides above them.
#
# So there is a ceiling, and it is geometric by default: tabs are only ever parked when the
# alternative is being off-screen, which means the behaviour never surprises anyone who has not hit
# it. `max_tabs` in the config lowers it further for anyone who wants a shorter dock.
#
# Parked tabs are not retired. They keep their slot file, and therefore their place in the order, so
# the ranking every badge computes stays identical whether a tab is parked or not - which is what
# stops the whole thing oscillating: hiding a tab must not change who else gets hidden.
function Stack-Capacity($bottomAnchor, $pitch, $reserve, $topMargin = 8) {
    if ($pitch -le 0) { return 1 }
    $n = [int][Math]::Floor(($bottomAnchor - $topMargin - $reserve) / $pitch)   # Floor: [int] rounds
    if ($n -lt 1) { return 1 }
    return $n
}

function Stack-VisibleLimit($total, $maxTabs, $capacity) {
    $lim = [int]$capacity
    if (([int]$maxTabs) -gt 0 -and ([int]$maxTabs) -lt $lim) { $lim = [int]$maxTabs }
    if ($lim -lt 1) { $lim = 1 }
    if (([int]$total) -lt $lim) { return [int]$total }
    return $lim
}

function Stack-RankOf($ordered, $id) {
    for ($i = 0; $i -lt @($ordered).Count; $i++) {
        if ($ordered[$i].id -eq $id) { return $i }
    }
    return 0
}

# ── live drag ──────────────────────────────────────────────────────────────────────────────────
# Reordering used to be a surprise: you dragged a tab, nothing else moved, and the stack rearranged
# itself the instant you let go. The tabs should get out of the way while you are still deciding.
#
# The mechanics are already there - every badge eases toward whatever slot the shared ordering gives
# it - so all that was missing was for the dragged tab to publish where it currently WOULD land, and
# for the others to look often enough to notice. This flag is how they know to look: one file whose
# timestamp is the whole message, so the check is a stat and not a read.
function Stack-DragFlagPath {
    # Cached: Join-Path is a cmdlet at ~125us a call, and Stack-DragActive runs every frame
    # of every badge. The namespace is set once at startup and never moves.
    if (-not $script:dragFlagPath) { $script:dragFlagPath = $script:PopupDir + "\_drag.flag" }
    return $script:dragFlagPath
}

function Stack-SignalDrag {
    try { [PerPixelLayered]::AtomicWrite((Stack-DragFlagPath), "$(NowMs)") } catch {}
}

function Stack-DragActive {
    try {
        $f = Stack-DragFlagPath
        $age = ([DateTime]::UtcNow - [System.IO.File]::GetLastWriteTimeUtc($f)).TotalMilliseconds
        return ($age -ge 0 -and $age -lt 700)
    } catch { return $false }
}

# ── the dock handle ────────────────────────────────────────────────────────────────────────────
# Sliding the whole dock off the right edge and back. Distinct from the HUD's on/off flag: that
# retires every overlay, this just gets them out of the way for a minute and keeps them running, so
# a chat that needs you still has a tab to come back to.
#
# The hard part is that the dock is not one window - it is a tab per chat plus the meter, each its
# own process with its own frame clock. Left to ease themselves toward a target they drift apart
# immediately, and worse, each tab would travel a DIFFERENT distance, because a chip is only as wide
# as its label. A narrow tab finishes while a wide one is still going. It reads as things scattering
# rather than a panel closing.
#
# So nobody eases anything. The flag carries the instant the move begins, and every overlay computes
# the same curve from the same wall clock: offset = TRAVEL * smoothstep((now - start) / DURATION).
# Identical input, identical output, and they move as one object whatever their frame rates are.
# The start is set a little in the FUTURE - long enough that every overlay has polled and seen it
# before the first pixel moves, so nothing joins late and has to snap into position.
$script:DockFlag = Join-Path (Join-Path $env:USERPROFILE ".claude\hal_voice") "dock.stow"
$script:DOCK_POLL_MS  = 60      # how often an overlay re-reads the flag
$script:DOCK_LEAD_MS  = 90      # grace before the move starts, so everyone is ready
$script:DOCK_DUR_MS   = 300
$script:DOCK_TRAVEL   = 560     # far enough that even the widest tab clears the screen edge
$script:dockStowed = $false
$script:dockStart  = 0
$script:dockChecked = 0

function Dock-Refresh {
    $now = NowMs
    if (($now - $script:dockChecked) -lt $script:DOCK_POLL_MS) { return }
    $script:dockChecked = $now
    try {
        $p = ([PerPixelLayered]::ReadText($script:DockFlag)).Trim() -split '\s+'
        $script:dockStowed = ($p[0] -eq '1')
        if ($p.Count -gt 1) { $script:dockStart = [int64]$p[1] }
    } catch {
        $script:dockStowed = $false      # no flag yet: the dock is out, and has always been out
        $script:dockStart = 0
    }
}

function Dock-Stowed { Dock-Refresh; return $script:dockStowed }

# How far through the move we are, from elapsed milliseconds. Smoothstep rather than a spring: a
# spring carries state, and state is the one thing these processes cannot share. It also starts and
# stops at zero speed, so the dock eases into motion and settles rather than jerking at both ends.
# Pure, and separated out purely so it can be tested at exact instants.
function Dock-Curve($elapsedMs) {
    if ($elapsedMs -le 0) { return 0.0 }
    if ($elapsedMs -ge $script:DOCK_DUR_MS) { return 1.0 }
    $x = $elapsedMs / [double]$script:DOCK_DUR_MS
    return $x * $x * (3.0 - 2.0 * $x)
}

# 0 = fully out, 1 = fully stowed.
function Dock-Phase {
    Dock-Refresh
    $k = Dock-Curve ((NowMs) - $script:dockStart)
    if ($script:dockStowed) { return $k }
    return 1.0 - $k
}

# How far right everything is, in pixels. Every element adds this to wherever it would otherwise be,
# so they all travel the same distance at the same moment - which is what makes it read as one panel
# rather than as a handful of tabs leaving separately.
function Dock-Offset { return $script:DOCK_TRAVEL * (Dock-Phase) }

function Dock-Moving {
    Dock-Refresh
    $t = (NowMs) - $script:dockStart
    return ($t -gt (-$script:DOCK_LEAD_MS - 40) -and $t -lt ($script:DOCK_DUR_MS + 40))
}

function Set-DockStowed($v) {
    $start = (NowMs) + $script:DOCK_LEAD_MS
    $flag = 0; if ($v) { $flag = 1 }
    try {
        [void][System.IO.Directory]::CreateDirectory((Split-Path $script:DockFlag))
        [PerPixelLayered]::AtomicWrite($script:DockFlag, ("{0} {1}" -f $flag, $start))
    } catch { }
    $script:dockChecked = 0        # so this process sees it on its very next frame too
}

function Hud-ConfigNum($name, $default) {
    try {
        $mt = [System.IO.File]::GetLastWriteTimeUtc($script:HalCfgPath)
        if ($mt -ne $script:cfgMt) { $script:cfgCache = Read-JsonFile $script:HalCfgPath; $script:cfgMt = $mt }
    } catch { }
    $c = $script:cfgCache
    if ($c -and ($c.PSObject.Properties.Name -contains $name)) {
        try { return [double]$c.$name } catch { return $default }
    }
    return $default
}
