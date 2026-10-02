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
rented() {
  # A live buyer rental = a running container the agent labelled with the rental's VM id (or a
  # legacy petabyte-vm-* VM). Local docker only: this window never calls the platform.
  [ -n "$(timeout 10 docker ps -q --filter label=pb.vm_id 2>/dev/null)$(timeout 10 docker ps -q --filter name=petabyte-vm- 2>/dev/null)" ]
}
while :; do
  case "$(systemctl is-active petabyte-agent 2>/dev/null)" in
    active) if rented; then s="RENTED - earning now / مؤجّر ويكسب الحين"
            else s="online - waiting for rentals / متصل - ينتظر مستأجر"; fi ;;
    activating) s="starting..." ;;
    *) s="AGENT STOPPED - run the Resume command above" ;;
  esac
  printf '\r  Status (%s): %-60s' "$(date +%H:%M)" "$s"
  sleep 30
done
