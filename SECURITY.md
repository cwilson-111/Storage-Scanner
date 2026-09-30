# Security

## Reporting a problem

Please don't open a public issue for a security problem. Report it
privately through GitHub: the repository's **Security** tab ▸ **Report a
vulnerability**. Include what you found, how to reproduce it, and which
version you ran (Tools ▸ Help ▸ Copy Diagnostic Info gives it without any
file names).

Security fixes go into the newest release; older versions don't get
updates.

## What counts

Storage Scanner deletes files, asks for administrator rights for Turbo
Scan, and runs scheduled scans, so these matter most:

- anything that deletes, moves or overwrites something the user didn't
  choose, or skips the Recycle Bin/Trash without asking;
- the elevated Turbo Scan helper doing more than reading the volume and
  writing its result file;
- a network request other than the update check described in
  [PRIVACY.md](PRIVACY.md), or data leaving the machine.
