# Build-WinPE.ps1
# Creates a WinPE USB/ISO with the Bretter-IMG agent baked in.
# Prerequisites: Windows ADK + WinPE add-on installed
# Run as Administrator on a Windows machine.

param(
    [string]$AgentExePath = ".\agent.exe",
    [string]$ServerUrl    = "http://192.168.1.10:8000",
    [string]$AgentToken   = "",
    [string]$OutputISO    = ".\BretterIMG-WinPE.iso",
    [string]$ADKPath      = "C:\Program Files (x86)\Windows Kits\10\Assessment and Deployment Kit"
)

$ErrorActionPreference = "Stop"
$WinPERoot = "$ADKPath\Windows Preinstallation Environment"
$DISMPath  = "$ADKPath\Deployment Tools\amd64\DISM"

Write-Host "=== Bretter-IMG WinPE Builder ===" -ForegroundColor Cyan

# 1. Copy WinPE base files
$WorkDir = "$env:TEMP\BretterWinPE"
if (Test-Path $WorkDir) { Remove-Item $WorkDir -Recurse -Force }
Copy-Item "$WinPERoot\amd64\en-us" $WorkDir -Recurse

# 2. Mount the WIM
$MountDir = "$env:TEMP\BretterMount"
New-Item -ItemType Directory -Path $MountDir -Force | Out-Null
$WimPath = "$WorkDir\media\sources\boot.wim"

Write-Host "Mounting WIM..."
& "$DISMPath\dism.exe" /Mount-Wim /WimFile:"$WimPath" /index:1 /MountDir:"$MountDir"

# 3. Copy agent binary + config
$AgentDir = "$MountDir\BretterIMG"
New-Item -ItemType Directory -Path $AgentDir -Force | Out-Null
Copy-Item $AgentExePath "$AgentDir\agent.exe"

# Write config
@"
BRETTER_SERVER=$ServerUrl
BRETTER_AGENT_TOKEN=$AgentToken
BRETTER_ID_FILE=X:\BretterIMG\machine_id.txt
BRETTER_TEMP=X:\BretterIMG\temp
BRETTER_WINPE_STAGE=X:\BretterIMG\winpe
"@ | Set-Content "$AgentDir\agent.env"

# 4. Write winpeshl.ini to auto-start agent
@"
[LaunchApps]
"X:\BretterIMG\agent.exe"
"@ | Set-Content "$MountDir\Windows\System32\winpeshl.ini"

# 5. Unmount and commit
Write-Host "Committing WIM..."
& "$DISMPath\dism.exe" /Unmount-Wim /MountDir:"$MountDir" /Commit

# 6. Build ISO using oscdimg
$OscdimgPath = "$ADKPath\Deployment Tools\amd64\Oscdimg\oscdimg.exe"
Write-Host "Building ISO: $OutputISO"
& "$OscdimgPath" -m -o -u2 -udfver102 `
    -bootdata:"2#p0,e,b$WorkDir\fwfiles\etfsboot.com#pEF,e,b$WorkDir\fwfiles\efisys.bin" `
    "$WorkDir\media" "$OutputISO"

Write-Host "Done! ISO created: $OutputISO" -ForegroundColor Green
Write-Host "Flash to USB with Rufus or: dd if=$OutputISO of=/dev/sdX bs=4M status=progress"
