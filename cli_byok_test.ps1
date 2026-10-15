<#
.SYNOPSIS
  Runs the *same* Copilot CLI that Visual Studio 2026 drives inside Agent (Preview)
  (Common7\IDE\CopilotCli\copilot.exe) against the local Strata bridge, outside VS.

.DESCRIPTION
  This isolates the agent runtime from Visual Studio's UI/completion coordinator.
  Exit code 0 + "Duration/Tokens" summary = the CLI finished the turn normally.
  If this works but VS keeps the Stop button lit, the stall is in VS
  (CopilotCliRuntimeCompletionCoordinator), not in Strata or the bridge.

.EXAMPLE
  .\cli_byok_test.ps1                                  # trivial turn, no tools
  .\cli_byok_test.ps1 -Prompt "refactor X" -Tools      # agent loop with shell tools
  .\cli_byok_test.ps1 -ModelId gpt-4.1                 # well-known id -> model-specific config
#>
param(
    [string]$Prompt = "Rispondi esattamente con la parola: PONZANO",
    [string]$Bridge = "http://127.0.0.1:8081/v1",
    [string]$Model  = "qwen3.8-flash-next-iq3_s",
    # Setting a well-known model id makes the agent use that model's tool support
    # and token limits instead of safe defaults (see: copilot help providers).
    [string]$ModelId,
    [switch]$Tools,
    [string]$WorkDir = "$env:TEMP\opencode\cli-test"
)

$cli = "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CopilotCli\copilot.exe"
if (-not (Test-Path $cli)) { "copilot.exe not found at $cli"; exit 1 }

$env:COPILOT_PROVIDER_BASE_URL = $Bridge
$env:COPILOT_PROVIDER_API_KEY  = "strata"
$env:COPILOT_MODEL             = $Model
if ($ModelId) {
    $env:COPILOT_PROVIDER_MODEL_ID   = $ModelId
    $env:COPILOT_PROVIDER_WIRE_MODEL = $Model
}
$env:COPILOT_PROVIDER_MAX_PROMPT_TOKENS = "128000"
$env:COPILOT_PROVIDER_MAX_OUTPUT_TOKENS = "8192"

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$logDir = "$env:TEMP\opencode\cli-logs"

$cliArgs = @("-C", $WorkDir, "-p", $Prompt,
             "--no-auto-update", "--no-remote", "--no-remote-export",
             "--no-custom-instructions", "--output-format", "text",
             "--log-dir", $logDir, "--log-level", "info")
if ($Tools) { $cliArgs += "--allow-all-tools" }

"cli    : $cli"
"bridge : $Bridge   model: $Model" + $(if ($ModelId) { "   (model id: $ModelId)" })
"prompt : $Prompt" + $(if ($Tools) { "   [tools allowed]" })
"----"
& $cli @cliArgs 2>&1 | ForEach-Object { "$_" }
"----"
"EXITCODE=$LASTEXITCODE"
if ($LASTEXITCODE -eq 0) {
    "the CLI closed the turn by itself -> runtime + bridge + Strata are fine."
} else {
    "the CLI did NOT close the turn -> reproduce with --log-level debug in $logDir"
}
