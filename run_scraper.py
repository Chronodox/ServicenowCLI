"""
Scrape een ServiceNow-pagina naar JSON (vanuit PyCharm: Run 'run_scraper').

    python run_scraper.py INC0010001          # een record via nummer
    python run_scraper.py incident_list.do    # een lijst
    python run_scraper.py https://.../...     # een volledige URL

Je kunt ook in de inspector (run_inspector.py) op F9 drukken om de pagina te
scrapen die je op dat moment open hebt.
"""
import os
import sys

from servicenow_smart_locator.scraper import main

INSTANCE = os.environ.get("SN_INSTANCE", "https://dev12345.service-now.com")  # <- jouw instance
DOEL = "INC0010001"

if __name__ == "__main__":
    args = sys.argv[1:] or [DOEL]
    main([INSTANCE] + args)
