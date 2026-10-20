#!/bin/sh
# 把 Cortech Chat 小红书助手打成 zip，便于发给同事加载。
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
src="$root/cortech-xhs-assist"
out="$root/cortech-xhs-assist.zip"
rm -f "$out"
(
  cd "$src"
  zip -r "$out" . -x '*.zip' -x '__MACOSX/*' -x '.DS_Store'
)
echo "已生成 $out"
