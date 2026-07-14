$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (!(Test-Path ".env")) { Copy-Item ".env.example" ".env" }

# 가상환경(.venv)이 있으면 사용, 없으면 현재 python 사용
if (Test-Path ".\.venv\Scripts\python.exe") {
    $py = ".\.venv\Scripts\python.exe"
} else {
    $py = "python"
}
& $py -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
