#!/usr/bin/env bash
# Live AiCam processing monitor — refreshes every 2 seconds
# Usage: ./scripts/watch.sh [backend_url]
#   e.g. ./scripts/watch.sh http://localhost:8100

URL="${1:-http://localhost:8100}"
ENDPOINT="$URL/api/native/live"

clear
echo "🎥 AiCam Live Monitor — $URL (Ctrl+C to quit)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

while true; do
  DATA=$(curl -s --max-time 3 "$ENDPOINT" 2>/dev/null)
  if [ $? -ne 0 ] || [ -z "$DATA" ]; then
    tput cup 3 0 2>/dev/null
    echo "❌ Backend not reachable at $URL"
    sleep 2
    continue
  fi

  # Parse JSON with python (available in venv)
  OUTPUT=$(python3 -c "
import json, sys
d = json.loads(sys.stdin.read())
now = d['now']
state = d['state']
q = d['queue_depth']
alive = '✓' if d['worker_alive'] else '✗'
total = d['total_clips_today']
ago = d.get('last_clip_ago_sec')
ago_str = f'{ago:.0f}s ago' if ago and ago < 120 else f'{ago/60:.1f}m ago' if ago else 'n/a'

print(f'  🕐 {now}   State: {\"🟢 \" + state if state==\"processing\" else \"⏸  \" + state}')
print(f'  Worker: {alive}   Queue: {q}   Clips today: {total}   Last clip: {ago_str}')
print()

# Recent detections
dets = d.get('recent_detections', {})
if dets:
    det_line = '  🔍 Recent objects: ' + ', '.join(f'{k}:{v}' for k,v in sorted(dets.items(), key=lambda x:-x[1])[:8])
    print(det_line)
else:
    print('  🔍 No detections in recent clips')
print()

# Recent clips table
clips = d.get('recent_clips', [])
if clips:
    print('  ┌──────┬────────────────────┬────────┬────────┐')
    print('  │ Clip │ Time               │ Status │ Frames │')
    print('  ├──────┼────────────────────┼────────┼────────┤')
    for c in clips:
        cid = str(c['id']).rjust(4)
        t = c.get('start_iso','')
        if len(t) > 19: t = t[11:19]
        elif len(t) > 10: t = t[11:19]
        else: t = t[:19]
        st = c.get('status','?')[:6].ljust(6)
        fr = str(c.get('sampled_frames',0)).rjust(4)
        print(f'  │ {cid} │ {t:>18} │ {st} │ {fr}   │')
    print('  └──────┴────────────────────┴────────┴────────┘')

# Processing
proc = d.get('processing', [])
if proc:
    print()
    print('  ⚙️  Currently processing: ' + ', '.join(f\"#{p['id']}\" for p in proc))
" <<< "$DATA" 2>/dev/null)

  # Move cursor to line 3 and overwrite
  tput cup 3 0 2>/dev/null
  # Clear from here to bottom
  tput ed 2>/dev/null
  echo "$OUTPUT"
  echo ""
  echo "  ─── refreshing every 2s ───"

  sleep 2
done
