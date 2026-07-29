[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$target = if ($env:TRADINGLAB_KEYS_FILE) {
    [Environment]::ExpandEnvironmentVariables($env:TRADINGLAB_KEYS_FILE)
} else {
    Join-Path $HOME ".config\tradinglab\tradinglab.keys"
}

function Read-SecretValue([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Set-UserOnlyAcl([string]$Path) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object -TypeName System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
        $identity,
        "FullControl",
        "Allow"
    )
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$values = [ordered]@{
    ALPHA_VANTAGE_API_KEY = Read-SecretValue "Alpha Vantage API key"
    SEC_USER_AGENT = Read-SecretValue "SEC User-Agent (application + contact email)"
    GOOGLE_API_KEY = Read-SecretValue "Google/Gemini API key"
    DEEPSEEK_API_KEY = Read-SecretValue "DeepSeek API key"
    TRADINGLAB_API_TOKEN = ([Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

foreach ($name in $values.Keys) {
    if ([string]::IsNullOrWhiteSpace([string]$values[$name])) {
        throw "$name cannot be empty"
    }
}

$directory = Split-Path -Parent $target
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$temporary = "$target.tmp.$([Guid]::NewGuid().ToString('N'))"
$content = @(
    "ALPHA_VANTAGE_API_KEY=$($values.ALPHA_VANTAGE_API_KEY)"
    "SEC_USER_AGENT=$($values.SEC_USER_AGENT)"
    "GOOGLE_API_KEY=$($values.GOOGLE_API_KEY)"
    "DEEPSEEK_API_KEY=$($values.DEEPSEEK_API_KEY)"
    "DEEPSEEK_BASE_URL=https://api.deepseek.com"
    "DEEPSEEK_MODEL=deepseek-v4-flash"
    "TRADINGLAB_API_TOKEN=$($values.TRADINGLAB_API_TOKEN)"
) -join [Environment]::NewLine

[IO.File]::WriteAllText($temporary, $content, (New-Object Text.UTF8Encoding($false)))
Set-UserOnlyAcl $temporary
Move-Item -LiteralPath $temporary -Destination $target -Force
Set-UserOnlyAcl $target

Write-Output "saved: $target"
Write-Output "Windows ACL: current user only"
Write-Output "The file is outside the repository and values were not printed."
