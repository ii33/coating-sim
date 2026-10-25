[CmdletBinding()]
param(
    [string]$RepoPath = '.',
    [string]$TargetSha,
    [string]$ExpectedVersion,
    [switch]$Push,
    [switch]$DryRun,
    [switch]$UseCnbEnvironmentCredential,
    [switch]$SelfTest
)

# No credential values, machine identity or local paths belong in this script's output.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$script:GithubUrl = 'https://github.com/ii33/coating-sim.git'
$script:CnbUrl = 'https://cnb.cool/nonereason/coating-sim'
$script:ProxyUrl = 'http://127.0.0.1:10808'
$script:ResolvedRepo = $null
$script:MetadataDirectory = $null
$script:PushAttempted = $false

function Test-FullSha([string]$Value) { return $Value -cmatch '\A[0-9a-f]{40}\z' }
function Test-Version([string]$Value) {
    return $Value -cmatch '^(?:dev\.[0-9]{8}\.[0-9]{2}|v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?)\z'
}
function Get-Mode([bool]$WantPush, [bool]$WantDryRun) {
    if ($WantDryRun) { return 'dry-run' }
    if ($WantPush) { return 'push' }
    return 'read-only'
}
function Get-Decision([string]$Current, [string]$Wanted, [bool]$Ancestor) {
    if ($Current -ceq $Wanted) { return 'noop' }
    if (-not $Ancestor) { return 'stop-diverged' }
    return 'fast-forward'
}
function Get-SafeCategory([string]$RawText) {
    if ($RawText -match '(?i)authentication|could not read (?:password|username)|terminal prompts disabled|\b40[13]\b') { return 'authentication-failed' }
    if ($RawText -match '(?i)certificate|\bTLS\b|\bSSL\b|schannel') { return 'tls-failed' }
    if ($RawText -match '(?i)non-fast-forward|fetch first|rejected') { return 'remote-rejected' }
    if ($RawText -match '(?i)resolve host|resolve proxy') { return 'name-resolution-failed' }
    if ($RawText -match '(?i)connect|proxy') { return 'connection-failed' }
    return 'git-operation-failed'
}
function Test-AllowedUrl([string]$Value, [bool]$Cnb) {
    # Exact canonical literals only: even equivalent :443/.git variants fail.
    $canonical=if ($Cnb) { $script:CnbUrl } else { $script:GithubUrl }
    return $Value -ceq $canonical
}
function Test-RemoteShape([string[]]$RawFetch, [bool]$HasPushUrl, [string[]]$EffectiveFetch, [string[]]$EffectivePush, [bool]$Cnb) {
    if ($HasPushUrl -or $RawFetch.Count -ne 1 -or $EffectiveFetch.Count -ne 1 -or $EffectivePush.Count -ne 1) { return $false }
    return (Test-AllowedUrl $RawFetch[0] $Cnb) -and (Test-AllowedUrl $EffectiveFetch[0] $Cnb) -and (Test-AllowedUrl $EffectivePush[0] $Cnb)
}
function Get-NullConfigValues([string]$Value) {
    if (-not $Value.EndsWith([string][char]0)) { throw 'remote-url-policy-refused' }
    # Remove only the terminating NUL; preserve empty/multiple configured values.
    return ,($Value.Substring(0,$Value.Length-1).Split([char]0))
}
function Test-PublicReadCredentialBoundary([bool]$CredentialFileExists, [string[]]$EnvironmentNames) {
    return -not $CredentialFileExists -and -not (@($EnvironmentNames | Where-Object { $_ -ieq 'NETRC' }).Count -gt 0)
}
function Test-ClientCredentialConfigNames([string[]]$Names) {
    return @($Names | Where-Object { $_ -imatch '^http(?:\..+)?\.(?:sslcert|sslkey|proxysslcert|proxysslkey)$' }).Count -eq 0
}
function Assert-ClientCredentialConfiguration {
    # --name-only never returns values. Keys can contain scoped URLs, so neither
    # original names nor their text may be echoed or added to a run record.
    $text=Get-Checked @('config','--name-only','--null','--list')
    $names=@($text.Split([char]0) | Where-Object { -not [string]::IsNullOrEmpty($_) })
    if (-not (Test-ClientCredentialConfigNames $names)) { throw 'public-read-client-credential-config-refused' }
}
function Assert-PublicReadCredentialBoundary {
    # Names only: never obtain NETRC's value and never open credential files.
    $names=@([Environment]::GetEnvironmentVariables('Process').Keys)
    if (-not (Test-PublicReadCredentialBoundary $false $names)) { throw 'public-read-implicit-credential-file-refused' }
    $homes=@([Environment]::GetEnvironmentVariable('HOME','Process'),
             [Environment]::GetEnvironmentVariable('USERPROFILE','Process'),
             [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)) |
             Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique
    foreach ($homeDirectory in $homes) {
        foreach ($name in @('.netrc','_netrc')) {
            $credentialPath=Join-Path $homeDirectory $name
            $exists=$false
            try {
                $null=Get-Item -LiteralPath $credentialPath -Force -ErrorAction Stop
                $exists=$true
            } catch {
                if ($_.CategoryInfo.Category -ne [Management.Automation.ErrorCategory]::ObjectNotFound) {
                    throw 'public-read-implicit-credential-file-refused'
                }
            }
            if (-not (Test-PublicReadCredentialBoundary $exists $names)) { throw 'public-read-implicit-credential-file-refused' }
        }
    }
    Assert-ClientCredentialConfiguration
}
function Get-NetworkOptions([bool]$Cnb, [bool]$AllowNativeHelper = $false) {
    $url = if ($Cnb) { $script:CnbUrl } else { $script:GithubUrl }
    $route = if ($Cnb) { '' } else { $script:ProxyUrl }
    $options = @('-c','http.followRedirects=false','-c','http.sslVerify=true',
                 '-c',('http.{0}.followRedirects=false' -f $url),
                 '-c',('http.{0}.sslVerify=true' -f $url),
                 '-c',('http.proxy={0}' -f $route),'-c',('http.{0}.proxy={1}' -f $url,$route),
                 '-c','http.extraHeader=','-c',('http.{0}.extraHeader=' -f $url),
                 '-c','http.cookieFile=','-c',('http.{0}.cookieFile=' -f $url),
                 '-c','http.saveCookies=false','-c',('http.{0}.saveCookies=false' -f $url),
                 '-c','http.emptyAuth=false','-c',('http.{0}.emptyAuth=false' -f $url))
    if (-not $AllowNativeHelper) {
        $options += @('-c','http.proactiveAuth=none','-c',('http.{0}.proactiveAuth=none' -f $url))
        $options += @('-c','credential.helper=','-c',('credential.{0}.helper=' -f $url))
    }
    if ($Cnb) {
        $options += @('-c','remote.cnb.proxy=','-c','credential.username=cnb',
                      '-c',('credential.{0}.username=cnb' -f $url),
                      '-c','credential.useHttpPath=true','-c',('credential.{0}.useHttpPath=true' -f $url))
    } else {
        $options += @('-c',('remote.origin.proxy={0}' -f $script:ProxyUrl))
    }
    return $options
}
function Get-PushArguments([string]$Sha, [bool]$EnvironmentCredential) {
    if (-not (Test-FullSha $Sha)) { throw 'invalid-target-sha' }
    $options = @(Get-NetworkOptions $true (-not $EnvironmentCredential))
    return $options + @('push','--porcelain','--no-verify','--no-follow-tags','--recurse-submodules=no',$script:CnbUrl,('{0}:refs/heads/main' -f $Sha))
}
function Test-StripEnvironment([string]$Name, [bool]$AllowEnvironmentCredential) {
    if ($Name -ceq 'CNB' -and $AllowEnvironmentCredential) { return $false }
    return $Name -ieq 'NETRC' -or $Name -ieq 'CURL_HOME' -or $Name -match '(?i)^(?:GIT_|CNB|GCM_|SSH_ASKPASS)' -or $Name -match '(?i)proxy|trace'
}
function New-AskpassFiles([string]$MetadataRoot) {
    $root = [IO.Path]::GetFullPath($MetadataRoot)
    $directory = Join-Path $root ('auth-once-' + [Guid]::NewGuid().ToString('N'))
    try {
        [void][IO.Directory]::CreateDirectory($directory)
        # The helper contains a variable NAME only. The child reads its own inherited
        # value and writes it once to Git's private stdout pipe, never to a file.
        $helper = @'
    param([string]$Prompt)
    $ErrorActionPreference='Stop'
    if ($Prompt -cnotmatch "^Password for 'https://cnb@cnb\.cool/nonereason/coating-sim':\s*$") { exit 1 }
    $marker=Join-Path $PSScriptRoot 'used'
    try { $stream=[IO.File]::Open($marker,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None);$stream.Close() } catch { exit 1 }
    $value=[Environment]::GetEnvironmentVariable('CNB','Process')
    if ([string]::IsNullOrEmpty($value)) { exit 1 }
    [Console]::Out.Write($value)
    $value=$null
'@
        $windows = [Environment]::GetFolderPath([Environment+SpecialFolder]::Windows)
        $shell = Join-Path $windows 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (-not [IO.File]::Exists($shell)) { throw 'credential-helper-runtime-unavailable' }
        $wrapper = [string]::Join([Environment]::NewLine, @('@echo off',
            ('"{0}" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0askpass.ps1" "%~1"' -f $shell),
            'exit /b %errorlevel%', ''))
        [IO.File]::WriteAllText((Join-Path $directory 'askpass.ps1'),$helper,[Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path $directory 'askpass.cmd'),$wrapper,[Text.Encoding]::ASCII)
        return $directory
    } catch {
        if ([IO.Directory]::Exists($directory)) { Remove-OwnedAuthDirectory $directory $root }
        throw 'credential-helper-setup-failed'
    }

}
function Remove-OwnedAuthDirectory([string]$Directory, [string]$MetadataRoot) {
    $root = [IO.Path]::GetFullPath($MetadataRoot).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Directory)
    if ([IO.Path]::GetDirectoryName($full) -cne $root -or [IO.Path]::GetFileName($full) -cnotmatch '^auth-once-[0-9a-f]{32}$') { throw 'unsafe-helper-cleanup-refused' }
    if ([IO.Directory]::Exists($full)) {
        $item = Get-Item -LiteralPath $full -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'unsafe-helper-cleanup-refused' }
        Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
    }
}
function Get-HistorySafetyEnvironment {
    return @{ GIT_NO_REPLACE_OBJECTS='1' }
}
function Test-HistoryMetadataBoundary([bool]$Exists, [bool]$AccessFailed) {
    return -not $Exists -and -not $AccessFailed
}
function Assert-NoLegacyGrafts {
    # Let Git resolve the correct common metadata path, including linked worktrees.
    # No graft content is read, and no metadata is changed or removed.
    $gitPath=Get-Checked @('rev-parse','--git-path','info/grafts')
    if ([string]::IsNullOrWhiteSpace($gitPath)) { throw 'local-history-override-refused' }
    $path=if ([IO.Path]::IsPathRooted($gitPath)) { $gitPath } else { Join-Path $script:ResolvedRepo $gitPath }
    $exists=$false; $failed=$false
    try {
        $null=Get-Item -LiteralPath $path -Force -ErrorAction Stop
        $exists=$true
    } catch {
        if ($_.CategoryInfo.Category -ne [Management.Automation.ErrorCategory]::ObjectNotFound) { $failed=$true }
    }
    if (-not (Test-HistoryMetadataBoundary $exists $failed)) { throw 'local-history-override-refused' }
}
function Get-CommonGitOptions {
    return @('--no-replace-objects','--no-optional-locks','-c','core.longpaths=true','-c','core.fsmonitor=false',
             '-c','core.hooksPath=NUL','-c','core.askPass=',
             '-c','gc.auto=0','-c','maintenance.auto=false')
}
function Invoke-Git([string[]]$GitArgs, [bool]$EnvironmentCredential = $false, [bool]$SuppressOutput = $false) {
    $authDirectory = $null
    $process = $null
    try {
        $git = Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
        $arguments = @(Get-CommonGitOptions) + @('-C',$script:ResolvedRepo) + $GitArgs
        $quoted = foreach ($argument in $arguments) {
            if ($argument -match '["\r\n]') { throw 'unsupported-git-argument' }
            '"' + [regex]::Replace($argument,'\\+$',{param($m) $m.Value+$m.Value}) + '"'
        }
        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName=$git.Path; $start.Arguments=$quoted -join ' '
        $start.UseShellExecute=$false; $start.CreateNoWindow=$true
        $start.RedirectStandardOutput=$true; $start.RedirectStandardError=$true
        foreach ($key in @($start.EnvironmentVariables.Keys)) {
            if (Test-StripEnvironment ([string]$key) $EnvironmentCredential) { $start.EnvironmentVariables.Remove([string]$key) }
        }
        # Keep the user's existing Git configuration/credential helper available
        # only to the explicit native push. Every public network call clears
        # helpers at generic and exact-URL scope; effective URL validation uses
        # this same configuration. Never install or replace an auth mechanism.
        foreach ($trace in @('GIT_TRACE','GIT_TRACE_PACKET','GIT_TRACE_CURL','GIT_TRACE2','GIT_TRACE2_EVENT','GIT_TRACE2_PERF')) {
            $start.EnvironmentVariables[$trace]='0'
        }
        $start.EnvironmentVariables['GIT_TRACE_CURL_NO_DATA']='1'
        foreach ($entry in (Get-HistorySafetyEnvironment).GetEnumerator()) {
            $start.EnvironmentVariables[$entry.Key]=$entry.Value
        }
        $start.EnvironmentVariables['GIT_TERMINAL_PROMPT']='0'
        $start.EnvironmentVariables['GIT_OPTIONAL_LOCKS']='0'
        $start.EnvironmentVariables['GIT_NO_LAZY_FETCH']='1'
        $start.EnvironmentVariables['GCM_INTERACTIVE']='never'
        $start.EnvironmentVariables['GIT_ASKPASS']=''
        $start.EnvironmentVariables['SSH_ASKPASS']=''
        if ($EnvironmentCredential) {
            if ($script:Mode -cne 'push' -or $GitArgs -notcontains 'push' -or $GitArgs -notcontains $script:CnbUrl) { throw 'credential-scope-refused' }
            $authDirectory=New-AskpassFiles $script:MetadataDirectory
            $start.EnvironmentVariables['GIT_ASKPASS']=Join-Path $authDirectory 'askpass.cmd'
        }
        $process=[Diagnostics.Process]::new(); $process.StartInfo=$start
        [void]$process.Start()
        $stdout=$process.StandardOutput.ReadToEndAsync(); $stderr=$process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(120000)) {
            $process.Kill(); $process.WaitForExit()
            return [pscustomobject]@{Code=124;Text='';Category='operation-timeout-outcome-unverified'}
        }
        $code=$process.ExitCode
        $category=if ($code -eq 0) { 'ok' } else { Get-SafeCategory $stderr.Result }
        $text=if ($code -eq 0 -and -not $SuppressOutput) { $stdout.Result.TrimEnd([char[]]@([char]13,[char]10)) } else { '' }
        return [pscustomobject]@{Code=$code;Text=$text;Category=$category}
    } catch { return [pscustomobject]@{Code=125;Text='';Category='local-operation-failed'} }
    finally {
        if ($process) { $process.Dispose() }
        if ($authDirectory) { Remove-OwnedAuthDirectory $authDirectory $script:MetadataDirectory }
    }
}
function Get-Checked([string[]]$GitArgs) {
    $result=Invoke-Git $GitArgs
    if ($result.Code -ne 0) { throw $result.Category }
    return $result.Text
}
function Get-Lines([string]$Value) { return @($Value -split '\r?\n' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) }
function Assert-UrlList([string]$Value, [bool]$Cnb) {
    $lines=@(Get-Lines $Value)
    if ($lines.Count -ne 1 -or -not (Test-AllowedUrl $lines[0] $Cnb)) { throw 'remote-url-policy-refused' }
}
function Assert-Remote([string]$Name, [bool]$Cnb) {
    $raw=@(Get-NullConfigValues (Get-Checked @('config','--null','--get-all',('remote.{0}.url' -f $Name))))
    # Unwrap the preserved array without treating an empty entry as no entry.
    if ($raw.Count -eq 1 -and $raw[0] -is [Array]) { $raw=$raw[0] }
    $push=Invoke-Git @('config','--get-all',('remote.{0}.pushurl' -f $Name))
    if ($push.Code -ne 0 -and $push.Code -ne 1) { throw 'remote-url-policy-refused' }
    $fetch=@(Get-Lines (Get-Checked @('remote','get-url','--all',$Name)))
    $effectivePush=@(Get-Lines (Get-Checked @('remote','get-url','--push','--all',$Name)))
    if (-not (Test-RemoteShape $raw ($push.Code -eq 0) $fetch $effectivePush $Cnb)) { throw 'remote-url-policy-refused' }
    # No explicit pushurl is allowed: it would suppress pushInsteadOf for the
    # alias while a literal-URL push could still be rewritten. With exactly one
    # canonical raw URL and no pushurl, both operations share the same input and
    # both effective fetch/push URLs must remain that exact literal.
}
function Get-RemoteMain([bool]$Cnb) {
    Assert-PublicReadCredentialBoundary
    $url=if ($Cnb) { $script:CnbUrl } else { $script:GithubUrl }
    $text=Get-Checked (@(Get-NetworkOptions $Cnb) + @('ls-remote','--refs',$url,'refs/heads/main'))
    $rows=@(Get-Lines $text)
    if ($rows.Count -ne 1 -or $rows[0] -cnotmatch '^([0-9a-f]{40})\s+refs/heads/main$') { throw 'remote-main-response-invalid' }
    return $Matches[1]
}
function Test-Object([string]$Sha) {
    $result=Invoke-Git @('cat-file','-e',($Sha+'^{commit}'))
    if ($result.Code -eq 0) { return $true }
    if ($result.Code -eq 1 -or $result.Code -eq 128) { return $false }
    throw $result.Category
}
function Ensure-Object([string]$Sha, [bool]$Cnb) {
    if (Test-Object $Sha) { return }
    if ($script:Mode -cne 'push') { throw 'object-unavailable-read-only-will-not-fetch' }
    $url=if ($Cnb) { $script:CnbUrl } else { $script:GithubUrl }
    # Public reads only, even in push mode. No helper/askpass or CNB env value.
    Assert-PublicReadCredentialBoundary
    $null=Get-Checked (@(Get-NetworkOptions $Cnb) + @('fetch','--no-tags','--no-write-fetch-head','--recurse-submodules=no',$url,$Sha))
    if (-not (Test-Object $Sha)) { throw 'required-commit-unavailable' }
}
function Test-Ancestor([string]$Older, [string]$Newer) {
    $result=Invoke-Git @('merge-base','--is-ancestor',$Older,$Newer)
    if ($result.Code -eq 0) { return $true }
    if ($result.Code -eq 1) { return $false }
    throw 'ancestry-check-failed'
}
function Get-Version([string]$Sha) {
    $value=Get-Checked @('cat-file','blob',($Sha+':coat_sim/version.txt'))
    if (-not (Test-Version $value) -or $value -cne $ExpectedVersion) { throw 'version-mismatch' }
    return $value
}
function Write-Record([object]$Record, [string]$RecordPath) {
    # Fixed-schema records contain no raw process output, URLs, username or paths.
    [IO.File]::WriteAllText($RecordPath,($Record | ConvertTo-Json -Depth 4),[Text.UTF8Encoding]::new($false))
}
function Invoke-SelfTest {
    $checks=0
    if (@(Get-CommonGitOptions)[0] -cne '--no-replace-objects') { throw 'selftest-disable-replace-global-option' }; $checks++
    if ((Get-HistorySafetyEnvironment)['GIT_NO_REPLACE_OBJECTS'] -cne '1') { throw 'selftest-disable-replace-environment' }; $checks++
    if (-not (Test-HistoryMetadataBoundary $false $false)) { throw 'selftest-no-grafts' }; $checks++
    foreach ($state in @(@($true,$false),@($false,$true),@($true,$true))) {
        if (Test-HistoryMetadataBoundary $state[0] $state[1]) { throw 'selftest-refuse-grafts-or-access-error' }; $checks++
    }
    if (@(Get-CommonGitOptions) -notcontains 'core.askPass=') { throw 'selftest-no-configured-askpass' }; $checks++
    foreach ($role in @($false,$true)) {
        $good=if ($role) { $script:CnbUrl } else { $script:GithubUrl }
        if (-not (Test-AllowedUrl $good $role)) { throw 'selftest-valid-url' }; $checks++
        foreach ($bad in @($good.Replace('https:','http:'),$good.Replace('https://','https://user:secret@'),($good+'?token=secret'),($good+'#secret'),$good.Replace('.com/','.com:444/').Replace('.cool/','.cool:444/'),($good+'/../other'),$good.Replace('coating-sim','coating%2dsim'),$good.Replace('coating-sim','other'),($good+'/'),$good.Replace('https://','https://evil.invalid/'))) {
            if (Test-AllowedUrl $bad $role) { throw 'selftest-invalid-url' }; $checks++
        }
    }
    if (Test-AllowedUrl 'https://cnb.cool:443/nonereason/coating-sim' $true) { throw 'selftest-default-port-variant' }; $checks++
    if (Test-AllowedUrl $script:GithubUrl $true) { throw 'selftest-role-host' }; $checks++
    $rejected=$false
    try { Assert-UrlList ($script:CnbUrl+[char]10+$script:CnbUrl) $true } catch { $rejected=$true }
    if (-not $rejected) { throw 'selftest-url-multiplicity' }; $checks++
    foreach ($role in @($false,$true)) {
        $url=if ($role) { $script:CnbUrl } else { $script:GithubUrl }
        if (-not (Test-RemoteShape @($url) $false @($url) @($url) $role)) { throw 'selftest-canonical-shape' }; $checks++
        if (Test-RemoteShape @($url) $true @($url) @($url) $role) { throw 'selftest-explicit-pushurl' }; $checks++
        if (Test-RemoteShape @($url,$url) $false @($url) @($url) $role) { throw 'selftest-multiple-raw-url' }; $checks++
        if (Test-RemoteShape @($url,'') $false @($url) @($url) $role) { throw 'selftest-empty-extra-raw-url' }; $checks++
        if (Test-RemoteShape @($url) $false @('https://invalid.example/rewrite') @($url) $role) { throw 'selftest-fetch-rewrite' }; $checks++
        if (Test-RemoteShape @($url) $false @($url) @('https://invalid.example/rewrite') $role) { throw 'selftest-push-rewrite' }; $checks++
        if (Test-RemoteShape @($url) $false @($url,$url) @($url) $role) { throw 'selftest-multiple-effective-url' }; $checks++
    }
    $configValues=Get-NullConfigValues ($script:CnbUrl+[char]0+''+[char]0)
    if ($configValues.Count -ne 2 -or $configValues[1] -cne '') { throw 'selftest-preserve-empty-config' }; $checks++
    if ((Get-Mode $true $true) -cne 'dry-run' -or (Get-Mode $false $false) -cne 'read-only') { throw 'selftest-mode' }; $checks++
    if ((Get-Decision 'a' 'a' $false) -cne 'noop' -or (Get-Decision 'a' 'b' $false) -cne 'stop-diverged' -or (Get-Decision 'a' 'b' $true) -cne 'fast-forward') { throw 'selftest-decisions' }; $checks++
    $github=@(Get-NetworkOptions $false); $cnb=@(Get-NetworkOptions $true)
    if ($github -notcontains ('http.{0}.proxy={1}' -f $script:GithubUrl,$script:ProxyUrl) -or $github -notcontains 'credential.helper=' -or $github -contains 'http.proxy=') { throw 'selftest-github-route' }; $checks++
    if ($cnb -notcontains ('http.{0}.proxy=' -f $script:CnbUrl) -or $cnb -notcontains 'credential.helper=' -or ($cnb -join ' ') -match '10808') { throw 'selftest-cnb-route' }; $checks++
    foreach ($url in @($script:GithubUrl,$script:CnbUrl)) {
        $route=if ($url -ceq $script:CnbUrl) { $cnb } else { $github }
        if ($route -notcontains ('http.{0}.emptyAuth=false' -f $url) -or $route -notcontains ('http.{0}.saveCookies=false' -f $url)) { throw 'selftest-scoped-auth-cookie' }; $checks++
    }
    $nativePush=@(Get-PushArguments ('a'*40) $false)
    $environmentPush=@(Get-PushArguments ('a'*40) $true)
    if ($nativePush -contains 'credential.helper=' -or $nativePush -contains ('credential.{0}.helper=' -f $script:CnbUrl)) { throw 'selftest-native-helper-reuse' }; $checks++
    if ($environmentPush -notcontains 'credential.helper=' -or $environmentPush -notcontains ('credential.{0}.helper=' -f $script:CnbUrl)) { throw 'selftest-environment-helper-reset' }; $checks++
    foreach ($route in @($github,$cnb)) { if ($route -notcontains 'http.sslVerify=true' -or $route -notcontains 'http.followRedirects=false') { throw 'selftest-tls' }; $checks++ }
    $sha='a'*40; $pushArgs=@(Get-PushArguments $sha $true)
    if ($pushArgs[-1] -cne ($sha+':refs/heads/main') -or $pushArgs -notcontains $script:CnbUrl -or ($pushArgs -join ' ') -match '\bHEAD\b|--force|\+refs') { throw 'selftest-refspec' }; $checks++
    foreach ($name in @('CNB','CNB_TOKEN','HTTP_PROXY','https_proxy','NO_PROXY','GIT_CONFIG_COUNT','GIT_TRACE2','GIT_DIR','SSH_ASKPASS','GCM_TRACE','NETRC','CURL_HOME')) {
        if (-not (Test-StripEnvironment $name $false)) { throw 'selftest-env-strip' }; $checks++
    }
    if (-not (Test-PublicReadCredentialBoundary $false @('PATH','HOME'))) { throw 'selftest-public-boundary-clean' }; $checks++
    if (Test-PublicReadCredentialBoundary $true @('PATH')) { throw 'selftest-netrc-file' }; $checks++
    foreach ($name in @('NETRC','netrc','NetRc')) {
        if (Test-PublicReadCredentialBoundary $false @('PATH',$name)) { throw 'selftest-netrc-environment-name' }; $checks++
    }
    foreach ($url in @($script:GithubUrl,$script:CnbUrl)) {
        $route=if ($url -ceq $script:CnbUrl) { $cnb } else { $github }
        if ($route -notcontains ('http.{0}.proactiveAuth=none' -f $url)) { throw 'selftest-public-proactive-auth' }; $checks++
    }
    if (-not (Test-ClientCredentialConfigNames @('core.filemode','http.sslVerify','http.proxy'))) { throw 'selftest-client-config-clean' }; $checks++
    foreach ($key in @('http.sslCert','HTTP.SSLKEY','http.proxySSLCert','Http.ProxySslKey','http.https://opaque.invalid/repo.sslCert','HTTP.https://opaque.invalid/repo.PROXYSSLKEY')) {
        if (Test-ClientCredentialConfigNames @('core.filemode',$key)) { throw 'selftest-client-config-refused' }; $checks++
    }
    foreach ($route in @($github,$cnb,$nativePush,$environmentPush)) {
        if (@($route | Where-Object { $_ -imatch '^http(?:\..+)?\.(?:sslcert|sslkey|proxysslcert|proxysslkey)=' }).Count -gt 0) { throw 'selftest-no-empty-certificate-arguments' }; $checks++
    }
    if (Test-StripEnvironment 'PATH' $false) { throw 'selftest-env-path' }; $checks++
    if (Test-StripEnvironment 'CNB' $true) { throw 'selftest-env-opt-in' }; $checks++
    foreach ($raw in @('https://user:secret@invalid/?token=hidden Bearer confidential','authentication failed secret','TLS opaque-fixture' ,'unknown private-host private-user')) {
        if ((Get-SafeCategory $raw) -notmatch '^[a-z-]+$' -or (Get-SafeCategory $raw) -match 'secret|hidden|confidential|private') { throw 'selftest-redaction' }; $checks++
    }
    [pscustomobject]@{result='passed';checks=$checks;scope='pure-logic';gitInvoked=$false;networkInvoked=$false;credentialsRead=$false;filesWritten=$false} | ConvertTo-Json -Compress
}
function Invoke-Main {
    if (-not (Test-FullSha $TargetSha) -or -not (Test-Version $ExpectedVersion)) { throw 'explicit-sha-and-version-required' }
    $script:Mode=Get-Mode ([bool]$Push) ([bool]$DryRun)
    $script:ResolvedRepo=(Resolve-Path -LiteralPath $RepoPath -ErrorAction Stop).Path
    $null=Get-Checked @('rev-parse','--show-toplevel')
    Assert-NoLegacyGrafts
    Assert-Remote 'origin' $false; Assert-Remote 'cnb' $true
    $github=Get-RemoteMain $false; $mirror=Get-RemoteMain $true
    if ($github -cne $TargetSha) { throw 'upstream-main-does-not-match-target' }
    # Equality short-circuits before fetching, credential setup and any log write.
    if ($mirror -ceq $TargetSha) {
        if (-not (Test-Object $TargetSha)) { throw 'object-unavailable-noop-will-not-fetch' }
        $version=Get-Version $TargetSha
        [pscustomobject]@{mode=$script:Mode;result='noop';targetSha=$TargetSha;version=$version} | ConvertTo-Json -Compress
        return
    }
    Ensure-Object $TargetSha $false; Ensure-Object $mirror $true
    $ancestor=Test-Ancestor $mirror $TargetSha
    if ((Get-Decision $mirror $TargetSha $ancestor) -cne 'fast-forward') { throw 'mirror-diverged-stop' }
    $version=Get-Version $TargetSha
    if ($script:Mode -cne 'push') {
        [pscustomobject]@{mode=$script:Mode;result='would-fast-forward';targetSha=$TargetSha;mirrorMainSha=$mirror;ancestor=$ancestor;version=$version} | ConvertTo-Json -Compress
        return
    }
    # A dirty working tree cannot affect the explicit refspec, but requiring an
    # isolated clean checkout makes the public write operation reviewable.
    if (-not [string]::IsNullOrWhiteSpace((Get-Checked @('status','--porcelain=v1','--untracked-files=all')))) { throw 'push-requires-clean-checkout' }
    Assert-Remote 'origin' $false; Assert-Remote 'cnb' $true
    $githubBefore=Get-RemoteMain $false; $mirrorBefore=Get-RemoteMain $true
    if ($githubBefore -cne $TargetSha -or $mirrorBefore -cne $mirror) { throw 'remote-ref-changed-stop' }
    if (-not (Test-Ancestor $mirrorBefore $TargetSha)) { throw 'mirror-diverged-stop' }
    $version=Get-Version $TargetSha
    $gitDirectory=Get-Checked @('rev-parse','--absolute-git-dir')
    $script:MetadataDirectory=Join-Path $gitDirectory 'sync-cnb'
    [void][IO.Directory]::CreateDirectory($script:MetadataDirectory)
    $recordPath=Join-Path $script:MetadataDirectory ('run-'+[Guid]::NewGuid().ToString('N')+'.json')
    $record=[ordered]@{recordedAtUtc=[DateTime]::UtcNow.ToString('o');targetSha=$TargetSha;upstreamMainSha=$githubBefore;mirrorMainSha=$mirrorBefore;ancestor=$true;version=$version;result='push-pending';category='none';requiresReadOnlyRecheck=$false}
    Write-Record $record $recordPath
    Assert-ClientCredentialConfiguration
    $script:PushAttempted=$true
    $result=Invoke-Git (Get-PushArguments $TargetSha ([bool]$UseCnbEnvironmentCredential)) ([bool]$UseCnbEnvironmentCredential) $true
    if ($result.Code -ne 0) {
        $record.result='push-outcome-unverified';$record.category=$result.Category;$record.requiresReadOnlyRecheck=$true
        Write-Record $record $recordPath
        throw 'push-failed-check-remote-read-only-before-any-retry'
    }
    try {
        $post=Get-RemoteMain $true
        if ($post -cne $TargetSha -or (Get-Version $post) -cne $ExpectedVersion) { throw 'postcheck-mismatch' }
        $record.result='push-and-postcheck-succeeded';$record['postPushMainSha']=$post
        Write-Record $record $recordPath
    } catch {
        $record.result='push-returned-success-postcheck-unverified';$record.category='postcheck-failed';$record.requiresReadOnlyRecheck=$true
        Write-Record $record $recordPath
        throw 'push-postcheck-unverified-check-remote-read-only-before-any-retry'
    }
    [pscustomobject]@{result='push-and-postcheck-succeeded';targetSha=$TargetSha;version=$version;record='git-metadata'} | ConvertTo-Json -Compress
}

