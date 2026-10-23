"""
Start de visuele inspector vanuit PyCharm (rechtermuisknop > Run 'run_inspector').

Zet je instance-URL hieronder, of geef hem mee als argument / via SN_INSTANCE.
Extra opties: --browser msedge   (gebruik de geïnstalleerde Edge)
"""
import os
import sys

from servicenow_smart_locator.inspector import main

INSTANCE = os.environ.get("SN_INSTANCE", "https://amsterdamumc.service-now.com/")  # <- jouw instance

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        args = [INSTANCE] + args
    main(args)
