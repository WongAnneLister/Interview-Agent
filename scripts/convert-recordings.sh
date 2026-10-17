#!/usr/bin/env bash
# 把实测用的原始录音（backend/tools/benchmark/data/raw/，m4a / mp3 / wav 等）转成识别实测要求的
# 16 kHz、单声道、16 bit wav，输出到 backend/tools/benchmark/data/asr/<同名>.wav。
# - 已经转换过的跳过；加 FORCE=1 重新转换全部。
# - macOS 用系统自带的 afconvert，其他系统用 ffmpeg。
# - 转换后检查每条录音是否有同名 .txt 参考文本，没有的列出来（实测时缺参考文本会报错）。
# 两个目录都在 .gitignore 中，录音和参考文本不会被提交。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RAW_DIR="$ROOT_DIR/backend/tools/benchmark/data/raw"
ASR_DIR="$ROOT_DIR/backend/tools/benchmark/data/asr"
mkdir -p "$RAW_DIR" "$ASR_DIR"

if command -v afconvert >/dev/null 2>&1; then
  convert() { afconvert -f WAVE -d LEI16@16000 -c 1 "$1" "$2"; }
elif command -v ffmpeg >/dev/null 2>&1; then
  convert() { ffmpeg -loglevel error -y -i "$1" -ar 16000 -ac 1 -sample_fmt s16 "$2"; }
else
  echo "需要 afconvert（macOS 自带）或 ffmpeg" >&2
  exit 1
fi

shopt -s nullglob nocaseglob
converted=0
skipped=0
for src in "$RAW_DIR"/*.{m4a,mp3,wav,aac,caf,flac}; do
  name="$(basename "${src%.*}")"
  dst="$ASR_DIR/$name.wav"
  if [[ -f "$dst" && "${FORCE:-0}" != "1" ]]; then
    skipped=$((skipped + 1))
    continue
  fi
  convert "$src" "$dst"
  echo "已转换 $(basename "$src") → asr/$name.wav"
  converted=$((converted + 1))
done
echo "转换 $converted 条，跳过已存在的 $skipped 条（FORCE=1 可重新转换）"

missing=()
for wav in "$ASR_DIR"/*.wav; do
  [[ -f "${wav%.wav}.txt" ]] || missing+=("$(basename "${wav%.wav}").txt")
done
if ((${#missing[@]})); then
  echo "以下录音还缺参考文本（逐字写下实际说的话，放在 data/asr/ 下）：${missing[*]}"
fi