if ($SelfTest) {
    try { Invoke-SelfTest } catch { [Console]::Error.WriteLine('selftest-failed'); exit 1 }
    return
}
try { Invoke-Main } catch {
    # Never emit ErrorRecord/Exception/raw command output: it can contain secrets.
    $allowed=@('local-history-override-refused','public-read-client-credential-config-refused','public-read-implicit-credential-file-refused','explicit-sha-and-version-required','remote-url-policy-refused','object-unavailable-read-only-will-not-fetch','object-unavailable-noop-will-not-fetch','required-commit-unavailable','upstream-main-does-not-match-target','version-mismatch','mirror-diverged-stop','push-requires-clean-checkout','remote-ref-changed-stop','ancestry-check-failed','remote-main-response-invalid','authentication-failed','tls-failed','remote-rejected','name-resolution-failed','connection-failed','git-operation-failed','local-operation-failed','operation-timeout-outcome-unverified','push-failed-check-remote-read-only-before-any-retry','push-postcheck-unverified-check-remote-read-only-before-any-retry')
    $category=if ($allowed -ccontains $_.Exception.Message) { $_.Exception.Message } else { 'local-safety-check-failed' }
    if ($script:PushAttempted) { $category='push-outcome-requires-read-only-recheck-before-retry' }
    [Console]::Error.WriteLine($category)
    exit 1
}
