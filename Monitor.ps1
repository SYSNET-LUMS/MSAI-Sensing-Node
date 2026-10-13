param([string]$Port)
$ErrorActionPreference='Stop'
if (-not $Port) {
  $ports=@([System.IO.Ports.SerialPort]::GetPortNames())
  if ($ports.Count -eq 1) {$Port=$ports[0]} else {
    Write-Host ('Available ports: '+($ports -join ', '))
    $Port=Read-Host 'PCB programming port (for example COM17)'
  }
}
$serial=New-Object System.IO.Ports.SerialPort $Port,115200,None,8,One
$serial.DtrEnable=$false
$serial.RtsEnable=$false
$serial.ReadTimeout=500
try {
  $serial.Open()
  Write-Host 'Console open. Press RESET to see startup. Commands: status, setup, reset-setup. Ctrl+C exits.'
  $command=''
  while ($true) {
    $text=$serial.ReadExisting()
    if ($text) {Write-Host -NoNewline $text}
    if ([Console]::KeyAvailable) {
      $key=[Console]::ReadKey($true)
      if ($key.Key -eq 'Enter') {$serial.WriteLine($command);$command='';Write-Host ''}
      elseif ($key.Key -eq 'Backspace') {if ($command.Length) {$command=$command.Substring(0,$command.Length-1);Write-Host -NoNewline "`b `b"}}
      elseif (-not [char]::IsControl($key.KeyChar)) {$command+=$key.KeyChar;Write-Host -NoNewline $key.KeyChar}
    }
    Start-Sleep -Milliseconds 100
  }
} finally {$serial.Close();$serial.Dispose()}
