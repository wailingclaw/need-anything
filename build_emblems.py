from pathlib import Path
from PIL import Image, ImageDraw
import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent

SOURCE = ROOT / "emblem-source.png"
OUTPUT = ROOT / "emblem-artwork.webp"

TILE_SIZE = 256
COLUMNS = 6
ROWS = 5

FINISHES = [
    # Nature Inspired
    "sand",
    "sunset",
    "meadow",
    "forest",
    "ocean",
    "glacier",
    "lavender",
    "stone",

    # Modern Pastels
    "sunshine",
    "peach",
    "blush",
    "lilac",
    "periwinkle",
    "sky",
    "mint",
    "lime",

    # Polished Metals
    "gold",
    "copper",
    "rose-gold",
    "silver",
    "iron",

    # Gemstone Tones
    "emerald",
    "ruby",
    "sapphire",

    # Iridescent Finishes
    "diamond",
    "opal",
    "titanium",
    "aurora",
    "nebula",
]

CENTERS = [
    # Nature Inspired
    (560, 186),
    (701, 186),
    (842, 186),
    (984, 186),
    (1126, 186),
    (1268, 186),
    (1410, 186),
    (1551, 186),

    # Modern Pastels
    (560, 385),
    (701, 385),
    (842, 385),
    (984, 385),
    (1126, 385),
    (1268, 385),
    (1410, 385),
    (1551, 385),

    # Polished Metals
    (91, 768),
    (207, 768),
    (323, 768),
    (440, 768),
    (558, 768),

    # Gemstone Tones
    (710, 768),
    (830, 768),
    (950, 768),

    # Iridescent Finishes
    (1099, 768),
    (1218, 768),
    (1334, 768),
    (1450, 768),
    (1566, 768),
]

def remove_embedded_symbol(tile):
    pixels = np.array(tile.convert("RGB"))
    height, width = pixels.shape[:2]

    gray = cv2.cvtColor(
        pixels,
        cv2.COLOR_RGB2GRAY
    )

    # Restrict detection to the symbol area.
    region = np.zeros(
        (height, width),
        dtype=np.uint8
    )

    cv2.ellipse(
        region,
        (width // 2, height // 2),
        (int(width * 0.36), int(height * 0.36)),
        0,
        0,
        360,
        255,
        -1
    )

    # Detect narrow bright outlines.
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (15, 15)
    )

    bright = cv2.morphologyEx(
        gray,
        cv2.MORPH_TOPHAT,
        kernel
    )

    _, mask = cv2.threshold(
        bright,
        22,
        255,
        cv2.THRESH_BINARY
    )

    mask = cv2.bitwise_and(mask, region)

    # Include the immediate outline and shadow.
    shadow_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (9, 9)
    )

    mask = cv2.dilate(
        mask,
        shadow_kernel,
        iterations=1
    )

    mask = cv2.bitwise_and(mask, region)

    # Reconstruct only the detected areas.
    restored = cv2.inpaint(
        pixels,
        mask,
        4,
        cv2.INPAINT_TELEA
    )

    # Preserve every pixel outside the mask.
    result = pixels.copy()
    result[mask > 0] = restored[mask > 0]

    return Image.fromarray(
        result
    ).convert("RGBA")
def build_emblems():
    if not SOURCE.exists():
        raise FileNotFoundError(
            "Missing emblem-source.png"
        )

    source = Image.open(SOURCE).convert("RGBA")

    if source.size != (1648, 928):
        source = source.resize(
            (1648, 928),
            Image.Resampling.LANCZOS
        )

    sprite = Image.new(
        "RGBA",
        (
            COLUMNS * TILE_SIZE,
            ROWS * TILE_SIZE
        ),
        (0, 0, 0, 0)
    )

    for index, (name, center) in enumerate(
        zip(FINISHES, CENTERS)
    ):
        x, y = center
        radius = 55

        tile = source.crop((
            x - radius,
            y - radius,
            x + radius,
            y + radius
        ))

        tile = tile.resize(
            (TILE_SIZE, TILE_SIZE),
            Image.Resampling.LANCZOS
        )
        tile = remove_embedded_symbol(tile)
        column = index % COLUMNS
        row = index // COLUMNS

        sprite.paste(
            tile,
            (
                column * TILE_SIZE,
                row * TILE_SIZE
            )
        )

        print(f"Prepared: {name}")

    sprite.save(
        OUTPUT,
        format="WEBP",
        lossless=True,
        method=6
    )

    print(f"Created: {OUTPUT.name}")
    print(f"Dimensions: {sprite.size}")
    print(f"Total finishes: {len(FINISHES)}")


if __name__ == "__main__":
    build_emblems()
