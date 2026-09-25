#!/usr/bin/env python3
"""画「家宽选择器.app」的图标：深色圆角底 + 悬浮窗里「家宽」的那种绿色房子，输出 .icns。

    python3 scripts/make_icon.py 输出.icns

每个尺寸都按矢量重画，不是从大图缩；需要 PyObjC 和 macOS 自带的 iconutil。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

GREEN = (0.22, 0.92, 0.50)  # 和 float.py 里 homebb 的颜色一致
SIZES = (16, 32, 128, 256, 512)


def draw_png(px: int, out: Path) -> None:
    from AppKit import (
        NSBezierPath,
        NSBitmapImageRep,
        NSColor,
        NSFontWeightSemibold,
        NSGradient,
        NSGraphicsContext,
        NSImage,
        NSImageSymbolConfiguration,
        NSShadow,
    )
    from Foundation import NSMakeRect, NSMakeSize, NSZeroRect

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
    )
    rep.setSize_(NSMakeSize(px, px))
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    s = px / 1024.0

    # macOS 图标网格：824×824 圆角矩形居中，下方带一点投影
    inset = 100 * s
    body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(inset, inset, px - 2 * inset, px - 2 * inset), 185 * s, 185 * s
    )
    NSGraphicsContext.saveGraphicsState()
    shadow = NSShadow.alloc().init()
    shadow.setShadowOffset_(NSMakeSize(0, -10 * s))
    shadow.setShadowBlurRadius_(24 * s)
    shadow.setShadowColor_(NSColor.colorWithWhite_alpha_(0, 0.35))
    shadow.set()
    NSColor.colorWithSRGBRed_green_blue_alpha_(0.10, 0.11, 0.14, 1).setFill()
    body.fill()
    NSGraphicsContext.restoreGraphicsState()
    grad = NSGradient.alloc().initWithStartingColor_endingColor_(
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.19, 0.22, 0.28, 1),
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.07, 0.08, 0.11, 1),
    )
    grad.drawInBezierPath_angle_(body, -90)

    green = NSColor.colorWithSRGBRed_green_blue_alpha_(*GREEN, 1)
    cfg = NSImageSymbolConfiguration.configurationWithPointSize_weight_(420 * s, NSFontWeightSemibold)
    cfg = cfg.configurationByApplyingConfiguration_(NSImageSymbolConfiguration.configurationWithPaletteColors_([green]))
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("house.fill", None)
    if img is not None:
        img = img.imageWithSymbolConfiguration_(cfg)
        w, h = img.size().width, img.size().height
        box = 500 * s
        k = min(box / w, box / h)
        w, h = w * k, h * k
        img.drawInRect_fromRect_operation_fraction_(
            NSMakeRect((px - w) / 2, (px - h) / 2 + 12 * s, w, h), NSZeroRect, 2, 1.0  # 2 = SourceOver
        )
    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(4, {})  # 4 = PNG
    data.writeToFile_atomically_(str(out), True)


def make_icns(out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for n in SIZES:
            draw_png(n, iconset / f"icon_{n}x{n}.png")
            draw_png(n * 2, iconset / f"icon_{n}x{n}@2x.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("用法：python3 scripts/make_icon.py 输出.icns", file=sys.stderr)
        return 2
    make_icns(Path(args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
