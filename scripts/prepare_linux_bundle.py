from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _find_icon(bundle_dir: Path, script_dir: Path) -> Path | None:
    candidates = [
        bundle_dir / "resources" / "logo" / "pysar.png",
        bundle_dir / "_internal" / "resources" / "logo" / "pysar.png",
        script_dir.parent / "resources" / "logo" / "pysar.png",
    ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    matches = sorted(bundle_dir.rglob("pysar.png"))
    for match in matches:
        if match.parent.name == "logo":
            return match
    return matches[0] if matches else None


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: prepare_linux_bundle.py <dist-dir>", file=sys.stderr)
        return 1

    bundle_dir = Path(sys.argv[1]).resolve()
    binary_path = bundle_dir / "Pysar"
    icon_path = bundle_dir / "resources" / "logo" / "pysar.png"
    desktop_path = bundle_dir / "Pysar.desktop"
    script_dir = Path(__file__).resolve().parent

    if not bundle_dir.is_dir():
        print(f"Bundle directory not found: {bundle_dir}", file=sys.stderr)
        return 1
    if not binary_path.exists():
        print(f"Executable not found: {binary_path}", file=sys.stderr)
        return 1

    if not icon_path.exists():
        source_icon = _find_icon(bundle_dir, script_dir)
        if source_icon is None:
            print(f"Icon not found: {icon_path}", file=sys.stderr)
            return 1
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_icon, icon_path)
        print(f"Copied icon from {source_icon} to {icon_path}")

    desktop_path.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Version=1.0",
                "Type=Application",
                "Name=Pysar",
                "Comment=Nintendo Wii BRSAR editor",
                "Exec=./Pysar",
                "Icon=./resources/logo/pysar.png",
                "Terminal=false",
                "Categories=AudioVideo;Audio;Development;",
                "StartupNotify=true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(desktop_path, 0o755)
    print(f"Created {desktop_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

