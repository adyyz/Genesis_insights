# All CLI commands use the pinned Genesis dependency and project-local emulator cache.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
# The parent editor sets DEBUG=release; Firebase interprets any DEBUG as verbose
# logging and can print a subprocess environment. Disable it in this child process.
$env:DEBUG = $null
$env:FIREBASE_EMULATORS_PATH = Join-Path $projectRoot '.cache/firebase/emulators'
$javaRoot = Join-Path $projectRoot '.tools/java'
$javaHome = Get-ChildItem -LiteralPath $javaRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin/java.exe') } |
    Select-Object -First 1
if ($javaHome) {
    $env:JAVA_HOME = $javaHome.FullName
    $env:PATH = (Join-Path $javaHome.FullName 'bin') + [IO.Path]::PathSeparator + $env:PATH
}
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if ($nodeCommand) { $nodePath = $nodeCommand.Source }
elseif (Test-Path -LiteralPath 'C:/Program Files/nodejs/node.exe') { $nodePath = 'C:/Program Files/nodejs/node.exe' }
else { throw 'Node.js is required.' }
& $nodePath (Join-Path $projectRoot 'node_modules/firebase-tools/lib/bin/firebase.js') @args
exit $LASTEXITCODE
