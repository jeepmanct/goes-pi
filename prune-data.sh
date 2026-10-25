#!/bin/bash
# Retention for ~/goes/data (run from cron):
#   1. raw full-disk / mesoscale PNGs (+ .json headers) older than RAW_DAYS are deleted;
#      the rendered views (globe, regions, fog) are kept much longer
#   2. EMWIN graphics older than EMWIN_DAYS are deleted
#   3. EMWIN text older than EMWIN_DAYS is deleted unless its issuing office is in keep-offices.txt
#   4. backstop: if the whole data dir reaches 190 GB, delete the oldest files until it is back under
DATA=${GOES_DATA:-/home/pi/goes/data}
KEEP=${GOES_KEEP:-/home/pi/goes/keep-offices.txt}
RAW_DAYS=${RAW_DAYS:-7}
EMWIN_DAYS=${EMWIN_DAYS:-3}
TRIGGER=${TRIGGER:-$((190*1000*1000*1000))}
LOCK=${GOES_LOCK:-/tmp/goes-prune.lock}
exec 9>"$LOCK"; flock -n 9 || exit 0

for r in fd m1 m2; do
  [ -d "$DATA/goes19/$r" ] && find "$DATA/goes19/$r" -type f \( -name '*.png' -o -name '*.json' \) -mtime +"$RAW_DAYS" -delete
done

if [ -d "$DATA/emwin" ]; then
  find "$DATA/emwin" -type f ! -iname '*.txt' -mtime +"$EMWIN_DAYS" -delete
  declare -A keep
  while read -r code _; do [[ $code =~ ^[A-Z]{4}$ ]] && keep[$code]=1; done < "$KEEP"
  re='^[AZ]_[A-Z]{4}[0-9]{2}([A-Z]{4})[0-9]{6}_'
  while IFS= read -r -d '' f; do
    n=${f##*/}
    if [[ $n =~ $re ]] && [ -n "${keep[${BASH_REMATCH[1]}]}" ]; then continue; fi
    rm -f -- "$f"
  done < <(find "$DATA/emwin" -type f -iname '*.txt' -mtime +"$EMWIN_DAYS" -print0)
fi

used=$(du -sb "$DATA" | cut -f1)
if [ "$used" -ge "$TRIGGER" ]; then
  find "$DATA" -path "$DATA/backup" -prune -o -type f -printf '%T@ %s %p\n' | sort -n | while read -r _ size path; do   # never the state backups
    [ "$used" -lt "$TRIGGER" ] && break
    rm -f -- "$path" && used=$((used - size))
  done
  logger -t goes-prune "size cap reached; pruned oldest files, now $((used/1000000000)) GB"
fi
find "$DATA" -mindepth 1 -type d -empty -delete
