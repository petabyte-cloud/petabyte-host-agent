#!/usr/bin/env bash
# Windows only: the "PetabyteNode" logon task runs this inside the Petabyte WSL distro. While this
# process lives, WSL (and the agent's systemd service) stays up; when its window is closed, WSL
# shuts down and the node goes offline. Windows Terminal shows it as a tab, so it says what it is
# instead of being a blank `wsl.exe` window that sellers close.
API=$(sed -n 's/^PETABYTE_API_URL=//p' /etc/petabyte/agent.env 2>/dev/null | tail -n 1)
API=${API:-https://petabyte.market}
printf '\033]0;Petabyte node - keep open\007'
cat <<EOF

  Petabyte node is running - keep this window open (you can minimise it).
  Closing it takes your GPU offline and stops your earnings.

  جهازك شغّال على بيتابايت - خلّ هذي النافذة مفتوحة (تقدر تصغّرها).
  إغلاقها يوقف جهازك ويوقف أرباحك.

  Node offline? Don't reinstall. In an Administrator PowerShell run:
      irm $API/manage.ps1 | iex
  and choose 1 (Resume).

EOF
while :; do
  case "$(systemctl is-active petabyte-agent 2>/dev/null)" in
    active) s="online - agent running" ;;
    activating) s="starting..." ;;
    *) s="AGENT STOPPED - run the Resume command above" ;;
  esac
  printf '\r  Status (%s): %-48s' "$(date +%H:%M)" "$s"
  sleep 30
done
