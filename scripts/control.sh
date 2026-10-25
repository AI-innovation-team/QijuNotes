#!/usr/bin/env bash
set -euo pipefail
dsh_notify_unit=()
if [[ -f "$HOME/.config/systemd/user/dsh-personal-notify.service" ]]; then
 dsh_notify_unit=(dsh-personal-notify.service)
fi
case "${1:-status}" in
 start) systemctl --user start dsh-personal.service dsh-personal-host.service dsh-personal-backup.timer dsh-personal-ledger.timer "${dsh_notify_unit[@]}" ;;
 stop) systemctl --user stop dsh-personal-backup.timer dsh-personal-ledger.timer dsh-personal.service dsh-personal-host.service "${dsh_notify_unit[@]}" ;;
 restart) systemctl --user restart dsh-personal.service dsh-personal-host.service "${dsh_notify_unit[@]}" ;;
 status) systemctl --user status dsh-personal.service dsh-personal-host.service dsh-personal-backup.timer dsh-personal-ledger.timer "${dsh_notify_unit[@]}" --no-pager ;;
 account) shift; python3 "$(dirname "$0")/account.py" "$@" ;;
 token) if [[ -f "$HOME/.local/share/dsh-personal/account-auth.json" ]]; then
  echo "已启用账号密码；忘记密码请运行 scripts/control.sh account reset。"
 else cat "$HOME/.local/share/dsh-personal/access-token"; fi ;;
 logs) journalctl --user -u dsh-personal.service -u dsh-personal-host.service -n 80 --no-pager ;;
 *) echo 'Usage: control.sh start|stop|restart|status|account|token|logs'; exit 2 ;;
esac
