<#
.SYNOPSIS
  Prepares a fresh Windows machine as a self-hosted GitHub Actions runner for fvlvte/projectag.

.DESCRIPTION
  Installs, machine-wide, the tools the Windows jobs expect on a self-hosted runner
  (claude-ci.yml `windows`, qa-windows-harness.yml `run-qa-gpu`), then optionally
  registers the runner. Run it again at any time: installed tools are detected and
  skipped, and the final check reports anything still missing.

  Installs:
    - Visual Studio 2022 Build Tools: C++ workload with the Windows SDK (MSVC linker)
    - Rust through rustup (in -ToolsRoot when rustup is absent), toolchain -RustToolchain
      with rustfmt, clippy and the wasm32-unknown-unknown target, as rust-toolchain.toml pins
    - Git for Windows (git, Git Bash, openssl, tar), PowerShell 7 (pwsh), Python 3.13,
      GitHub CLI (gh), CMake, NASM, LLVM/Clang (LIBCLANG_PATH), Visual C++ runtime
  Configures: Win32 long paths, git core.longpaths, and (unless -SkipPowerSettings)
  never sleep or turn off the display, so a windowed client keeps presenting.
  Reports, never installs: GPU and driver, Vulkan and Direct3D 12 runtimes. Install the
  GPU vendor's driver yourself.

  Runner registration (optional): pass -RunnerToken from GitHub -> repository Settings ->
  Actions -> Runners -> New self-hosted runner. The token is used once and never printed.
  `self-hosted`, `Windows` and `X64` labels are automatic; -RunnerLabels adds the rest.
  Logon mode (default) starts the runner in the signed-in desktop session at logon,
  which the windowed QA client needs; Service mode suits build-only machines.
  Keep pull_request workflows off self-hosted labels: this repository is public.

.EXAMPLE
  # Elevated PowerShell (Run as administrator) on the new machine:
  Set-ExecutionPolicy -Scope Process Bypass -Force
  $s = Join-Path $env:TEMP 'windows-runner-setup.ps1'
  Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/fvlvte/projectag/master/.github/scripts/windows-runner-setup.ps1 -OutFile $s
  & $s -RunnerToken '<token>' -RunnerLabels heavy-gpu

.EXAMPLE
  # Report what is installed and missing without changing anything:
  & $s -CheckOnly
