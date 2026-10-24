#!/usr/bin/env bash
# install.sh – PC5 Launcher installieren oder aktualisieren (E104 b). Im Desktop-Modus im entpackten Ordner:
#
#   bash install.sh [--yes] [--hook|--no-hook] [--dry-run] [--no-network] [--status] [--rollback] [--adopt]
#
# Arbeitet nur unter deinem persönlichen Ordner: ruft nie sudo, pkexec, steamos-readonly, pacman, rpm-ostree
# oder ujust auf und fragt nie nach einem Kennwort. Prüft nur, dass python3 ab 3.10 da ist, und übergibt dann
# an installer/pc5_install.py (README.md).
set -u
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)" || exit 1
case "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" in
  en*) need="install.sh: Python 3.10 or newer (python3) is missing. Install it with your system's package manager." ;;
  *)   need="install.sh: Python 3.10 oder neuer (python3) fehlt. Installiere es über die Paketverwaltung deines Systems." ;;
esac
py="$(command -v python3 2>/dev/null)" || py=""
if [[ -z "$py" ]] || ! "$py" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "$need" >&2
  exit 1
fi
exec "$py" -I "$here/installer/pc5_install.py" install "$@"
