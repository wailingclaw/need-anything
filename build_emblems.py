from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent

SOURCE = ROOT / "emblem-final.png"
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

    # Neon Brights
    "acid-lime",
    "hot-pink",
    "neon-orange",
    "ultraviolet",
    "cyan-glow",
]


def build_emblems():
    if not SOURCE.exists():
        raise FileNotFoundError(
            "Missing emblem-final.png"
        )

    source = Image.open(SOURCE).convert("RGBA")

    width, height = source.size

    sprite = Image.new(
        "RGBA",
        (
            COLUMNS * TILE_SIZE,
            ROWS * TILE_SIZE
        ),
        (0, 0, 0, 0)
    )

    for index, name in enumerate(FINISHES):
        column = index % COLUMNS
        row = index // COLUMNS

        left = round(column * width / COLUMNS)
        top = round(row * height / ROWS)

        right = round(
            (column + 1) * width / COLUMNS
        )

        bottom = round(
            (row + 1) * height / ROWS
        )

        tile = source.crop(
            (left, top, right, bottom)
        )

        tile = tile.resize(
            (TILE_SIZE, TILE_SIZE),
            Image.Resampling.LANCZOS
        )

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