#>
[CmdletBinding()]
param(
  # Keep equal to rust-toolchain.toml (tests/projectag-rust-toolchain.test.mjs enforces it).
  [string]$RustToolchain = '1.99.1',
  [string]$ToolsRoot = 'C:\Rust',
  [string]$RunnerToken = '',
  [string]$RunnerUrl = 'https://github.com/fvlvte/projectag',
  [string]$RunnerName = $env:COMPUTERNAME,
  [string[]]$RunnerLabels = @('heavy-gpu'),
  [string]$RunnerDir = 'C:\actions-runner',
  [ValidateSet('Logon', 'Service')]
  [string]$RunnerMode = 'Logon',
  [switch]$SkipPowerSettings,
  [switch]$DefenderExclusions,
  [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$ProgramFilesX86 = [Environment]::GetFolderPath('ProgramFilesX86')
$Vswhere = Join-Path $ProgramFilesX86 'Microsoft Visual Studio\Installer\vswhere.exe'
$RustComponents = @('rustfmt', 'clippy')
$RustTargets = @('wasm32-unknown-unknown')
$script:RebootNeeded = $false

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note([string]$Message) { Write-Host "    $Message" }
function Write-Warn([string]$Message) { Write-Host "    WARNING: $Message" -ForegroundColor Yellow }

function Assert-Administrator {
  $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell (Run as administrator).'
  }
}

# Put the registry PATH (machine, then user) first in this session so freshly installed
# tools resolve, keeping any session-only entries after it.
function Update-SessionPath {
  $registry = @(([Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
    [Environment]::GetEnvironmentVariable('Path', 'User')) -split ';' | Where-Object { $_ })
  $session = @($env:Path -split ';' | Where-Object { $_ -and ($registry -notcontains $_) })
  $env:Path = ($registry + $session) -join ';'
}

# Append a directory to the machine PATH, keeping existing %VAR% entries unexpanded.
function Add-MachinePath([string]$Dir) {
  if (-not (Test-Path -LiteralPath $Dir)) { return }
  $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $true)
  try {
    $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $parts = @($raw -split ';' | Where-Object { $_ })
    $present = $parts | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $Dir.TrimEnd('\') }
    if (-not $present) {
      $key.SetValue('Path', (($parts + $Dir) -join ';'), [Microsoft.Win32.RegistryValueKind]::ExpandString)
      Write-Note "added to machine PATH: $Dir"
    }
  } finally { $key.Close() }
  Update-SessionPath
}

function Set-MachineVariable([string]$Name, [string]$Value) {
  if ([Environment]::GetEnvironmentVariable($Name, 'Machine') -ne $Value) {
    [Environment]::SetEnvironmentVariable($Name, $Value, 'Machine')
    Write-Note "set machine $Name=$Value"
  }
  Set-Item -Path "env:$Name" -Value $Value
}

function Find-Command([string]$Name) {
  # A WindowsApps python.exe is the Store alias stub, not an interpreter.
  Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
    Where-Object { $Name -eq 'winget' -or $_.Source -notlike '*\WindowsApps\*' } | Select-Object -First 1
}

function Save-Download([string]$Uri, [string]$Name) {
  $dir = Join-Path $env:TEMP 'projectag-runner-setup'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $path = Join-Path $dir $Name
  Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $path
  return $path
}

function Assert-Sha256([string]$Path, [string]$Expected) {
  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
  if ($actual -ine $Expected.Trim()) { throw "checksum mismatch for $Path" }
}

function Assert-MicrosoftSigned([string]$Path) {
  $sig = Get-AuthenticodeSignature -LiteralPath $Path
  if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
    throw "$Path is not validly signed by Microsoft"
  }
}

function Get-Winget {
  $winget = Find-Command 'winget'
  if ($winget) { return $winget.Source }
  Write-Note 'winget not found; registering App Installer'
  try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe } catch { }
  $winget = Find-Command 'winget'
  if ($winget) { return $winget.Source }
  Write-Note 'installing winget through the Microsoft.WinGet.Client module'
  Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers | Out-Null
  Install-Module -Name Microsoft.WinGet.Client -Force -Scope AllUsers -Repository PSGallery
  Import-Module Microsoft.WinGet.Client
  Repair-WinGetPackageManager -AllUsers -Latest
  Update-SessionPath
  $winget = Find-Command 'winget'
  if ($winget) { return $winget.Source }
  throw 'winget is unavailable. Install "App Installer" from Microsoft, then run this script again.'
}

# Each tool: winget id, a command that proves it is installed, its usual install directory
# (added to the machine PATH so service accounts see it too) and the binary inside it.
$Tools = @(
  @{ Name = 'Git for Windows'; Id = 'Git.Git'; Command = 'git'; Dirs = @("$env:ProgramFiles\Git\cmd"); Probe = "$env:ProgramFiles\Git\bin\bash.exe" },
  @{ Name = 'PowerShell 7'; Id = 'Microsoft.PowerShell'; Command = 'pwsh'; Dirs = @("$env:ProgramFiles\PowerShell\7"); Probe = "$env:ProgramFiles\PowerShell\7\pwsh.exe" },
  @{ Name = 'Python 3.13'; Id = 'Python.Python.3.13'; Command = 'python'; Dirs = @("$env:ProgramFiles\Python313", "$env:ProgramFiles\Python313\Scripts"); Probe = "$env:ProgramFiles\Python313\python.exe" },
  @{ Name = 'GitHub CLI'; Id = 'GitHub.cli'; Command = 'gh'; Dirs = @("$env:ProgramFiles\GitHub CLI"); Probe = "$env:ProgramFiles\GitHub CLI\gh.exe" },
  @{ Name = 'CMake'; Id = 'Kitware.CMake'; Command = 'cmake'; Dirs = @("$env:ProgramFiles\CMake\bin"); Probe = "$env:ProgramFiles\CMake\bin\cmake.exe" },
  @{ Name = 'NASM'; Id = 'NASM.NASM'; Command = 'nasm'; Dirs = @("$env:ProgramFiles\NASM"); Probe = "$env:ProgramFiles\NASM\nasm.exe" },
  @{ Name = 'LLVM/Clang'; Id = 'LLVM.LLVM'; Command = 'clang'; Dirs = @("$env:ProgramFiles\LLVM\bin"); Probe = "$env:ProgramFiles\LLVM\bin\clang.exe" },
  @{ Name = 'Visual C++ runtime'; Id = 'Microsoft.VCRedist.2015+.x64'; Command = ''; Dirs = @(); Probe = "$env:SystemRoot\System32\vcruntime140.dll" }
)

