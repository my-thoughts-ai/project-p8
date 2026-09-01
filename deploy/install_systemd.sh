#!/usr/bin/env bash
# Install the Manaslu alerter as systemd *user* timers on this machine.
#
#   ./deploy/install_systemd.sh            install and start
#   ./deploy/install_systemd.sh --uninstall  stop and remove
#
# User timers (not system ones) are used deliberately: no root is needed, and the
# service reads the .env sitting in your home directory. `loginctl enable-linger`
# is what keeps them running when you are not logged in.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"
UNITS=(manaslu-digest.service manaslu-digest.timer manaslu-danger.service manaslu-danger.timer)

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "Stopping and removing Manaslu timers..."
  systemctl --user disable --now manaslu-digest.timer manaslu-danger.timer 2>/dev/null || true
  for unit in "${UNITS[@]}"; do rm -f "${UNIT_DIR}/${unit}"; done
  systemctl --user daemon-reload
  echo "Removed. The database in data/ was left untouched."
  exit 0
fi

# --- locate an interpreter that actually has the dependencies -----------------
if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  PYTHON="${PROJECT_DIR}/.venv/bin/python"
elif [[ -x "${PROJECT_DIR}/../.venv/bin/python" ]]; then
  PYTHON="$(cd "${PROJECT_DIR}/.." && pwd)/.venv/bin/python"
else
  PYTHON="$(command -v python3)"
fi
echo "Using interpreter: ${PYTHON}"

if ! "${PYTHON}" -c "import httpx, pandas, yaml, jinja2" 2>/dev/null; then
  echo "ERROR: ${PYTHON} is missing dependencies. Run:" >&2
  echo "  ${PYTHON} -m pip install -r ${PROJECT_DIR}/requirements.txt" >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  echo "WARNING: no .env found. The service will run but every channel stays disabled."
  echo "         cp .env.example .env  and fill it in."
fi

# --- install ------------------------------------------------------------------
mkdir -p "${UNIT_DIR}"
for unit in "${UNITS[@]}"; do
  sed -e "s|__PROJECT_DIR__|${PROJECT_DIR}|g" \
      -e "s|__PYTHON__|${PYTHON}|g" \
      "${PROJECT_DIR}/deploy/systemd/${unit}" > "${UNIT_DIR}/${unit}"
  echo "  installed ${unit}"
done

# EnvironmentFile is added here rather than in the template so the absolute path
# is correct regardless of where the repo lives.
for svc in manaslu-digest.service manaslu-danger.service; do
  sed -i "/^\[Service\]/a EnvironmentFile=-${PROJECT_DIR}/.env" "${UNIT_DIR}/${svc}"
done

systemctl --user daemon-reload
systemctl --user enable --now manaslu-digest.timer manaslu-danger.timer

# Timers only fire while a user session exists unless lingering is enabled.
if ! loginctl show-user "${USER}" 2>/dev/null | grep -q "Linger=yes"; then
  echo
  echo "Enabling lingering so the timers survive logout (may prompt for sudo):"
  sudo loginctl enable-linger "${USER}" || \
    echo "  Could not enable lingering. Timers will only run while you are logged in."
fi

echo
echo "Installed. Next scheduled runs:"
systemctl --user list-timers 'manaslu-*' --no-pager
echo
echo "Logs:   journalctl --user -u manaslu-digest.service -f"
echo "Status: ${PYTHON} -m src.main --mode status   (from ${PROJECT_DIR})"
