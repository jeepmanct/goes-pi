#!/bin/sh
# Hourly: append panel (goes-webui) uptime and RSS to ~/goes/panel-mem.log
pid=$(systemctl show -p MainPID --value goes-webui)
[ -n "$pid" ] && [ "$pid" != 0 ] || exit 0
echo "$(date -Is) pid=$pid $(ps -o etime=,rss= -p "$pid" | awk '{print "up="$1" rss_mb="int($2/1024)}')" >> "$HOME/goes/panel-mem.log"
