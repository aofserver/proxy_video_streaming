#!/bin/sh
# probe.sh MASTER_URL — โชว์ความละเอียด / เสียง / ซับ ที่มีใน master (อ่านง่าย)
[ -z "$1" ] && { echo "ใช้: ./probe.sh \"MASTER_URL\""; exit 1; }

curl -s "$1" \
  -H "Origin: https://zmdb.net" \
  -H "Referer: https://zmdb.net/" \
  -H "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36" \
| awk '
function grab(s, key,   m) { if (match(s, key "=\"[^\"]*\"")) { m=substr(s,RSTART,RLENGTH); sub(key "=\"","",m); sub(/"$/,"",m); return m } return "" }
/#EXT-X-STREAM-INF/ {
  res=""; if (match($0,/RESOLUTION=[0-9]+x[0-9]+/)) res=substr($0,RSTART+11,RLENGTH-11)
  bw="";  if (match($0,/BANDWIDTH=[0-9]+/))       bw=substr($0,RSTART+10,RLENGTH-10)
  fr="";  if (match($0,/FRAME-RATE=[0-9.]+/))     fr=substr($0,RSTART+11,RLENGTH-11)
  vids[++nv]=sprintf("  %-11s  %sfps  ~%.1f Mbps", res, fr, bw/1000000)
}
/#EXT-X-MEDIA/ && /TYPE=AUDIO/ {
  lang=grab($0,"LANGUAGE"); name=grab($0,"NAME"); def=($0 ~ /DEFAULT=YES/)?" (default)":""
  auds[++na]=sprintf("  %-6s %s%s", lang, name, def)
}
/#EXT-X-MEDIA/ && /TYPE=SUBTITLES/ {
  lang=grab($0,"LANGUAGE"); name=grab($0,"NAME"); def=($0 ~ /DEFAULT=YES/)?" (default)":""
  subs[++ns]=sprintf("  %-6s %s%s", lang, name, def)
}
END {
  print "🎬 วิดีโอ (" nv "):";        for(i=1;i<=nv;i++) print vids[i]
  print "\n🔊 เสียง (" na "):";        for(i=1;i<=na;i++) print auds[i]; if(na==0) print "  (ไม่มี)"
  print "\n💬 ซับ (" ns "):";          for(i=1;i<=ns;i++) print subs[i]; if(ns==0) print "  (ไม่มี)"
}'
