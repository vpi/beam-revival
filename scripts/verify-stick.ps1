# Read-only check that a USB stick holds a beam-usb-*.img system correctly.
# Defaults to beam-usb-stable.img; for the lab stick add  -Image ..\beam-usb-lab.img
# Run in PowerShell **as Administrator** (raw disk reads need it):
#   powershell -ExecutionPolicy Bypass -File scripts\verify-stick.ps1 -DiskNumber 3
#
# Compares the boot code area (first 1 MiB) and the whole system partition with the image.
# The BEAMCONFIG partition is skipped: Windows adds files to it, and you edit beam.conf there.
param(
  [Parameter(Mandatory = $true)][int]$DiskNumber,
  [string]$Image
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 leaves $PSScriptRoot empty in param defaults, so resolve it here.
if (-not $Image) {
  $Image = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) 'beam-usb-stable.img'
}
if (-not (Test-Path $Image)) { throw "Image not found: $Image (pass -Image <path to beam-usb-stable.img or beam-usb-lab.img>)" }

$disk = Get-Disk -Number $DiskNumber
if ($disk.BusType -ne 'USB') { throw "Disk $DiskNumber is not a USB disk ($($disk.FriendlyName)). Refusing." }
Write-Host "Checking disk $DiskNumber ($($disk.FriendlyName)) against $Image"

$dev = [System.IO.File]::Open("\\.\PhysicalDrive$DiskNumber", 'Open', 'Read', 'ReadWrite')
$img = [System.IO.File]::OpenRead((Resolve-Path $Image))

function Compare-Region([string]$name, [long]$start, [long]$length, [int[]]$skip = @()) {
  $chunk = [long]4MB                     # long: [Math]::Min must not pick the Int32 overload for 5+ GB lengths
  $a = New-Object byte[] $chunk; $b = New-Object byte[] $chunk
  $dev.Position = $start; $img.Position = $start
  $done = [long]0; $lastPct = -1
  $bad = 0; $total = 0; $firstBad = $null; $lastBad = $null
  while ($done -lt $length) {
    $n = [int][Math]::Min($chunk, [long]($length - $done))
    foreach ($pair in @(@($dev, $a), @($img, $b))) {
      $r = 0
      while ($r -lt $n) {
        $got = $pair[0].Read($pair[1], $r, $n - $r)
        if ($got -le 0) {                  # never spin on end-of-device
          Write-Host "  $name : MISMATCH: read ended early at byte $($start + $done + $r)" -ForegroundColor Red
          return $false
        }
        $r += $got
      }
    }
    foreach ($s in $skip) { if ($s -ge $done -and $s -lt $done + $n) { $a[$s - $done] = $b[$s - $done] } }
    $total++
    if (-not [System.Linq.Enumerable]::SequenceEqual($a, $b)) {
      # Keep scanning: the pattern of bad chunks says more than the first bad byte.
      $bad++
      if ($null -eq $firstBad) {
        for ($i = 0; $i -lt $n; $i++) { if ($a[$i] -ne $b[$i]) { break } }
        $firstBad = $start + $done + $i
        Write-Host "  $name : first MISMATCH at byte $firstBad (continuing scan)" -ForegroundColor Red
      }
      $lastBad = $start + $done
    }
    $done += $n
    $pct = [int](100 * $done / $length)
    if ($pct -ne $lastPct -and $pct % 10 -eq 0) { Write-Host "  $name : $pct%"; $lastPct = $pct }
  }
  if ($bad -eq 0) { Write-Host "  $name : OK" -ForegroundColor Green; return $true }
  $mb = [math]::Round(($lastBad - $firstBad) / 1MB)
  Write-Host ("  $name : {0} of {1} 4-MiB chunks differ, spread over {2} MiB (bytes {3} .. {4})" -f $bad, $total, $mb, $firstBad, $lastBad) -ForegroundColor Red
  if ($bad -eq $total) { Write-Host "    -> nothing matches: the image wasn't written here at all" }
  elseif ($bad -le 3) { Write-Host "    -> a few isolated bad spots: likely a flaky stick" }
  else { Write-Host "    -> large areas differ: an interrupted or failed write" }
  return $false
}

# MBR disk signature (bytes 440-443) may be stamped by Windows; ignore it.
$ok1 = Compare-Region "boot code (MBR + GRUB)" 0 1MB @(440, 441, 442, 443)
$p2 = Get-Partition -DiskNumber $DiskNumber -PartitionNumber 2
$ok2 = Compare-Region "system partition" $p2.Offset ($img.Length - $p2.Offset)
$dev.Close(); $img.Close()

if ($ok1 -and $ok2) { Write-Host "`nThe stick matches the image. It's good to boot." -ForegroundColor Green }
else { Write-Host "`nThe stick does NOT match the image. Rewrite it (Rufus, rear USB port) or try another stick." -ForegroundColor Red }
