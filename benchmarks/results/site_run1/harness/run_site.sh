#!/bin/bash
# run_site.sh NAME: start the Antigravity site session detached-friendly; writes NAME.pid / NAME.exit next to the log.
S=<scratch>; D=$S/sj/agy_site; name=$1
echo $$ > $D/$name.pid
K="$GEMINI_API_KEY"; [ -n "$K" ] || { echo "no key"; echo 2 > $D/$name.exit; exit 2; }
tree=$S/sj/live_tree; cd $tree || exit 3
. <repository>/.venv/bin/activate; export PYTHONPATH=$tree/src PYTHONDONTWRITEBYTECODE=1
. $S/r3/engines_env.sh
export GEMINI_API_KEY="$K"; unset GOOGLE_GEMINI_BASE_URL GOOGLE_API_KEY
echo "start $(date -u +%FT%TZ) tree $(git rev-parse --short HEAD) load $(cut -d' ' -f1-3 /proc/loadavg)"
timeout 12000 xvfb-run -a python $D/run_site.py $D/$name $D/$name/work
rc=$?
echo "end $(date -u +%FT%TZ) rc=$rc load $(cut -d' ' -f1-3 /proc/loadavg)"
echo $rc > $D/$name.exit
