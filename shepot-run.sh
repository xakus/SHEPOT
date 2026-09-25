#!/bin/bash
source "$HOME/venvs/shepot/bin/activate"
SITE=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
export LD_LIBRARY_PATH="$(ls -d $SITE/nvidia/*/lib 2>/dev/null | paste -sd:)${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec python3 "$HOME/bin/shepot.py"
