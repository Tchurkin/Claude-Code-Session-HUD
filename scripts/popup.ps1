param(
    [string]$Text = "PROJECT DOMINATED!!",
    [int]$DurationMs = 4500
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# ── Per-pixel-alpha layered window helper (true transparency + glow) ───────────
$src = @"
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public class PerPixelLayered {
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; public POINT(int x,int y){X=x;Y=y;} }
    [StructLayout(LayoutKind.Sequential)] public struct SIZE  { public int cx, cy; public SIZE(int x,int y){cx=x;cy=y;} }
    [StructLayout(LayoutKind.Sequential, Pack=1)] public struct BLENDFUNCTION { public byte BlendOp, BlendFlags, SourceConstantAlpha, AlphaFormat; }
    [DllImport("user32.dll", SetLastError=true)] static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll", SetLastError=true)] static extern int SetWindowLong(IntPtr h, int i, int v);
    [DllImport("user32.dll", SetLastError=true)] static extern bool UpdateLayeredWindow(IntPtr h, IntPtr dst, ref POINT pdst, ref SIZE ps, IntPtr src, ref POINT psrc, int key, ref BLENDFUNCTION bf, int flags);
    [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr h, IntPtr dc);
    [DllImport("gdi32.dll")]  static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")]  static extern IntPtr SelectObject(IntPtr dc, IntPtr o);
    [DllImport("gdi32.dll")]  static extern bool DeleteDC(IntPtr dc);
    [DllImport("gdi32.dll")]  static extern bool DeleteObject(IntPtr o);
    const int GWL_EXSTYLE=-20, WS_EX_LAYERED=0x80000, ULW_ALPHA=2;
    public static void Init(IntPtr h){ SetWindowLong(h, GWL_EXSTYLE, GetWindowLong(h,GWL_EXSTYLE)|WS_EX_LAYERED); }
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

$screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea

$CW=440; $R=6; $GLOW=16; $PAD_L=18; $PAD_T=11; $HDR_H=18
$ACCENT = [System.Drawing.Color]::FromArgb(0, 215, 80)

$hdrFont = New-Object System.Drawing.Font("Segoe UI", 8,  [System.Drawing.FontStyle]::Bold)
$msgFont = New-Object System.Drawing.Font("Segoe UI", 13, [System.Drawing.FontStyle]::Bold)

# Measure message height
$tb = New-Object System.Drawing.Bitmap(1,1); $tg = [System.Drawing.Graphics]::FromImage($tb)
$meas = $tg.MeasureString($Text, $msgFont, ($CW - $PAD_L - 30))
$tg.Dispose(); $tb.Dispose()
$textH = [int][Math]::Ceiling($meas.Height) + 4
$CH = $PAD_T + $HDR_H + 6 + $textH + $PAD_T

$FORM_W = $CW + $GLOW*2
$FORM_H = $CH + $GLOW*2

# Close-X geometry (bitmap coords)
$CS=10; $CXL = $GLOW + $CW - 24; $CYT = $GLOW + 12
$script:closeHot = $false

function CA($a,$c){ [System.Drawing.Color]::FromArgb([int]$a, $c.R, $c.G, $c.B) }
function RoundedPath($x,$y,$w,$h,$r){
    # square LEFT corners, rounded RIGHT corners
    $p = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = $r*2
    $p.AddLine($x, $y, ($x+$w-$r), $y)                  # top edge (from square top-left)
    $p.AddArc(($x+$w-$d), $y,         $d,$d, 270, 90)   # rounded top-right
    $p.AddArc(($x+$w-$d), ($y+$h-$d), $d,$d, 0,   90)   # rounded bottom-right
    $p.AddLine(($x+$w-$r), ($y+$h), $x, ($y+$h))        # bottom edge (to square bottom-left)
    $p.CloseFigure()                                    # left edge (square corners)
    return $p
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition   = [System.Windows.Forms.FormStartPosition]::Manual
$form.ShowInTaskbar   = $false
$form.TopMost         = $true
$form.Width  = $FORM_W
$form.Height = $FORM_H
$form.Left   = $screen.Right - $CW - 20 - $GLOW
$form.Top    = $screen.Top + 36 - $GLOW

$render = {
    $bmp = New-Object System.Drawing.Bitmap($FORM_W, $FORM_H, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    # Outer glow: concentric rounded strokes bleeding into the transparent margin
    for ($sp = $GLOW; $sp -ge 1; $sp--) {
        $alpha = [int](190 * [Math]::Exp(-$sp * 0.28))
        if ($alpha -lt 4) { continue }
        $x=$GLOW-$sp; $y=$GLOW-$sp; $w=$CW+$sp*2; $h=$CH+$sp*2; $r=[Math]::Min($R+$sp,14)
        $gp  = RoundedPath $x $y $w $h $r
        $pen = New-Object System.Drawing.Pen((CA $alpha $ACCENT), 1.5)
        $g.DrawPath($pen, $gp); $pen.Dispose(); $gp.Dispose()
    }

    # Content: semi-transparent dark fill
    $cpath = RoundedPath $GLOW $GLOW $CW $CH $R
    $bg = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(224, 18, 18, 18))
    $g.FillPath($bg, $cpath); $bg.Dispose()

    # Accent strip (clipped to rounded content)
    $g.SetClip($cpath)
    $sb = New-Object System.Drawing.SolidBrush $ACCENT
    $g.FillRectangle($sb, $GLOW, $GLOW, 5, $CH); $sb.Dispose()
    $g.ResetClip()

    # Border
    $bpen = New-Object System.Drawing.Pen((CA 215 $ACCENT), 1.3)
    $g.DrawPath($bpen, $cpath); $bpen.Dispose()
    $cpath.Dispose()

    # Header
    $hb = New-Object System.Drawing.SolidBrush $ACCENT
    $g.DrawString("CLAUDE CODE", $hdrFont, $hb, [float]($GLOW+$PAD_L), [float]($GLOW+$PAD_T)); $hb.Dispose()

    # Message
    $mb = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(243,243,243))
    $rect = New-Object System.Drawing.RectangleF ([float]($GLOW+$PAD_L), [float]($GLOW+$PAD_T+$HDR_H+4), [float]($CW-$PAD_L-30), [float]$textH)
    $g.DrawString($Text, $msgFont, $mb, $rect); $mb.Dispose()

    # Close X
    $cc = if ($script:closeHot) { 245 } else { 150 }
    $xp = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb($cc,205,205,205)), 1.8
    $g.DrawLine($xp, $CXL, $CYT, ($CXL+$CS), ($CYT+$CS))
    $g.DrawLine($xp, ($CXL+$CS), $CYT, $CXL, ($CYT+$CS))
    $xp.Dispose()

    $g.Dispose()
    [PerPixelLayered]::SetBitmap($form.Handle, $bmp, $form.Left, $form.Top, 244)
    $bmp.Dispose()
}

function HitClose($x,$y){ ($x -ge ($CXL-7)) -and ($x -le ($CXL+$CS+7)) -and ($y -ge ($CYT-7)) -and ($y -le ($CYT+$CS+7)) }

$form.Add_MouseDown({ param($s,$e) if (HitClose $e.X $e.Y) { $form.Close() } })
$form.Add_MouseMove({
    param($s,$e)
    $h = HitClose $e.X $e.Y
    if ($h -ne $script:closeHot) { $script:closeHot = $h; & $render }
})
$form.Add_Shown({ [PerPixelLayered]::Init($form.Handle); & $render })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $DurationMs
$timer.Add_Tick({ $form.Close() })
$timer.Start()

[System.Windows.Forms.Application]::Run($form)
