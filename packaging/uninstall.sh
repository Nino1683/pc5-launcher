#!/usr/bin/env bash
# uninstall.sh – PC5 Launcher entfernen (E104 b):
#
#   bash uninstall.sh [--yes] [--purge-data] [--dry-run]
#
# Entfernt den eigenen Haken (nur, wenn er unverändert ist), die Desktop-Einträge und die Fassungen. Einstellungen
# bleiben, außer mit --purge-data (dann in den Papierkorb). Spiele, Steam, Proton und Flatpaks fasst es nie an.
# Nie sudo, nie ein Kennwort.
set -u
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
  en*) need="uninstall.sh: Python 3.10 or newer (python3) is missing." ;;
  *)   need="uninstall.sh: Python 3.10 oder neuer (python3) fehlt." ;;
esac
py="$(command -v python3 2>/dev/null)" || py=""
if [[ -z "$py" ]] || ! "$py" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "$need" >&2
  exit 1
fi
cd -- "${HOME:-/}" 2>/dev/null || cd /     # nicht in einer Fassung stehen bleiben, die gleich entfernt wird
exec "$py" -I "$here/installer/pc5_install.py" uninstall "$@"
