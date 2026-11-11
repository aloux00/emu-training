# -*- coding: utf-8 -*-
"""生成与排课系统视觉一致的两款钉钉图标（纯代码绘制，无水印、可无损缩放）。

输出：
  assets/dingtalk-bot-*.png    钉钉机器人头像（蓝底 + 白色机器人 + 日历徽标）
  assets/dingtalk-app-*.png    钉钉企业应用图标（蓝底 + 日历 + CT 环形扫描 + 课程卡）
配色取自 static/index.html：主色 #185FA5，深色 #0C447C。
"""
import os
from PIL import Image, ImageDraw, ImageFilter

PRIMARY = (24, 95, 165)     # #185FA5
DARK = (12, 68, 124)        # #0C447C
TOP = (34, 116, 184)        # 渐变亮端
WHITE = (255, 255, 255)
LIGHT_BLUE = (108, 182, 232)  # #6CB6E8
MID_BLUE = (90, 169, 222)     # #5AA9DE
GRID = (220, 230, 240)

S = 4          # 超采样倍数
BASE = 1024    # 设计尺寸
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def gradient_bg(size: int) -> Image.Image:
    """135° 对角渐变（左上亮 → 右下深），与登录页渐变为同一色系。"""
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        for x in range(0, size, 8):
            t = (x + y) / (2.0 * (size - 1))
            c = tuple(int(TOP[i] + (DARK[i] - TOP[i]) * t) for i in range(3))
            for dx in range(8):
                if x + dx < size:
                    px[x + dx, y] = c
    return img.convert("RGBA")


def rr(d, box, r, fill):
    d.rounded_rectangle(box, radius=r, fill=fill)


def ring(d, cx, cy, r, width, color):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=width)


def robot_icon() -> Image.Image:
    W = BASE * S
    img = gradient_bg(W).resize((W, W), Image.LANCZOS) if False else gradient_bg(W)
    d = ImageDraw.Draw(img, "RGBA")
    k = S

    # 天线
    d.line((512 * k, 232 * k, 512 * k, 320 * k), fill=WHITE, width=24 * k)
    d.ellipse((474 * k, 186 * k, 550 * k, 262 * k), fill=LIGHT_BLUE)

    # 头部
    rr(d, (232 * k, 316 * k, 792 * k, 688 * k), 96 * k, WHITE)

    # 眼睛（含高光）
    for cx in (392, 632):
        d.ellipse(((cx - 56) * k, (462 - 56) * k, (cx + 56) * k, (462 + 56) * k), fill=MID_BLUE)
        d.ellipse(((cx - 40) * k, (446 - 34) * k, (cx - 40 + 34) * k, (446 - 34 + 34) * k), fill=(255, 255, 255, 230))

    # 微笑条
    rr(d, (452 * k, 580 * k, 572 * k, 616 * k), 18 * k, MID_BLUE)

    # 日历徽标（右下）
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    rr(sd, (696 * k, 690 * k, 892 * k, 886 * k), 46 * k, (0, 0, 0, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10 * k)))
    d = ImageDraw.Draw(img, "RGBA")

    rr(d, (688 * k, 682 * k, 884 * k, 878 * k), 46 * k, WHITE)
    # 顶部蓝色条（圆角仅上沿）
    rr(d, (688 * k, 682 * k, 884 * k, 760 * k), 46 * k, PRIMARY)
    d.rectangle((688 * k, 736 * k, 884 * k, 760 * k), fill=WHITE)
    # 日期点阵
    for row in range(3):
        for col in range(3):
            cx = 738 + col * 48
            cy = 792 + row * 34
            d.ellipse(((cx - 9) * k, (cy - 9) * k, (cx + 9) * k, (cy + 9) * k), fill=PRIMARY)
    return img


def app_icon() -> Image.Image:
    W = BASE * S
    img = gradient_bg(W)
    d = ImageDraw.Draw(img, "RGBA")
    k = S

    # CT 环形扫描底纹
    ring(d, 512 * k, 470 * k, 330 * k, 10 * k, (255, 255, 255, 46))
    ring(d, 512 * k, 470 * k, 236 * k, 8 * k, (255, 255, 255, 38))
    import math
    for i in range(16):
        a = math.pi * 2 * i / 16
        x1, y1 = 512 + 330 * math.cos(a), 470 + 330 * math.sin(a)
        x2, y2 = 512 + 366 * math.cos(a), 470 + 366 * math.sin(a)
        d.line((x1 * k, y1 * k, x2 * k, y2 * k), fill=(255, 255, 255, 52), width=8 * k)

    # 日历主体
    rr(d, (286 * k, 286 * k, 738 * k, 760 * k), 66 * k, WHITE)
    rr(d, (286 * k, 286 * k, 738 * k, 470 * k), 66 * k, PRIMARY)
    d.rectangle((286 * k, 392 * k, 738 * k, 470 * k), fill=WHITE)
    # 网格
    for x in (406, 512, 618):
        d.line((x * k, 440 * k, x * k, 726 * k), fill=GRID, width=6 * k)
    for y in (492, 574, 656):
        d.line((318 * k, y * k, 706 * k, y * k), fill=GRID, width=6 * k)

    # 两张课程卡（右下，带投影）
    shadow = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    rr(sd, (486 * k, 540 * k, 726 * k, 726 * k), 34 * k, (0, 0, 0, 80))
    rr(sd, (556 * k, 626 * k, 800 * k, 816 * k), 34 * k, (0, 0, 0, 80))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9 * k)))
    d = ImageDraw.Draw(img, "RGBA")

    for (x1, y1, x2, y2), bar in (((478, 532, 718, 718), True), ((548, 618, 792, 808), False)):
        rr(d, (x1 * k, y1 * k, x2 * k, y2 * k), 34 * k, WHITE)
        if bar:
            rr(d, ((x1 + 26) * k, (y1 + 28) * k, (x1 + 114) * k, (y1 + 48) * k), 10 * k, PRIMARY)
        d.line(((x1 + 26) * k, (y1 + 82) * k, (x2 - 30) * k, (y1 + 82) * k), fill=(214, 226, 238), width=8 * k)
        d.line(((x1 + 26) * k, (y1 + 112) * k, (x2 - 70) * k, (y1 + 112) * k), fill=(224, 233, 242), width=8 * k)
    return img


def save_all(img: Image.Image, stem: str):
    big = img.resize((1024, 1024), Image.LANCZOS)
    for size in (1024, 512, 256, 128):
        out = big.resize((size, size), Image.LANCZOS)
        p = os.path.join(OUT_DIR, "%s-%d.png" % (stem, size))
        out.convert("RGB").save(p, "PNG", optimize=True)
        print("saved", p)


if __name__ == "__main__":
    save_all(robot_icon(), "dingtalk-bot")
    save_all(app_icon(), "dingtalk-app")
