#!/bin/bash
# Retention for ~/goes/data (run from cron):
#   1. raw full-disk / mesoscale PNGs (+ .json headers) older than RAW_DAYS are deleted;
#      the rendered views (globe, regions, fog) are kept much longer
#   2. EMWIN graphics older than EMWIN_DAYS are deleted
#   3. EMWIN text older than EMWIN_DAYS is deleted unless its issuing office is in keep-offices.txt
#   4. backstop: if the whole data dir reaches 800 GB, delete the oldest files until it is back under
#      (the backup/ folder of state archives is never deleted by this)
DATA=${GOES_DATA:-/home/pi/goes/data}
KEEP=${GOES_KEEP:-/home/pi/goes/keep-offices.txt}
RAW_DAYS=${RAW_DAYS:-180}
EMWIN_DAYS=${EMWIN_DAYS:-3}
TRIGGER=${TRIGGER:-$((800*1000*1000*1000))}
LOCK=${GOES_LOCK:-/tmp/goes-prune.lock}
exec 9>"$LOCK"; flock -n 9 || exit 0

n_raw=0 n_emwin=0
for r in fd m1 m2; do
  if [ -d "$DATA/goes19/$r" ]; then
    c=$(find "$DATA/goes19/$r" -type f \( -name '*.png' -o -name '*.json' \) -mtime +"$RAW_DAYS" -print -delete | wc -l)
    n_raw=$((n_raw + c))
  fi
done

if [ -d "$DATA/emwin" ]; then
  n_emwin=$(find "$DATA/emwin" -type f ! -iname '*.txt' -mtime +"$EMWIN_DAYS" -print -delete | wc -l)
  declare -A keep
  while read -r code _; do [[ $code =~ ^[A-Z]{4}$ ]] && keep[$code]=1; done < "$KEEP"
  re='^[AZ]_[A-Z]{4}[0-9]{2}([A-Z]{4})[0-9]{6}_'
  while IFS= read -r -d '' f; do
    n=${f##*/}
    if [[ $n =~ $re ]] && [ -n "${keep[${BASH_REMATCH[1]}]}" ]; then continue; fi
    rm -f -- "$f" && n_emwin=$((n_emwin + 1))
  done < <(find "$DATA/emwin" -type f -iname '*.txt' -mtime +"$EMWIN_DAYS" -print0)
fi

[ $((n_raw + n_emwin)) -gt 0 ] && logger -t goes-prune "removed $n_raw raw scan files and $n_emwin EMWIN files (older than ${RAW_DAYS} d / ${EMWIN_DAYS} d)"

used=$(du -sb "$DATA" | cut -f1)
if [ "$used" -ge "$TRIGGER" ]; then
  find "$DATA" -path "$DATA/backup" -prune -o -type f -printf '%T@ %s %p\n' | sort -n | while read -r _ size path; do   # never the state backups
    [ "$used" -lt "$TRIGGER" ] && break
    rm -f -- "$path" && used=$((used - size))
  done
  logger -t goes-prune "size cap reached; pruned oldest files, now $((used/1000000000)) GB"
fi
find "$DATA" -mindepth 1 -type d -empty -delete
