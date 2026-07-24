"""Optional Cloudflare Quick Tunnel: reach the dashboard from your phone.

Free, no Cloudflare account, no port forwarding. We run the `cloudflared`
binary as a subprocess (like the gosom provider) and parse the public
https://<random>.trycloudflare.com URL out of its output.

Research note (repo rule 6): cloudflared is the only maintained free option
with no account and no signup (ngrok's free tier now requires an account and
rotates URLs per session too). Alternatives considered and skipped: localtunnel
(npm dependency, frequently down), bore/localhost.run (SSH-based, no HTTPS on
free tier). We call the official binary rather than vendoring anything.

SECURITY: the tunnel exposes your dashboard to the public internet, protected
only by DASHBOARD_PASSWORD. The engine refuses to start a tunnel while the
password is still the default, and warns that the URL is unlisted but public.
"""
import asyncio
import logging
import re
import shutil
from pathlib import Path

from engine.config import get_settings

log = logging.getLogger("tunnel")

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DEFAULT_BINARY = Path("bin/cloudflared.exe")
START_TIMEOUT = 45  # seconds to wait for the URL to appear


def find_binary() -> str | None:
    """bin/cloudflared.exe (Windows), bin/cloudflared, or one on PATH."""
    settings = get_settings()
    configured = Path(settings.cloudflared_binary) if settings.cloudflared_binary else None
    for candidate in (configured, DEFAULT_BINARY, Path("bin/cloudflared")):
        if candidate and candidate.exists():
            return str(candidate)
    return shutil.which("cloudflared")


class Tunnel:
    """Owns the cloudflared subprocess and the public URL."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.url: str = ""

    async def start(self) -> str:
        """Launch the tunnel and return the public URL ("" on failure)."""
        settings = get_settings()
        binary = find_binary()
        if not binary:
            print("  WARNING: TUNNEL_ENABLED=true but cloudflared was not found.\n"
                  "    Download it to bin/cloudflared.exe from\n"
                  "    https://github.com/cloudflare/cloudflared/releases/latest\n"
                  "    (asset: cloudflared-windows-amd64.exe), then restart.")
            return ""
        if settings.dashboard_password in ("", "changeme"):
            print("  REFUSING to open a public tunnel: DASHBOARD_PASSWORD is still the\n"
                  "    default. Set a strong password in .env first (the tunnel URL is\n"
                  "    public, and the password is the only thing protecting it).")
            return ""

        target = f"http://127.0.0.1:{settings.dashboard_port}"
        self.process = await asyncio.create_subprocess_exec(
            binary, "tunnel", "--url", target, "--no-autoupdate",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            self.url = await asyncio.wait_for(self._read_url(), timeout=START_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("cloudflared did not report a URL within %ss", START_TIMEOUT)
            self.url = ""
        if self.url:
            print("  " + "-" * 58)
            print(f"  PHONE ACCESS (Cloudflare tunnel): {self.url}")
            print("  Open that link on your phone and log in with DASHBOARD_PASSWORD.")
            print("  The URL is public but unlisted, and changes every restart.")
            print("  " + "-" * 58)
            asyncio.create_task(self._drain())
        return self.url

    async def _read_url(self) -> str:
        assert self.process and self.process.stdout
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                return ""
            line = raw.decode("utf-8", "replace")
            match = URL_RE.search(line)
            if match:
                return match.group(0)

    async def _drain(self) -> None:
        """Keep the pipe empty so cloudflared never blocks on a full buffer."""
        assert self.process and self.process.stdout
        while True:
            raw = await self.process.stdout.readline()
            if not raw:
                break
            log.debug("cloudflared: %s", raw.decode("utf-8", "replace").rstrip())

    async def stop(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
        self.url = ""


_tunnel = Tunnel()


def get_tunnel() -> Tunnel:
    return _tunnel


async def maybe_start_tunnel() -> str:
    """Start the tunnel when TUNNEL_ENABLED=true; return the URL or ""."""
    if not get_settings().tunnel_enabled:
        return ""
    return await _tunnel.start()
