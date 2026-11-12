# ADR 0003 — Pairing by one-time code, mDNS discovery, tray app and installer

- **Status:** accepted (owner request, 2026-11-12)

## Decisions

1. **Discovery:** the agent advertises `_pcdeck._tcp.local.` (zeroconf 0.154.0) on its LAN IPv4 with
   TXT `id` (random agent id), `name`, `v`. The deck browses with ESPmDNS. Discovery grants nothing.
   If a paired PC changes IP (DHCP), the deck re-locates it by `id` after 3 failed connects.
2. **Pairing:** the PC app shows a 6-digit code (`secrets`), single use, 120 s, max 5 wrong tries.
   The deck sends `pair.request{code,name}`; the agent answers `pair.result{token}` with a 256-bit
   random token per device. The agent stores only `sha256(token)` in `%LOCALAPPDATA%\PcControlDeck\devices.dat`,
   encrypted with DPAPI (current user). The deck stores the token in NVS (`pcd_net`, unencrypted
   flash: physical-access risk documented in SECURITY.md).
3. **Authentication:** every connection: `hello` → `auth.request{token}` → `auth.result`. No telemetry,
   no capabilities details, no commands before authentication. Per-peer failure rate limit (10/min)
   and per-session limit (5).
4. **Tray app:** PySide6 6.12.0. Closing the window keeps it in the tray; "Quit" stops it. Single
   instance via `QLocalServer`. Strings ES/EN by Windows display language.
5. **Elevation without UAC prompts:** the installer (admin) registers two scheduled tasks for the
   installing user with *highest privileges*: an on-demand task (the exe relaunches itself through
   it when started unelevated) and a logon task (`--minimized`), which is the "Start with Windows"
   switch. Elevation is needed for CPU temperature (Lenovo EC via WMI / LibreHardwareMonitor).
6. **Installer:** PyInstaller 6.22.3 one-folder bundle + Inno Setup 6 (`PrivilegesRequired=admin`).
   Optional tasks (checked by default, user can untick): start with Windows; firewall rules
   **private profile only**, program-scoped, TCP 8765 + UDP 5353. Uninstall removes tasks and rules.

## Consequences

- Plain `ws://` remains: pairing protects *who* may read/control, not confidentiality on the LAN.
  TLS (`wss://` with pinned certificate) stays on the roadmap.
- If a non-admin user installs with another account's admin credentials, the tasks are created for
  the admin account. Documented limitation.
