$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

& (Join-Path $PSScriptRoot 'MbAutoSign.exe') @args
exit $LASTEXITCODE
