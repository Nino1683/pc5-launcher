# pc5-launcher-hook v1 – gsp-hook.sh (E104 b, Bauplan 2.2): Haken für Systeme mit gamescope-session-plus
# (Bazzite, ChimeraOS, Nobara …). install.sh legt eine feste Kopie nach ~/.local/share/couch/hook/gsp-hook.sh
# (ersetzt sie nur nach bestandenem bash -n) und hängt an ~/.config/gamescope-session-plus/sessions.d/<client>
# diesen Block an bzw. legt die Datei mit genau diesem Block an:
#
#   # >>> pc5-launcher >>> (angelegt von install.sh, entfernt von uninstall.sh; nicht von Hand ändern)
#   [ -r "${HOME:-/nonexistent}/.local/share/couch/hook/gsp-hook.sh" ] && . "${HOME}/.local/share/couch/hook/gsp-hook.sh"
#   # <<< pc5-launcher <<<
#
# Inhaltlich session/sessions.d-steam, aber ohne OUTPUT_CONNECTOR (der Anschluss bleibt, wie das System ihn
# wählt) und mit COUCH_PLATFORM=gsp. gamescope-session-plus liest die Datei per "." unter "set -a" ein, nach
# der Stock-Datei des Systems. Regeln:
#   * kein Abbruch der Shell, kein Fehlermodus, nichts, was hängen kann. Jeder Fehler hier (Einstieg fehlt,
#     Syntaxfehler, Notschalter) heißt: reines Steam wie ohne uns.
#   * GAMESCOPECMD, Auflösung, HDR und Anschluss bleiben unangetastet (E1).
#   * Alles steht in einer Funktion: Bash liest eine Datei Befehl für Befehl. Hätte sie einen Syntaxfehler,
#     liefen ohne Funktion die Zeilen davor schon; so läuft gar nichts.

_couch_gsp_hook() {
    _couch_home="${HOME:-}"
    _couch_data="${COUCH_DATA:-$_couch_home/.local/share/couch}"
    _couch_entry="$_couch_data/current/session/couch-session"
    _couch_state="$_couch_home/.local/state/couch"

    if [[ -n "${CLIENTCMD:-}" && "${CLIENTCMD:-}" == "$_couch_entry" ]]; then
        :   # schon eingehängt (zweimal eingelesen, etwa über einen Verweis ogui-steam -> steam)
    else
        # Die echte Sitzung beginnt ohne COUCH_*-Variablen (ein COUCH_TEST_ROOT aus einem Testlauf in der
        # Benutzerumgebung ließe sie sonst im Prüfmodus laufen). COUCH_DATA bleibt (eigener Datenordner).
        for _couch_var in "${!COUCH_@}"; do
            [[ "$_couch_var" == COUCH_DATA ]] || unset "$_couch_var" 2>/dev/null || :
        done
        unset _couch_var
        COUCH_PLATFORM=gsp

        _couch_on=1
        [[ -n "$_couch_home" ]] || _couch_on=0
        # Notschalter: Desktop-Eintrag „PC5 Launcher – Spielmodus aus“ bzw. touch ~/.config/couch/disabled
        [[ -e "$_couch_home/.config/couch/disabled" ]] && _couch_on=0
        # Die Sitzung zerlegt $CLIENTCMD in Wörter: Der Pfad darf weder Leerraum noch Muster enthalten.
        case "$_couch_entry" in *[[:space:]\*\?\[\]]*) _couch_on=0 ;; esac
        if [[ $_couch_on == 1 ]]; then
            if [[ -f "$_couch_entry" && -x "$_couch_entry" ]] && timeout 5 bash -n "$_couch_entry" 2>/dev/null; then :; else _couch_on=0; fi
        fi
        # Ohne Stock-Befehl gäbe es keine Rückfallebene.
        [[ -n "${CLIENTCMD:-}" ]] || _couch_on=0

        # Kennung der eingehängten Fassung wie im Supervisor: VERSION@Ordnername
        _couch_rel="$(readlink -f -- "$_couch_data/current" 2>/dev/null)" || _couch_rel=""
        _couch_ver=""
        if [[ -n "$_couch_rel" && -f "$_couch_rel/VERSION" ]]; then
            read -r _couch_ver <"$_couch_rel/VERSION" 2>/dev/null || :
        fi
        _couch_id="${_couch_ver:-0}@${_couch_rel##*/}"

        # Sicherung fuse-launcher (Supervisor hat aufgegeben, fünf kurze Sitzungen): gilt für die Fassung aus
        # ihrer ersten Zeile (leer = immer), bis "couch-session --reset" läuft.
        if [[ $_couch_on == 1 && -e "$_couch_state/fuse-launcher" ]]; then
            _couch_fuse=""
            read -r _couch_fuse <"$_couch_state/fuse-launcher" 2>/dev/null || :
            if [[ -z "$_couch_fuse" || "$_couch_fuse" == "$_couch_id" ]]; then _couch_on=0; fi
        fi

        COUCH_SESSION_ACTIVE=0
        COUCH_SESSION_RELEASE="$_couch_id"
        if [[ $_couch_on == 1 ]]; then
            COUCH_FALLBACK_CMD="$CLIENTCMD"
            CLIENTCMD="$_couch_entry"
            COUCH_SESSION_ACTIVE=1
        fi
    fi

    # Fünf Sitzungen unter 60 s: Die Stock-Fassung von short_session_recover entpackt Steams Archiv und
    # wechselt in den Desktop-Modus. Erste Stufe ist hier nur die Sicherung (nächste Sitzung = reines Steam).
    if ! declare -F _couch_stock_short_session_recover >/dev/null 2>&1; then
        _couch_src="$(declare -f short_session_recover 2>/dev/null)" || _couch_src=""
        if [[ -n "$_couch_src" ]]; then
            eval "_couch_stock_${_couch_src}" 2>/dev/null || :
        fi
    fi
    if declare -F _couch_stock_short_session_recover >/dev/null 2>&1; then
        short_session_recover() {
            local state="${HOME:-}/.local/state/couch"
            local tracker="${short_session_tracker_file:-/tmp/chimeraos-short-session-tracker}"
            if [[ "${COUCH_SESSION_ACTIVE:-0}" == 1 && -n "${HOME:-}" ]] \
                && grep -qv '^gamescope failed' "$tracker" 2>/dev/null \
                && mkdir -p "$state" 2>/dev/null \
                && printf '%s\n%s\n' "${COUCH_SESSION_RELEASE:-}" "$(date '+%F %T' 2>/dev/null) fünf kurze Sitzungen" 2>/dev/null >"$state/fuse-launcher"; then
                echo "pc5-launcher: fünf kurze Sitzungen, Sicherung fuse-launcher gesetzt; die nächste Sitzung startet reines Steam" >&2
                return 0
            fi
            _couch_stock_short_session_recover
        }
    fi
    unset _couch_home _couch_data _couch_entry _couch_state _couch_on _couch_rel _couch_ver _couch_id _couch_fuse _couch_src
}
_couch_gsp_hook || :
unset -f _couch_gsp_hook 2>/dev/null || :