function Test-Tool($Tool) {
  if ($Tool.Probe -and (Test-Path -LiteralPath $Tool.Probe)) { return $true }
  if ($Tool.Command -and (Find-Command $Tool.Command)) { return $true }
  return $false
}

function Install-Tool($Tool) {
  foreach ($dir in $Tool.Dirs) { Add-MachinePath $dir }
  if (Test-Tool $Tool) { Write-Note "$($Tool.Name): present"; return }
  $winget = Get-Winget
  Write-Note "$($Tool.Name): installing $($Tool.Id)"
  $common = @('install', '--id', $Tool.Id, '--exact', '--source', 'winget', '--silent',
    '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity')
  & $winget @common --scope machine
  if ($LASTEXITCODE -ne 0 -and -not (Test-Tool $Tool)) {
    Write-Note "$($Tool.Name): no machine-scope installer (exit $LASTEXITCODE); retrying with the default scope"
    & $winget @common
  }
  foreach ($dir in $Tool.Dirs) { Add-MachinePath $dir }
  Update-SessionPath
  if (-not (Test-Tool $Tool)) { throw "$($Tool.Name) did not install (winget exit $LASTEXITCODE)" }
}

function Get-MsvcInstall {
  if (-not (Test-Path -LiteralPath $Vswhere)) { return $null }
  $path = & $Vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
  if ($path) { return ($path | Select-Object -First 1) }
  return $null
}

function Install-Msvc {
  $existing = Get-MsvcInstall
  if ($existing) { Write-Note "MSVC build tools: present in $existing"; return }
  Write-Note 'MSVC build tools: installing Visual Studio 2022 Build Tools (C++ workload, Windows SDK); this takes a while'
  $installer = Save-Download 'https://aka.ms/vs/17/release/vs_BuildTools.exe' 'vs_BuildTools.exe'
  Assert-MicrosoftSigned $installer
  $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
    '--quiet', '--wait', '--norestart', '--nocache',
    '--add', 'Microsoft.VisualStudio.Workload.VCTools', '--includeRecommended')
  if ($p.ExitCode -eq 3010) { $script:RebootNeeded = $true }
  elseif ($p.ExitCode -ne 0) { throw "Visual Studio Build Tools installer exited with $($p.ExitCode)" }
  if (-not (Get-MsvcInstall)) { throw 'MSVC build tools are still missing after the installer finished' }
}

function Install-Rust {
  $rustup = Find-Command 'rustup'
  if (-not $rustup) {
    Write-Note "rustup: installing into $ToolsRoot"
    $rustupHome = Join-Path $ToolsRoot 'rustup'
    $cargoHome = Join-Path $ToolsRoot 'cargo'
    New-Item -ItemType Directory -Force -Path $rustupHome, $cargoHome | Out-Null
    # The runner may run as NETWORK SERVICE or another account: let it update toolchains and caches.
    & icacls $ToolsRoot /grant '*S-1-5-11:(OI)(CI)M' '*S-1-5-20:(OI)(CI)M' /T /Q | Out-Null
    Set-MachineVariable 'RUSTUP_HOME' $rustupHome
    Set-MachineVariable 'CARGO_HOME' $cargoHome
    $base = 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe'
    $init = Save-Download $base 'rustup-init.exe'
    $sha = Save-Download "$base.sha256" 'rustup-init.exe.sha256'
    Assert-Sha256 $init ((Get-Content -LiteralPath $sha -Raw) -split '\s+')[0]
    & $init -y --no-modify-path --default-toolchain none --profile minimal
    if ($LASTEXITCODE -ne 0) { throw "rustup-init exited with $LASTEXITCODE" }
    Add-MachinePath (Join-Path $cargoHome 'bin')
    $rustup = Find-Command 'rustup'
    if (-not $rustup) { throw 'rustup is not on PATH after installation' }
  } else {
    Write-Note "rustup: present at $($rustup.Source)"
  }
  $install = @('toolchain', 'install', $RustToolchain, '--profile', 'minimal')
  foreach ($c in $RustComponents) { $install += @('--component', $c) }
  foreach ($t in $RustTargets) { $install += @('--target', $t) }
  & $rustup.Source @install
  if ($LASTEXITCODE -ne 0) { throw "rustup toolchain install exited with $LASTEXITCODE" }
  & $rustup.Source default $RustToolchain
  if ($LASTEXITCODE -ne 0) { throw "rustup default exited with $LASTEXITCODE" }
}

