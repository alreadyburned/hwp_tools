#!/bin/sh
# Check the tools hwp_tools needs, then build the .vsix and install it into VS Code.
# Stops before building if anything is missing, so a half-working install is never left behind.
#
# Usage: sh scripts/install.sh
#   PYTHON=/path/to/python3   use this Python instead of searching python3, python
#   CODE=codium               use this VS Code CLI instead of searching code, code-insiders, codium

set -u
cd "$(dirname "$0")/.." || exit 1

missing=""
problem() {
  missing="$missing
  - $1"
}

# Python 3.10+ with a working venv (Debian/Ubuntu ship venv/ensurepip in a separate package).
PY=""
for c in ${PYTHON:-python3 python}; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PY=$c
    break
  fi
done
if [ -z "$PY" ]; then
  problem "Python 3.10 이상 (찾은 명령: ${PYTHON:-python3, python})
      Debian/Ubuntu: sudo apt install python3 python3-venv
      Fedora: sudo dnf install python3   macOS: brew install python
      다른 위치의 Python은 PYTHON=/경로/python3 sh scripts/install.sh"
else
  tmp=$(mktemp -d)
  if ! "$PY" -m venv "$tmp/venv" >/dev/null 2>&1; then
    problem "Python 가상환경 생성 ($PY -m venv 실패)
      Debian/Ubuntu: sudo apt install python3-venv"
  fi
  rm -rf "$tmp"
fi

# VS Code command-line tool, new enough for package.json's engines.vscode.
CODE_CLI=""
for c in ${CODE:-code code-insiders codium}; do
  if command -v "$c" >/dev/null 2>&1; then
    CODE_CLI=$c
    break
  fi
done
need=$(sed -n 's/.*"vscode": *"^\{0,1\}\([0-9][0-9.]*\)".*/\1/p' package.json)
if [ -z "$CODE_CLI" ]; then
  problem "VS Code 명령줄 도구 (찾은 명령: ${CODE:-code, code-insiders, codium})
      VS Code를 설치하고 명령 팔레트 → 'Shell Command: Install 'code' command in PATH'
      다른 이름이면 CODE=명령 sh scripts/install.sh"
else
  have=$("$CODE_CLI" --version 2>/dev/null | head -n 1)
  if [ -z "$have" ] || ! printf '%s %s\n' "$have" "$need" | awk '{
      split($1, h, "."); split($2, n, ".")
      exit !(h[1] > n[1] || (h[1] == n[1] && h[2] >= n[2]))
    }'; then
    problem "VS Code $need 이상 ($CODE_CLI --version: ${have:-알 수 없음})"
  fi
fi

if [ -n "$missing" ]; then
  echo "hwp_tools 설치 실패 - 필요한 도구가 없습니다:$missing" >&2
  exit 1
fi

echo "Python: $(command -v "$PY") ($("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))"
echo "VS Code: $(command -v "$CODE_CLI") ($have)"

vsix=$("$PY" -c 'import json; p = json.load(open("package.json", encoding="utf-8")); print("dist/%s-%s.vsix" % (p["name"], p["version"]))') || exit 1
"$PY" scripts/build_vsix.py || exit 1
"$CODE_CLI" --install-extension "$vsix" --force || exit 1
echo "설치 완료: VS Code를 다시 시작하면 첫 실행 때 Python 환경(~/.hwp-mcp/venv)을 만듭니다."
