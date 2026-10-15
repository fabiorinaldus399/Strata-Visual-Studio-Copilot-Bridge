<#
.SYNOPSIS
  Interactive Copilot agent on Strata, launched from inside Visual Studio's
  Terminal (Tools -> Terminal). Same agent runtime and tools as Agent (Preview),
  but without Visual Studio's completion coordinator that leaves Stop lit.

.EXAMPLE
  .\agent_here.ps1                      # interactive session in the current folder
  .\agent_here.ps1 -Dir C:\proj\app     # start in another folder
  .\agent_here.ps1 -Yolo                # auto-approve tools
#>
param(
    [string]$Dir = (Get-Location).Path,
    [string]$Bridge = "http://127.0.0.1:8081/v1",
    [string]$Model  = "qwen3.8-flash-next-iq3_s",
    [switch]$Yolo
)

$cli = "C:\Program Files\Microsoft Visual Studio\18\Community\Common7\IDE\CopilotCli\copilot.exe"
if (-not (Test-Path $cli)) { "copilot.exe not found at $cli"; exit 1 }

$env:COPILOT_PROVIDER_BASE_URL = $Bridge
$env:COPILOT_PROVIDER_API_KEY  = "strata"
$env:COPILOT_MODEL             = $Model
$env:COPILOT_PROVIDER_MAX_PROMPT_TOKENS = "128000"
$env:COPILOT_PROVIDER_MAX_OUTPUT_TOKENS = "8192"

$a = @("-C", $Dir, "--no-auto-update", "--no-remote", "--no-remote-export")
if ($Yolo) { $a += "--allow-all-tools" }

& $cli @a
