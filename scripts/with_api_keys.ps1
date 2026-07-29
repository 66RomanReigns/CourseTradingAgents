$ErrorActionPreference = "Stop"
$CommandLine = @($MyInvocation.UnboundArguments)
$keysFile = if ($env:TRADINGLAB_KEYS_FILE) {
    [Environment]::ExpandEnvironmentVariables($env:TRADINGLAB_KEYS_FILE)
} else {
    Join-Path $HOME ".config\tradinglab\tradinglab.keys"
}

if (-not (Test-Path -LiteralPath $keysFile -PathType Leaf)) {
    throw "API key file not found: $keysFile; run scripts/configure_api_keys.ps1"
}

$acl = Get-Acl -LiteralPath $keysFile
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$allowedIdentities = @(
    $identity.ToLowerInvariant(),
    "nt authority\system",
    "builtin\administrators"
)
$allowed = $false
foreach ($rule in $acl.Access) {
    $ruleIdentity = $rule.IdentityReference.Value.ToLowerInvariant()
    if ($rule.AccessControlType -ne "Allow" -or $ruleIdentity -notin $allowedIdentities) {
        throw "API key file ACL grants access to an unsupported identity"
    }
    if ($ruleIdentity -eq $identity.ToLowerInvariant() -and
        ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne 0) {
        $allowed = $true
    }
}
if (-not $allowed) {
    throw "API key file ACL does not grant the current user full control"
}

$required = @(
    "ALPHA_VANTAGE_API_KEY",
    "SEC_USER_AGENT",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "TRADINGLAB_API_TOKEN"
)
$configured = @{}
foreach ($raw in [IO.File]::ReadAllLines($keysFile)) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith("#") -or $line.IndexOf("=") -lt 1) { continue }
    $separator = $line.IndexOf("=")
    $name = $line.Substring(0, $separator).Trim()
    $value = $line.Substring($separator + 1).Trim()
    if ($value.Length -ge 2) {
        $first = $value[0]
        $last = $value[$value.Length - 1]
        if (($first -eq [char]39 -and $last -eq [char]39) -or
            ($first -eq [char]34 -and $last -eq [char]34)) {
            $value = $value.Substring(1, $value.Length - 2)
        }
    }
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "invalid variable name in key file" }
    if ([string]::IsNullOrWhiteSpace($value)) { throw "empty value for $name" }
    $configured[$name] = $value
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}
foreach ($name in $required) {
    if (-not $configured.ContainsKey($name)) { throw "missing $name in $keysFile" }
}

if (-not $configured.ContainsKey("ZHIPU_BASE_URL")) {
    $env:ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
}
if (-not $configured.ContainsKey("ZHIPU_MODEL")) {
    $env:ZHIPU_MODEL = "glm-4.7-flash"
}
if (-not $configured.ContainsKey("DEEPSEEK_BASE_URL")) {
    $env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
}
if (-not $configured.ContainsKey("DEEPSEEK_MODEL")) {
    $env:DEEPSEEK_MODEL = "deepseek-v4-flash"
}

if (-not $CommandLine -or $CommandLine.Count -eq 0) {
    Write-Output "API keys loaded from $keysFile"
    Write-Output ("available variables: " + ($required -join " "))
    exit 0
}

$command = $CommandLine[0]
$arguments = @()
for ($index = 1; $index -lt $CommandLine.Count; $index++) {
    $arguments += [string]$CommandLine[$index]
}
& $command @arguments
exit $LASTEXITCODE
