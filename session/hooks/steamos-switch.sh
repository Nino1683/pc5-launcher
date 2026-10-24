#!/usr/bin/bash
# pc5-launcher-hook v1 – steamos-switch.sh (E104 d, Bauplan 2.3): Weiche für SteamOS 3 (Steam Machine, Deck).
#
# Die Ergänzungsdatei ~/.config/systemd/user/steam-launcher.service.d/50-pc5-launcher.conf startet statt
# Valves /usr/lib/steamos/steam-launcher diese Datei (feste Kopie unter ~/.local/share/couch/hook/, nur nach
# bestandenem bash -n). Sie entscheidet: PC5 Launcher (Einstieg current/session/couch-session) oder Valves
# Launcher, und zwar immer per exec (der Hauptprozess des Dienstes bleibt einer).
#
# Regeln: kein set -e, kein set -u, nichts, was hängen kann. Jeder Zweifel heißt Valves Launcher, also das
# Steam, das ohne uns gestartet wäre. Ein schnelles Ende zählt Valves Kurzsitzungs-Zähler (drei Läufe unter
# 60 s = Steam wird neu entpackt): Diese Datei endet deshalb nie von selbst.
shopt -s execfail        # scheitert ein exec, läuft das Skript weiter bis zur nächsten Rückfallebene

valve=/usr/lib/steamos/steam-launcher
steam_cmd=(steam -steamos3 -steampal -steamdeck -gamepadui)   # was Valves Launcher selbst ausführt

valve_launcher() {
    echo "pc5-launcher: $1; starte Valves steam-launcher" >&2
    if [[ -x "$valve" ]]; then exec "$valve"; fi
    exec "${steam_cmd[@]}"
    echo "pc5-launcher: weder $valve noch steam ausführbar" >&2
    exit 1
}

# Die Sitzung beginnt ohne fremde COUCH_*-Werte (etwa COUCH_TEST_ROOT aus einem Testlauf); COUCH_DATA (eigener
# Datenordner) und COUCH_PLATFORM (aus der Ergänzungsdatei) bleiben.
for _v in "${!COUCH_@}"; do
    case "$_v" in COUCH_DATA|COUCH_PLATFORM) ;; *) unset "$_v" 2>/dev/null ;; esac
done
unset _v
export COUCH_PLATFORM=steamos

home="${HOME:-}"
[[ -n "$home" ]] || valve_launcher "HOME fehlt"
data="${COUCH_DATA:-$home/.local/share/couch}"
entry="$data/current/session/couch-session"
state="$home/.local/state/couch"

# Notschalter: Desktop-Eintrag „PC5 Launcher – Spielmodus aus“ bzw. touch ~/.config/couch/disabled
[[ -e "$home/.config/couch/disabled" ]] && valve_launcher "Notschalter ~/.config/couch/disabled"

# Einstieg vorhanden, ausführbar und ohne Syntaxfehler?
syntax_ok() {
    if command -v timeout >/dev/null 2>&1; then timeout 5 bash -n "$1" 2>/dev/null; else bash -n "$1" 2>/dev/null; fi
}
if [[ -f "$entry" && -x "$entry" ]] && syntax_ok "$entry"; then :; else
    valve_launcher "Einstieg fehlt oder ist unbrauchbar ($entry)"
fi

# Kennung der Fassung wie im Supervisor und im gsp-Haken: VERSION@Ordnername
rel="$(readlink -f -- "$data/current" 2>/dev/null)" || rel=""
ver=""
if [[ -n "$rel" && -f "$rel/VERSION" ]]; then read -r ver <"$rel/VERSION" 2>/dev/null; fi
id="${ver:-0}@${rel##*/}"

# Sicherung fuse-launcher: gilt für die Fassung aus ihrer ersten Zeile (leer = immer)
if [[ -e "$state/fuse-launcher" ]]; then
    fuse=""
    read -r fuse <"$state/fuse-launcher" 2>/dev/null
    if [[ -z "$fuse" || "$fuse" == "$id" ]]; then valve_launcher "Sicherung fuse-launcher gesetzt"; fi
fi

if [[ -x "$valve" ]]; then COUCH_FALLBACK_CMD="$valve"; else COUCH_FALLBACK_CMD="${steam_cmd[*]}"; fi
export COUCH_FALLBACK_CMD
export COUCH_SESSION_ACTIVE=1
export COUCH_SESSION_RELEASE="$id"
export COUCH_SHORT_SESSION_TRACKER=      # leer: Der Supervisor fasst keinen Kurzsitzungs-Zähler an
exec "$entry"
valve_launcher "Einstieg ließ sich nicht starten"
