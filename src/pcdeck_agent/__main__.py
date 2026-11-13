"""Control Deck agent CLI (Windows, Linux, macOS).

  python -m pcdeck_agent sample [--count 5] [--interval 1.0] [--fake]
  python -m pcdeck_agent diag   [--out diag.json] [--fake]
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import time
from dataclasses import asdict

from . import __version__
from .hardware.catalog import diagnostic_report
from .hardware.mapper import MapperConfig, SensorMapper
from .hardware.provider import HardwareMetricsProvider

log = logging.getLogger("pcdeck_agent")


def create_provider(fake: bool) -> HardwareMetricsProvider:
    if fake:
        from .hardware.demo_frames import demo_provider

        return demo_provider()
    from .platforms import hardware_provider

    try:
        return hardware_provider()  # Windows: LibreHardwareMonitor; Linux: sysfs/hwmon; macOS: IOKit
    except RuntimeError as e:
        raise SystemExit(str(e)) from None


def short_host() -> str:
    return socket.gethostname().split(".")[0][:23]


def cmd_sample(args: argparse.Namespace) -> int:
    provider = create_provider(args.fake)
    provider.open()
    mapper = SensorMapper(MapperConfig(host=short_host()))
    try:
        status = provider.status()
        for n in status.notes:
            log.warning("limitation: %s", n)
        provider.read()  # LHM load sensors need a first sample
        for i in range(args.count):
            time.sleep(args.interval)
            t0 = time.perf_counter()
            snap = mapper.map(provider.read(), provider.os_memory(), status.notes)
            dt = (time.perf_counter() - t0) * 1000
            d = asdict(snap)
            if not args.full:
                d.pop("sensors")
            print(json.dumps(d, default=str, ensure_ascii=False))
            log.debug("sample %d took %.1f ms", i, dt)
    finally:
        provider.close()
    return 0


def cmd_diag(args: argparse.Namespace) -> int:
    provider = create_provider(args.fake)
    provider.open()
    try:
        provider.read()
        time.sleep(1.0)
        raw = provider.read()
        status = provider.status()
        snap = SensorMapper(MapperConfig(host=short_host())).map(raw, provider.os_memory(), status.notes)
        report = diagnostic_report(status, raw, snap)
        report["agent_version"] = __version__
    finally:
        provider.close()
    text = json.dumps(report, indent=2, default=str, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"diagnostic written to {args.out} ({report['sensor_count']} sensors)")
    else:
        print(text)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import threading

    from .config import load_config, log_dir
    from .logging_setup import setup_logging
    from .runtime import AgentRuntime

    cfg, cfg_path = load_config()
    log_path = setup_logging("DEBUG" if args.verbose else cfg.log_level, log_dir())
    if args.bind:
        cfg.bind = [args.bind]
    if args.port:
        cfg.port = args.port
    if args.no_mdns:
        cfg.mdns = False
    log.info("pcdeck-agent %s, config %s, log %s", __version__, cfg_path, log_path)
    rt = AgentRuntime(cfg, lambda: create_provider(args.fake))
    rt.on_paired = lambda cid, name: log.info("paired new device %s (%s)", name, cid)
    if args.pair:
        rt.on_pairing = lambda st: st.active and print(f"\n  PAIRING CODE: {st.code}  (valid 2 minutes, type it on the deck)\n", flush=True)
    rt.start()
    if args.pair:
        rt.start_pairing()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log.info("stopping")
        rt.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pcdeck_agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="print normalized snapshots as JSON lines")
    s.add_argument("--count", type=int, default=5)
    s.add_argument("--interval", type=float, default=1.0)
    s.add_argument("--full", action="store_true", help="include the compact sensor list")
    s.add_argument("--fake", action="store_true", help="synthetic provider, no hardware access")
    s.set_defaults(func=cmd_sample)
    d = sub.add_parser("diag", help="redacted diagnostic report (catalog + selection + limitations)")
    d.add_argument("--out")
    d.add_argument("--fake", action="store_true")
    d.set_defaults(func=cmd_diag)
    sv = sub.add_parser("serve", help="run the WebSocket telemetry server (LAN only)")
    sv.add_argument("--bind", help="override bind address (default: config 'auto' = LAN IPv4 + loopback)")
    sv.add_argument("--port", type=int)
    sv.add_argument("--fake", action="store_true")
    sv.add_argument("--pair", action="store_true", help="print a one-time pairing code at start")
    sv.add_argument("--no-mdns", action="store_true", help="do not advertise over mDNS (side-by-side dev runs)")
    sv.set_defaults(func=cmd_serve)
    args = p.parse_args(argv)
    if args.cmd == "serve":
        return args.func(args)  # configures its own logging (console + rotating file)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
