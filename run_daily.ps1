$ErrorActionPreference = "Stop"
$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$ProjectPath\.venv\Scripts\python.exe" "$ProjectPath\manage.py" run_daily_pipeline
