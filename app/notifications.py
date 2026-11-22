from urllib.parse import urlsplit

import httpx

from app.monitoring import CheckResult


class WebhookNotifier:
    """Send PulseCheck state transitions to a Discord channel."""

    def __init__(
        self,
        url: str,
        client: httpx.AsyncClient | None = None,
    ):
        parsed = urlsplit(url)

        parts = [part for part in parsed.path.split("/") if part]

        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"discord.com", "discordapp.com"}
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(parts) != 4
            or parts[:2] != ["api", "webhooks"]
            or not parts[2].isdigit()
            or not parts[3]
        ):
            raise ValueError("Invalid Discord webhook URL.")

        self._url = url
        self._client = client or httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    async def notify_transition(
        self,
        previous: CheckResult | None,
        current: CheckResult,
    ) -> None:
        if previous is not None and previous.status == current.status:
            return

        is_down = current.status == "DOWN"

        title = (
            f"Monitor DOWN: {current.name}"
            if is_down
            else f"Monitor recovered: {current.name}"
        )

        description = (
            "PulseCheck detected that this application is unavailable."
            if is_down
            else "PulseCheck detected that this application is responding again."
        )

        fields = [
            {
                "name": "Status change",
                "value": (
                    f"{previous.status} → {current.status}"
                    if previous is not None
                    else f"First check → {current.status}"
                ),
                "inline": True,
            },
            {
                "name": "HTTP status",
                "value": str(current.status_code or "Unavailable"),
                "inline": True,
            },
            {
                "name": "Response time",
                "value": f"{current.latency_ms:.2f} ms",
                "inline": True,
            },
            {
                "name": "Application URL",
                "value": current.url[:1024],
                "inline": False,
            },
        ]

        if is_down:
            fields.append({
                "name": "Consecutive failures",
                "value": str(current.consecutive_failures),
                "inline": True,
            })

            if current.error:
                fields.append({
                    "name": "Error",
                    "value": current.error[:1024],
                    "inline": False,
                })

        elif current.last_success_at:
            fields.append({
                "name": "Last successful check",
                "value": current.last_success_at.isoformat(),
                "inline": False,
            })

        payload = {
            "username": "PulseCheck",
            "allowed_mentions": {"parse": []},
            "embeds": [{
                "title": title[:256],
                "description": description,
                "color": 0xED4245 if is_down else 0x57F287,
                "fields": fields,
                "timestamp": current.checked_at.isoformat(),
                "footer": {
                    "text": "PulseCheck · Application Monitoring"
                },
            }],
        }

        response = await self._client.post(
            self._url,
            params={"wait": "true"},
            json=payload,
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