function Set-MachineSettings {
  $fs = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
  if ((Get-ItemProperty -Path $fs -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled -ne 1) {
    Set-ItemProperty -Path $fs -Name LongPathsEnabled -Value 1 -Type DWord
    Write-Note 'enabled Win32 long paths'
  }
  $git = Find-Command 'git'
  if ($git) { & $git.Source config --system core.longpaths true }
  if (-not $SkipPowerSettings) {
    foreach ($setting in 'standby-timeout-ac', 'hibernate-timeout-ac', 'monitor-timeout-ac') {
      & powercfg /change $setting 0
    }
    Write-Note 'power: never sleep, hibernate or turn off the display on AC power'
  }
  if ($DefenderExclusions) {
    Add-MpPreference -ExclusionPath $RunnerDir, $ToolsRoot
    Write-Note "Defender: excluded $RunnerDir and $ToolsRoot"
  }
}

function Show-Graphics {
  Write-Step 'Graphics (reported only)'
  foreach ($gpu in Get-CimInstance Win32_VideoController) {
    Write-Note "$($gpu.Name), driver $($gpu.DriverVersion)"
  }
  foreach ($dll in 'vulkan-1.dll', 'd3d12.dll') {
    $present = Test-Path -LiteralPath (Join-Path $env:SystemRoot "System32\$dll")
    if ($present) { Write-Note "$dll present" } else { Write-Warn "$dll missing: install the GPU vendor driver" }
  }
  $smi = Find-Command 'nvidia-smi'
  if ($smi) { & $smi.Source --query-gpu=name,driver_version,memory.total --format=csv,noheader }
  if ($RunnerLabels -contains 'heavy-gpu' -and -not (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match 'NVIDIA|AMD|Radeon|Intel\(R\) Arc' })) {
    Write-Warn 'no discrete GPU found, but the runner would get the heavy-gpu label'
  }
}

function Install-Runner {
  if (-not $RunnerToken) {
    Write-Note 'no -RunnerToken: runner registration skipped'
    return
  }
  if (Test-Path -LiteralPath (Join-Path $RunnerDir '.runner')) {
    Write-Note "a runner is already configured in $RunnerDir; remove it with config.cmd remove before registering again"
    return
  }
  New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
  $release = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/actions/runner/releases/latest' -Headers @{ 'User-Agent' = 'projectag-runner-setup' }
  $asset = $release.assets | Where-Object { $_.name -match '^actions-runner-win-x64-[\d.]+\.zip$' } | Select-Object -First 1
  if (-not $asset) { throw "no win-x64 asset in actions/runner $($release.tag_name)" }
  $hash = [regex]::Match([string]$release.body, '<!-- BEGIN SHA win-x64 -->([0-9a-fA-F]{64})<!-- END SHA win-x64 -->').Groups[1].Value
  if (-not $hash) { throw "actions/runner $($release.tag_name) publishes no win-x64 checksum" }
  Write-Note "runner: $($asset.name)"
  $zip = Save-Download $asset.browser_download_url $asset.name
  Assert-Sha256 $zip $hash
  Expand-Archive -LiteralPath $zip -DestinationPath $RunnerDir -Force
  $config = @('--unattended', '--url', $RunnerUrl, '--token', $RunnerToken, '--name', $RunnerName,
    '--labels', ($RunnerLabels -join ','), '--work', '_work', '--replace')
  if ($RunnerMode -eq 'Service') { $config += '--runasservice' }
  Push-Location $RunnerDir
  try {
    & (Join-Path $RunnerDir 'config.cmd') @config
    if ($LASTEXITCODE -ne 0) { throw "config.cmd exited with $LASTEXITCODE" }
  } finally { Pop-Location }
  if ($RunnerMode -eq 'Logon') {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute (Join-Path $RunnerDir 'run.cmd') -WorkingDirectory $RunnerDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName 'GitHub Actions runner' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName 'GitHub Actions runner'
    Write-Note "runner starts in $user's desktop session at logon (scheduled task 'GitHub Actions runner'); it is running now"
  } else {
    Write-Note 'runner installed as a Windows service; it has no desktop, so keep windowed QA jobs off this machine'
  }
}

