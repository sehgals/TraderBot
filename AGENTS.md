# TraderBot Repository Rules

## Windows service identity

- The production bot and its watchdog must always run through the `TraderBot_Watcher_Supervisor` scheduled task as the Windows `SYSTEM` service account.
- The task principal must use `ServiceAccount` logon with highest privileges; never configure it with `Interactive`, `InteractiveOrPassword`, or a user-session trigger.
- After changing task setup, startup, monitoring, or supervisor code, verify the scheduled-task principal and verify the running `pythonw.exe` supervisor is in Windows service session 0.
- Use `scripts/configure_watcher_supervisor_task.ps1` to enforce this configuration. Do not launch the production supervisor manually under a signed-in user account.

