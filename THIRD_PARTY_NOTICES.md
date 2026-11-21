# Third-Party Notices

CordInsight's own source code is licensed under the GNU General Public License,
version 3 (see [LICENSE](LICENSE)).

The Windows executable built by `scripts/build_exe.py` also bundles third-party
software. This file covers the two builds of **FFmpeg** it contains. Each is a
separate component under its own license.

## 1. FFmpeg executable (video conversion)

| | |
|---|---|
| **Used for** | Converting video formats that OpenCV cannot read reliably (.mts, .m2ts, .mxf, .vob, .ts, .m2v) to MP4. CordInsight runs it as a separate program. |
| **Comes from** | The [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) Python package (BSD-2-Clause), which ships a prebuilt FFmpeg executable. |
| **Build** | As of imageio-ffmpeg 0.8.1: FFmpeg 9.0.2 "essentials" build by [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), configured with `--enable-gpl --enable-version3` (includes GPL components such as libx264). |
| **License** | GNU General Public License, version 3 or later. Full text: [LICENSE](LICENSE) or <https://www.gnu.org/licenses/gpl-3.0.html> |
| **Source code** | FFmpeg 9.0.2 source: <https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz>. Build information from the builder: <https://www.gyan.dev/ffmpeg/builds/> |

## 2. FFmpeg library inside OpenCV (video reading)

| | |
|---|---|
| **Used for** | Reading video frames. Shipped inside the [opencv-python](https://github.com/opencv/opencv-python) package (OpenCV itself is Apache-2.0) as `opencv_videoio_ffmpeg*.dll`. |
| **License** | GNU Lesser General Public License, version 2.1, as stated by the opencv-python project. Full text: <https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html> |
| **Source code** | Project and build information: <https://github.com/opencv/opencv-python>. The package's full third-party license list is in its `LICENSE-3RD-PARTY.txt`. |

## Keeping this file current

The bundled FFmpeg versions depend on the imageio-ffmpeg and opencv-python
versions installed when the executable is built (see `requirements.txt`). If
those change, re-check the versions and licenses above.
