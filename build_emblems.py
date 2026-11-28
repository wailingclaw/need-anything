from pathlib import Path
from PIL import Image

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


def build_emblems():
    if not SOURCE.exists():
        raise FileNotFoundError(
            "Missing emblem-source.png"
        )

    source = Image.open(SOURCE).convert("RGBA")

    if source.size != (1648, 928):
        raise ValueError(
            f"Unexpected source dimensions: {source.size}"
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
