function Resolve-MedicPython {
    param(
        [string]$Preferred = ""
    )

    $Candidates = @()

    foreach ($Value in @($Preferred)) {
        if ($Value) {
            $Clean = $Value.Trim().Trim('"').Trim("'")
            if ($Clean -and $Candidates -notcontains $Clean) {
                $Candidates += $Clean
            }
        }
    }

    foreach ($Name in @($Preferred, "python.exe", "python")) {
        if (-not $Name) {
            continue
        }
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source -and ($Command.Source -notmatch "\\WindowsApps\\python(3)?\.exe$")) {
            $Clean = $Command.Source.Trim().Trim('"').Trim("'")
            if ($Clean -and $Candidates -notcontains $Clean) {
                $Candidates += $Clean
            }
        }
    }

    $PythonRoots = @()
    if ($env:LOCALAPPDATA) {
        $PythonRoots += (Join-Path $env:LOCALAPPDATA "Programs\Python")
    }
    if ($env:USERPROFILE) {
        $PythonRoots += (Join-Path $env:USERPROFILE "AppData\Local\Programs\Python")
    }
    if ($HOME) {
        $PythonRoots += (Join-Path $HOME "AppData\Local\Programs\Python")
    }

    foreach ($Root in ($PythonRoots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $Root)) {
            continue
        }
        foreach ($VersionDir in @("Python313", "Python312", "Python311", "Python310", "Python39")) {
            $Candidate = Join-Path (Join-Path $Root $VersionDir) "python.exe"
            if ($Candidates -notcontains $Candidate) {
                $Candidates += $Candidate
            }
        }
        $Dirs = Get-ChildItem -LiteralPath $Root -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending
        foreach ($Dir in $Dirs) {
            $Candidate = Join-Path $Dir.FullName "python.exe"
            if ($Candidates -notcontains $Candidate) {
                $Candidates += $Candidate
            }
        }
    }

    foreach ($Candidate in $Candidates) {
        if ($Candidate -match "\\WindowsApps\\python(3)?\.exe$") {
            continue
        }
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
        $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Command -and $Command.Source -and ($Command.Source -notmatch "\\WindowsApps\\python(3)?\.exe$")) {
            return $Command.Source
        }
    }

    throw "Unable to find a working Python executable. Pass -Python with the full path to python.exe."
}
