#!/usr/bin/env bash
# 下载并校验 LiveKit Server 官方 Windows 版（开发用，在本机运行）。
# 来源：https://github.com/livekit/livekit/releases ；校验官方 checksums.txt 中的 SHA-256。
# 结果放在 .tools/livekit/（已在 .gitignore 中排除）。
set -euo pipefail

VERSION="${LIVEKIT_VERSION:-1.13.9}"
ASSET="livekit_${VERSION}_windows_amd64.zip"
BASE_URL="https://github.com/livekit/livekit/releases/download/v${VERSION}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT_DIR/.tools/livekit"
DL_DIR="$TOOLS_DIR/downloads"
mkdir -p "$DL_DIR"

echo "下载 $ASSET 与 checksums.txt（v${VERSION}）"
curl -fsSL --retry 3 -o "$DL_DIR/$ASSET" "$BASE_URL/$ASSET"
curl -fsSL --retry 3 -o "$DL_DIR/checksums.txt" "$BASE_URL/checksums.txt"

expected="$(grep " ${ASSET}\$" "$DL_DIR/checksums.txt" | awk '{print $1}')"
actual="$(sha256sum "$DL_DIR/$ASSET" | awk '{print $1}')"
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "SHA-256 校验失败：expected=${expected:-<missing>} actual=$actual" >&2
  rm -f "$DL_DIR/$ASSET"
  exit 1
fi
echo "SHA-256 校验通过：$actual"

# 用 Python 标准库解压（Git Bash 不一定带 unzip），只取 livekit-server.exe
python -X utf8 - "$DL_DIR/$ASSET" "$TOOLS_DIR" <<'PY'
import sys, zipfile, pathlib
zip_path, out_dir = sys.argv[1], pathlib.Path(sys.argv[2])
with zipfile.ZipFile(zip_path) as z:
    names = [n for n in z.namelist() if n.endswith("livekit-server.exe")]
    if len(names) != 1:
        sys.exit(f"压缩包中找不到唯一的 livekit-server.exe：{z.namelist()}")
    (out_dir / "livekit-server.exe").write_bytes(z.read(names[0]))
print("已解压到", out_dir / "livekit-server.exe")
PY

"$TOOLS_DIR/livekit-server.exe" --version
