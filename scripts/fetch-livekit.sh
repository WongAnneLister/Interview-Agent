#!/usr/bin/env bash
# 准备本机开发用的 LiveKit Server，结果放在 .tools/livekit/（已在 .gitignore 中排除）。
# - Windows（Git Bash）：下载官方 release，校验官方 checksums.txt 中的 SHA-256。
# - macOS：官方 release 不提供 macOS 版（官方只通过 Homebrew 分发，旧系统没有预编译包），
#   改为按固定 tag 拉官方源码，校验 commit 哈希后用 Go 编译。
# 来源：https://github.com/livekit/livekit
set -euo pipefail

VERSION="${LIVEKIT_VERSION:-1.13.9}"
# 与 VERSION 对应的 tag commit，macOS 源码编译时校验；改 LIVEKIT_VERSION 时需同时提供 LIVEKIT_COMMIT
DEFAULT_COMMIT="6fbe7f7e648aab4b2f734d6044a416bc2cbc482c"
BASE_URL="https://github.com/livekit/livekit/releases/download/v${VERSION}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT_DIR/.tools/livekit"
mkdir -p "$TOOLS_DIR"

fetch_windows() {
  local asset="livekit_${VERSION}_windows_amd64.zip"
  local dl_dir="$TOOLS_DIR/downloads"
  mkdir -p "$dl_dir"

  echo "下载 $asset 与 checksums.txt（v${VERSION}）"
  curl -fsSL --retry 3 -o "$dl_dir/$asset" "$BASE_URL/$asset"
  curl -fsSL --retry 3 -o "$dl_dir/checksums.txt" "$BASE_URL/checksums.txt"

  local expected actual
  expected="$(grep " ${asset}\$" "$dl_dir/checksums.txt" | awk '{print $1}')"
  actual="$(sha256sum "$dl_dir/$asset" | awk '{print $1}')"
  if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
    echo "SHA-256 校验失败：expected=${expected:-<missing>} actual=$actual" >&2
    rm -f "${dl_dir:?}/${asset:?}"
    exit 1
  fi
  echo "SHA-256 校验通过：$actual"

  # 用 Python 标准库解压（Git Bash 不一定带 unzip），只取 livekit-server.exe
  python -X utf8 - "$dl_dir/$asset" "$TOOLS_DIR" <<'PY'
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
}

build_macos() {
  local commit="${LIVEKIT_COMMIT:-}"
  if [ -z "$commit" ]; then
    if [ "$VERSION" != "1.13.9" ]; then
      echo "修改了 LIVEKIT_VERSION 时需同时设置 LIVEKIT_COMMIT（该 tag 对应的完整 commit 哈希）" >&2
      exit 1
    fi
    commit="$DEFAULT_COMMIT"
  fi

  for cmd in git go; do
    if ! command -v "$cmd" >/dev/null; then
      echo "缺少 $cmd：macOS 上需要从源码编译 LiveKit Server（Go 任意 1.21+ 版本即可，会自动下载所需工具链）" >&2
      exit 1
    fi
  done
  if ! xcode-select -p >/dev/null 2>&1; then
    echo "缺少 C 编译器：请先运行 xcode-select --install（macOS 上编译需要 cgo）" >&2
    exit 1
  fi

  local src_dir="$TOOLS_DIR/src"
  if [ "$(git -C "$src_dir" rev-parse HEAD 2>/dev/null || true)" != "$commit" ]; then
    rm -rf "${src_dir:?}"
    echo "拉取 LiveKit 源码 v${VERSION}"
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "v${VERSION}" \
      https://github.com/livekit/livekit.git "$src_dir"
  fi
  local actual
  actual="$(git -C "$src_dir" rev-parse HEAD)"
  if [ "$actual" != "$commit" ]; then
    echo "commit 校验失败：expected=$commit actual=$actual" >&2
    exit 1
  fi
  echo "commit 校验通过：$actual"

  # Go 工具链版本以官方 Dockerfile 为准（官方 CI 也从这里读取）
  local go_version
  go_version="$(sed -n 's/^FROM golang:\([0-9.]*\)-.*/\1/p' "$src_dir/Dockerfile" | head -1)"
  if [ -z "$go_version" ]; then
    echo "无法从 $src_dir/Dockerfile 读取 Go 版本" >&2
    exit 1
  fi

  # macOS 上 go-osstat 读取 CPU 统计依赖 cgo，必须 CGO_ENABLED=1（官方 Linux 构建用的是 0）
  echo "编译 livekit-server（go${go_version}，首次需下载依赖，耗时几分钟）"
  (cd "$src_dir" && GOTOOLCHAIN="go${go_version}" CGO_ENABLED=1 \
    go build -trimpath -o "$TOOLS_DIR/livekit-server" ./cmd/server)

  "$TOOLS_DIR/livekit-server" --version
}

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) fetch_windows ;;
  Darwin) build_macos ;;
  *)
    echo "暂不支持当前系统：$(uname -s)（目前支持 Windows Git Bash 与 macOS）" >&2
    exit 1
    ;;
esac
