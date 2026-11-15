# Gaming Recap - exportiert die Playnite-Bibliothek fuer die Gaming-Recap-App
# und protokolliert jede Spielsession. Schreibt nur nach %APPDATA%\GamingRecap.

$script:OutDir = Join-Path $env:APPDATA "GamingRecap"

function script:ToIso($d) {
    if ($d) { return ([DateTime]$d).ToString("o") }
    return $null
}

function script:Names($items) {
    $out = @()
    if ($items) { foreach ($i in $items) { if ($i -and $i.Name) { $out += [string]$i.Name } } }
    return ,$out
}

function Export-GamingRecap {
    param($scriptMainMenuItemActionArgs)
    try {
        if (-not (Test-Path $script:OutDir)) { New-Item -ItemType Directory -Path $script:OutDir | Out-Null }
        $list = New-Object System.Collections.ArrayList
        foreach ($g in $PlayniteApi.Database.Games) {
            $cover = $null
            if ($g.CoverImage -and -not $g.CoverImage.StartsWith("http")) { $cover = $PlayniteApi.Database.GetFullFilePath($g.CoverImage) }
            $bg = $null
            if ($g.BackgroundImage -and -not $g.BackgroundImage.StartsWith("http")) { $bg = $PlayniteApi.Database.GetFullFilePath($g.BackgroundImage) }
            $year = $null
            if ($g.ReleaseDate) { $year = $g.ReleaseDate.Year }
            $source = $null
            if ($g.Source) { $source = $g.Source.Name }
            $inst = $null
            if ($g.InstallDirectory) {
                try { $inst = $PlayniteApi.ExpandGameVariables($g, $g.InstallDirectory) } catch { $inst = $g.InstallDirectory }
            }
            $completion = $null
            if ($g.CompletionStatus) { $completion = $g.CompletionStatus.Name }
            [void]$list.Add([ordered]@{
                id             = $g.Id.ToString()
                name           = $g.Name
                gameId         = $g.GameId
                source         = $source
                platforms      = (Names $g.Platforms)
                genres         = (Names $g.Genres)
                tags           = (Names $g.Tags)
                features       = (Names $g.Features)
                developers     = (Names $g.Developers)
                playtime       = [int64]$g.Playtime
                playCount      = [int64]$g.PlayCount
                lastActivity   = (ToIso $g.LastActivity)
                added          = (ToIso $g.Added)
                completion     = $completion
                releaseYear    = $year
                criticScore    = $g.CriticScore
                communityScore = $g.CommunityScore
                installed      = [bool]$g.IsInstalled
                installDir     = $inst
                hidden         = [bool]$g.Hidden
                favorite       = [bool]$g.Favorite
                cover          = $cover
                background     = $bg
            })
        }
        $doc = [ordered]@{ exported = (Get-Date).ToString("o"); version = 2; games = $list }
        $json = ConvertTo-Json -InputObject $doc -Depth 6 -Compress
        $tmp = Join-Path $script:OutDir "playnite.tmp"
        [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -Force -LiteralPath $tmp -Destination (Join-Path $script:OutDir "playnite.json")
    } catch {
        $__logger.Error("Gaming Recap Export fehlgeschlagen: " + $_.Exception.Message)
    }
}

function OnApplicationStarted() { param($evnArgs) Export-GamingRecap }
function OnLibraryUpdated() { param($evnArgs) Export-GamingRecap }
function OnApplicationStopped() { param($evnArgs) Export-GamingRecap }

function OnGameStopped() {
    param($evnArgs)
    try {
        if (-not (Test-Path $script:OutDir)) { New-Item -ItemType Directory -Path $script:OutDir | Out-Null }
        $g = $evnArgs.Game
        $sec = [int64]$evnArgs.ElapsedSeconds
        $end = Get-Date
        $src = $null
        if ($g.Source) { $src = $g.Source.Name }
        $line = ConvertTo-Json -Compress -InputObject ([ordered]@{
            id = $g.Id.ToString(); name = $g.Name; source = $src
            start = $end.AddSeconds(-$sec).ToString("o"); end = $end.ToString("o"); seconds = $sec
        })
        [System.IO.File]::AppendAllText((Join-Path $script:OutDir "sessions.jsonl"), $line + "`n", (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        $__logger.Error("Gaming Recap Session-Log fehlgeschlagen: " + $_.Exception.Message)
    }
    Export-GamingRecap
}

function GetMainMenuItems() {
    param($getMainMenuItemsArgs)
    $item = New-Object Playnite.SDK.Plugins.ScriptMainMenuItem
    $item.Description = "Gaming Recap: Jetzt exportieren"
    $item.FunctionName = "Export-GamingRecap"
    $item.MenuSection = "@"
    return $item
}
