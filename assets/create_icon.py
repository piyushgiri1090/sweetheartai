from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parent
SIZE = 256


def create_icon():
    image = Image.new("RGBA", (SIZE, SIZE), (9, 13, 21, 255))
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((35, 35, 221, 221), fill=(245, 185, 66, 75))
    image = Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(24)))

    draw = ImageDraw.Draw(image)
    draw.ellipse((17, 17, 239, 239), fill=(11, 17, 26, 255), outline=(245, 185, 66, 255), width=5)
    draw.ellipse((31, 31, 225, 225), fill=(23, 42, 57, 255), outline=(98, 217, 245, 210), width=3)
    heart = [
        (128, 200), (54, 133), (46, 122), (43, 107), (46, 90), (55, 76),
        (69, 68), (84, 66), (99, 71), (113, 83), (128, 99),
        (143, 83), (157, 71), (172, 66), (187, 68), (201, 76),
        (210, 90), (213, 107), (210, 122), (202, 133),
    ]
    heart_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(heart_glow).polygon(heart, fill=(255, 91, 67, 180))
    image = Image.alpha_composite(image, heart_glow.filter(ImageFilter.GaussianBlur(12)))
    draw = ImageDraw.Draw(image)
    draw.polygon(heart, fill=(224, 70, 60, 255), outline=(255, 209, 116, 255))
    draw.line([(64, 107), (82, 91), (100, 92), (128, 121)], fill=(255, 166, 120, 230), width=5, joint="curve")
    draw.ellipse((115, 113, 141, 139), fill=(185, 244, 255, 255), outline=(245, 255, 255, 255), width=2)
    draw.line((128, 20, 128, 34), fill=(245, 185, 66, 255), width=4)
    draw.line((128, 222, 128, 236), fill=(245, 185, 66, 255), width=4)
    draw.line((20, 128, 34, 128), fill=(245, 185, 66, 255), width=4)
    draw.line((222, 128, 236, 128), fill=(245, 185, 66, 255), width=4)

    image.save(ROOT / "sweetheart.ico", format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])


if __name__ == "__main__":
    create_icon()