# A configured runner keeps the PATH it saw at registration in .path; refresh it after installs.
function Update-RunnerPath {
  $file = Join-Path $RunnerDir '.path'
  if ((Test-Path -LiteralPath $file) -and ((Get-Content -LiteralPath $file -Raw).Trim() -ne $env:Path)) {
    Set-Content -LiteralPath $file -Value $env:Path -NoNewline
    Write-Note "updated $file; restart the runner so jobs see the new tools"
  }
}

function Test-Setup {
  Write-Step 'Check'
  $missing = @()
  foreach ($tool in $Tools) {
    if (Test-Tool $tool) { Write-Note "OK       $($tool.Name)" } else { Write-Note "MISSING  $($tool.Name)"; $missing += $tool.Name }
  }
  $msvc = Get-MsvcInstall
  if ($msvc) { Write-Note "OK       MSVC build tools ($msvc)" } else { Write-Note 'MISSING  MSVC build tools'; $missing += 'MSVC build tools' }
  $rustup = Find-Command 'rustup'
  $toolchains = if ($rustup) { (& $rustup.Source toolchain list) -join "`n" } else { '' }
  if ($toolchains -match [regex]::Escape($RustToolchain)) {
    Write-Note "OK       Rust $RustToolchain"
    $installed = & $rustup.Source +$RustToolchain component list --installed
    foreach ($c in $RustComponents) {
      if (-not ($installed | Where-Object { $_ -like "$c*" })) { Write-Note "MISSING  Rust component $c"; $missing += "Rust component $c" }
    }
    $targets = & $rustup.Source +$RustToolchain target list --installed
    foreach ($t in $RustTargets) {
      if ($targets -notcontains $t) { Write-Note "MISSING  Rust target $t"; $missing += "Rust target $t" }
    }
  } else {
    Write-Note "MISSING  Rust $RustToolchain"; $missing += "Rust $RustToolchain"
  }
  $libclang = $env:LIBCLANG_PATH
  if (-not $libclang) { $libclang = [Environment]::GetEnvironmentVariable('LIBCLANG_PATH', 'Machine') }
  if ($libclang -and (Test-Path -LiteralPath (Join-Path $libclang 'libclang.dll'))) { Write-Note 'OK       LIBCLANG_PATH' }
  else { Write-Note 'MISSING  LIBCLANG_PATH'; $missing += 'LIBCLANG_PATH' }
  if ((Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled -eq 1) { Write-Note 'OK       Win32 long paths' }
  else { Write-Note 'MISSING  Win32 long paths'; $missing += 'Win32 long paths' }
  return ,$missing
}

Write-Step "Windows runner setup for $RunnerUrl ($(if ($CheckOnly) { 'check only' } else { 'install' }))"
Update-SessionPath
if (-not $CheckOnly) {
  Assert-Administrator
  Write-Step 'Build tools'
  Install-Msvc
  Write-Step 'Developer tools'
  foreach ($tool in $Tools) { Install-Tool $tool }
  $clang = Find-Command 'clang'
  if ($clang -and (Test-Path -LiteralPath (Join-Path (Split-Path $clang.Source) 'libclang.dll'))) {
    Set-MachineVariable 'LIBCLANG_PATH' (Split-Path $clang.Source)
  }
  Write-Step "Rust $RustToolchain"
  Install-Rust
  Write-Step 'Machine settings'
  Set-MachineSettings
  Write-Step 'GitHub Actions runner'
  Install-Runner
  Update-RunnerPath
}
Show-Graphics
$missing = Test-Setup
if ($script:RebootNeeded) { Write-Warn 'the Visual Studio installer asked for a reboot; reboot before the first job' }
if ($missing.Count -gt 0) {
  Write-Host "Missing: $($missing -join ', ')" -ForegroundColor Red
  exit 1
}
Write-Host 'Runner machine ready.' -ForegroundColor Green
exit 0
