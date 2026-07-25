#!/usr/bin/env python3
"""
Build script for creating CordInsight Windows executable.

Usage:
    python build_exe.py
"""

import subprocess
import sys
import os
from pathlib import Path


def convert_png_to_ico(png_path: Path, ico_path: Path) -> bool:
    """
    Convert PNG image to ICO format for Windows executable icon.
    Creates multiple sizes for best display at different resolutions.
    """
    try:
        from PIL import Image

        print(f"Converting {png_path.name} to {ico_path.name}...")

        # Open the PNG image
        img = Image.open(png_path)

        # Convert to RGBA if necessary
        if img.mode != 'RGBA':
            img = img.convert('RGBA')

        # Create multiple sizes for the ICO file
        # Windows uses different sizes for different contexts
        sizes = [16, 32, 48, 64, 128, 256]
        icon_images = []

        for size in sizes:
            # Resize with high-quality resampling
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            icon_images.append(resized)

        # Save as ICO with multiple sizes
        icon_images[0].save(
            ico_path,
            format='ICO',
            sizes=[(s, s) for s in sizes],
            append_images=icon_images[1:]
        )

        print(f"  Created: {ico_path.name} with sizes {sizes}")
        return True

    except ImportError:
        print("WARNING: Pillow not installed, cannot convert icon")
        return False
    except Exception as e:
        print(f"WARNING: Could not convert icon: {e}")
        return False


def main():
    print("=" * 60)
    print("CordInsight - Windows Build Script")
    print("=" * 60)
    print()

    # Run from the repository root (this script lives in scripts/).
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)

    # Check if pyinstaller is available
    try:
        import PyInstaller
        print(f"PyInstaller version: {PyInstaller.__version__}")
    except ImportError:
        print("PyInstaller not found. Installing...")
        # Bounded to match requirements.txt so a surprise major release can't
        # change the build toolchain out from under us.
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "pyinstaller>=5.0.0,<8"])

    main_script = "cordinsight.py"

    if not Path(main_script).exists():
        print(f"ERROR: {main_script} not found!")
        return 1

    # Handle icon conversion (assets live in assets/)
    icon_png = Path("assets/icon.png")
    icon_ico = Path("assets/icon.ico")
    icon_arg = None

    if icon_png.exists():
        # Convert PNG to ICO
        if convert_png_to_ico(icon_png, icon_ico):
            icon_arg = str(icon_ico)
    elif icon_ico.exists():
        # Use existing ICO file
        icon_arg = str(icon_ico)
        print(f"Using existing icon: {icon_ico.name}")

    # Build command
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "CordInsight",
        "--clean",
        "--hidden-import", "cv2",
        "--hidden-import", "numpy",
        "--hidden-import", "pandas",
        "--hidden-import", "openpyxl",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageTk",
        "--hidden-import", "PIL._tkinter_finder",
        # imageio-ffmpeg provides the bundled ffmpeg used to convert MTS/M2TS, etc.
        # --collect-binaries pulls its ffmpeg executable into the build.
        "--hidden-import", "imageio_ffmpeg",
        "--collect-binaries", "imageio_ffmpeg",
    ]

    # Add icon if available
    if icon_arg:
        cmd.extend(["--icon", icon_arg])
        print(f"Using exe icon: {icon_arg}")
    else:
        print("No icon found, building without custom icon")

    # Determine path separator for --add-data (Windows uses ;, Linux/Mac uses :)
    sep = ";" if sys.platform == "win32" else ":"

    # Bundle the assets/ folder (logos + window icon) into the exe under "assets",
    # matching get_resource_path("assets/...") at runtime.
    assets_folder = Path("assets")
    if assets_folder.exists() and assets_folder.is_dir():
        cmd.extend(["--add-data", f"{assets_folder}{sep}assets"])
        print(f"Bundling assets from: {assets_folder}")

    # Add main script
    cmd.append(main_script)

    print()
    print("Building...")
    print()

    try:
        subprocess.check_call(cmd)

        print()
        print("=" * 60)
        print("BUILD SUCCESSFUL!")
        print("=" * 60)

        dist_path = Path("dist")
        if dist_path.exists():
            for f in dist_path.iterdir():
                size_mb = f.stat().st_size / (1024 * 1024)
                print(f"  Created: {f.name} ({size_mb:.1f} MB)")

        print()
        print("The executable is in the 'dist' folder.")

        # Clean up generated ICO file (optional - comment out to keep it)
        # if icon_ico.exists() and icon_png.exists():
        #     icon_ico.unlink()

        return 0

    except subprocess.CalledProcessError as e:
        print(f"\nBuild failed with error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
