"""
ServiceNow-opdrachtregel. Start in PyCharm (Run 'sn') of in de terminal:

    python sn.py
    sn PROEF -> taak SCTASK0194511
    sn PROEF SCTASK0194511> lever bs-26-1244 pc-26-4455
    sn PROEF SCTASK0194511> echt
    sn ECHT  SCTASK0194511> lever bs-26-1244 pc-26-4455

Eén opdracht zonder de opdrachtregel te openen:
    python sn.py lever SCTASK0194511 bs-26-1244 pc-26-4455
    python sn.py --echt lever SCTASK0194511 bs-26-1244 pc-26-4455

De eerste keer vraagt hij het adres van jullie ServiceNow en maakt hij instellingen.ini.
"""
import sys

from servicenow_smart_locator.shell import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
