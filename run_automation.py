"""
ServiceNow-automation starten vanuit PyCharm.

Standaard is alles een PROEFRUN: de bot vult alles in maar slaat NIETS op.
Pas als je UITVOEREN = True zet (of --uitvoeren meegeeft) wordt er echt opgeslagen.

Opdrachten (Nederlands of Engels):
    voeg PC-0042 toe aan INC0010001                    -> veld Configuration item
    voeg PC-0042 toe aan INC0010001 als affected ci    -> related list Affected CIs
    zet state van INC0010001 op In Progress            -> willekeurig veld (label of naam)
    installeer bs-26-1244 voor SCTASK0194511           -> Status Installed (+ Type Monitor bij een monitor)
                                                          + cost center van de taak

Of een CSV (bijv. opgeslagen uit Excel) met kolommen:  melding;pc;modus

Vanaf de opdrachtregel kan ook:
    python run_automation.py "voeg PC-0042 toe aan INC0010001" --uitvoeren
    python run_automation.py --csv koppelingen.csv --affected --uitvoeren
    python run_automation.py --help

Oefenen zonder risico: start eerst  python tests/mock_servicenow.py
en zet INSTANCE op http://127.0.0.1:8765  (login admin / admin).
"""
import os
import sys

from servicenow_smart_locator.automation import main

INSTANCE = os.environ.get("SN_INSTANCE", "https://dev12345.service-now.com")  # <- jouw instance

OPDRACHTEN = [
    "voeg PC-0042 toe aan INC0010001",
    # "voeg PC-0042 toe aan INC0010001 als affected ci",
    # "zet state van INC0010001 op In Progress",
]
CSV_BESTAND = None        # bijv. "koppelingen.csv"
UITVOEREN = False         # False = proefrun, True = echt opslaan
EXTRA = []                # bijv. ["--browser", "msedge"] of ["--polaris"]

if __name__ == "__main__":
    if len(sys.argv) > 1:                       # opdrachtregel heeft voorrang
        args = sys.argv[1:]
    else:
        args = list(OPDRACHTEN) + (["--csv", CSV_BESTAND] if CSV_BESTAND else []) \
               + (["--uitvoeren"] if UITVOEREN else []) + EXTRA
    if "--instance" not in args:
        args += ["--instance", INSTANCE]
    sys.exit(main(args))
