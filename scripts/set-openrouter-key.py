#!/usr/bin/env python3
"""set-openrouter-key.py -- kept so older instructions still work; the same as:

    python3 set-backup-key.py openrouter
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.execv(sys.executable, [sys.executable, os.path.join(HERE, "set-backup-key.py"), "openrouter"])
