"""アプリアイコン（.ico / .icns）を Web UI のアイコンから生成する。

    python dictation/make_icons.py

生成物（`icon.ico` / `icon.icns`）はリポジトリに含めてあるので、通常は実行不要。
アイコンの元絵を差し替えたときだけ回す。

.icns は macOS の `iconutil` を使わずに自前で書き出す（Linux/Windows 上でも生成できるように）。
フォーマットは「'icns' + 全体長 + (4byte型 + 4byte長 + PNG データ) の並び」という単純な構造。
"""

import struct
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "server" / "static" / "icons" / "icon-512.png"

#: ICNS のチャンク型 → 一辺のピクセル数（PNG を格納できる型のみ使う）
ICNS_TYPES = {
    "ic11": 32,  # 16pt @2x
    "ic12": 64,  # 32pt @2x
    "ic07": 128,
    "ic13": 256,  # 128pt @2x
    "ic08": 256,
    "ic14": 512,  # 256pt @2x
    "ic09": 512,
    "ic10": 1024,  # 512pt @2x
}

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _png(img: Image.Image, size: int) -> bytes:
    buf = BytesIO()
    img.resize((size, size), Image.Resampling.LANCZOS).save(buf, format="PNG")
    return buf.getvalue()


def build_icns(img: Image.Image) -> bytes:
    chunks = b""
    for kind, size in ICNS_TYPES.items():
        data = _png(img, size)
        chunks += kind.encode("ascii") + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(chunks) + 8) + chunks


def main() -> None:
    img = Image.open(SOURCE).convert("RGBA")
    ico = HERE / "icon.ico"
    icns = HERE / "icon.icns"
    img.save(ico, format="ICO", sizes=ICO_SIZES)
    icns.write_bytes(build_icns(img))
    print(f"{ico.name}: {ico.stat().st_size:,} bytes")
    print(f"{icns.name}: {icns.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
