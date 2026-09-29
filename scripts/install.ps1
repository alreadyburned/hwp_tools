# Check the tools hwp_tools needs, then build the .vsix and install it into VS Code.
# Stops before building if anything is missing, so a half-working install is never left behind.
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   $env:PYTHON = 'C:\path\python.exe'   use this Python instead of searching py -3, python, python3
#   $env:CODE = 'codium'                  use this VS Code CLI instead of searching code, code-insiders, codium

Set-Location (Split-Path -Parent $PSScriptRoot)

$missing = @()

# Python 3.10+ with a working venv. The Microsoft Store "python" alias exits without output, so it fails the check.
if ($env:PYTHON) { $candidates = @(, @($env:PYTHON)) } else { $candidates = @(@('py', '-3'), @('python'), @('python3')) }
$py = $null
foreach ($c in $candidates) {
    if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $pre = @($c | Select-Object -Skip 1)
    & $c[0] @pre -c 'import sys; sys.exit(sys.version_info < (3, 10))' *> $null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) {
    $tried = if ($env:PYTHON) { $env:PYTHON } else { 'py -3, python, python3' }
    $missing += "Python 3.10 이상 (찾은 명령: $tried)`n      https://www.python.org/downloads/ 또는 winget install Python.Python.3.12`n      다른 위치의 Python은 `$env:PYTHON = 'C:\경로\python.exe' 후 다시 실행"
} else {
    $pyPre = @($py | Select-Object -Skip 1)
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('hwp-tools-check-' + [guid]::NewGuid())
    & $py[0] @pyPre -m venv (Join-Path $tmp 'venv') *> $null
    if ($LASTEXITCODE -ne 0) { $missing += "Python 가상환경 생성 ($($py -join ' ') -m venv 실패)" }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

# VS Code command-line tool, new enough for package.json's engines.vscode.
$pkg = Get-Content -Raw -Encoding UTF8 package.json | ConvertFrom-Json
$need = [version]($pkg.engines.vscode.TrimStart('^'))
$codeNames = if ($env:CODE) { @($env:CODE) } else { @('code', 'code-insiders', 'codium') }
$code = $null
foreach ($c in $codeNames) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $code = $c; break }
}
if (-not $code) {
    $missing += "VS Code 명령줄 도구 (찾은 명령: $($codeNames -join ', '))`n      VS Code 설치 시 'PATH에 추가'를 선택하거나 `$env:CODE로 지정"
} else {
    $have = (& $code --version 2>$null | Select-Object -First 1)
    $ok = $false
    try { $ok = [version]$have -ge $need } catch { }
    if (-not $ok) { $missing += "VS Code $need 이상 ($code --version: $have)" }
}

if ($missing.Count -gt 0) {
    [Console]::Error.WriteLine("hwp_tools 설치 실패 - 필요한 도구가 없습니다:")
    foreach ($m in $missing) { [Console]::Error.WriteLine("  - $m") }
    exit 1
}

Write-Host "Python: $($py -join ' ')"
Write-Host "VS Code: $code ($have)"

& $py[0] @pyPre scripts/build_vsix.py
if ($LASTEXITCODE -ne 0) { exit 1 }
& $code --install-extension "dist/$($pkg.name)-$($pkg.version).vsix" --force
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Host "설치 완료: VS Code를 다시 시작하면 첫 실행 때 Python 환경(~/.hwp-mcp/venv)을 만듭니다."
