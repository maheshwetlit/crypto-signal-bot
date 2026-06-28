#!/usr/bin/env python3
"""Run the crypto signal bot directly."""
import subprocess, os, sys

# This script lives in C:\Users\mahes\AppData\Local\hermes\scripts\
# The bot lives in C:\Users\mahes\AppData\Local\hermes\hermes-agent\
AGENT_DIR = r'C:\Users\mahes\AppData\Local\hermes\hermes-agent'

# Ensure token files are available in the agent dir
for _f in [".gh_token", ".tg_token"]:
    _src = os.path.join(os.path.dirname(os.path.abspath(__file__)), _f)
    _dst = os.path.join(AGENT_DIR, _f)
    if os.path.exists(_src) and not os.path.exists(_dst):
        import shutil
        shutil.copy2(_src, _dst)

# Run the bot
result = subprocess.run(
    [sys.executable, os.path.join(AGENT_DIR, 'crypto_signal_bot.py')],
    capture_output=True, text=True, timeout=300, cwd=AGENT_DIR
)
print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)
if result.returncode != 0:
    print('STDERR:', result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr)
