"""Entry point shared by the repair-upload BAT launcher and direct CLI usage."""

from robocap_rerun_tools.repair_upload import main

if __name__ == "__main__":
    raise SystemExit(main())
