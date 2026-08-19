"""Lidl Plus tickets API client.

Two endpoints carry everything we need:

  GET /api/v2/{country}/tickets?pageNumber=N   -> paged list of summaries
  GET /api/v3/{country}/tickets/{id}           -> one receipt with line items
"""

from __future__ import annotations

import time
from collections.abc import Iterator

from . import auth
from .config import Config
from .http import HttpError, request_json

TICKETS_API = "https://tickets.lidlplus.com/api"


class AuthExpired(RuntimeError):
    """The stored refresh token is no longer accepted."""


class LidlClient:
    def __init__(self, config: Config, *, persist: bool = True) -> None:
        self.config = config
        self._persist = persist
        self._access_token = ""
        self._expires_at = 0.0

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token

        if not self.config.refresh_token:
            raise AuthExpired("No refresh token stored. Run `lidl login`.")

        try:
            data = auth.refresh_tokens(self.config.refresh_token)
        except HttpError as exc:
            if exc.status in (400, 401):
                raise AuthExpired(
                    "Refresh token rejected by Lidl "
                    f"({exc.status}). Run `lidl login` again.\n{exc.body[:300]}"
                ) from exc
            raise

        self._access_token = data["access_token"]
        # Renew a minute early so a request never races the expiry.
        self._expires_at = time.time() + int(data.get("expires_in", 3600)) - 60

        # Lidl rotates refresh tokens: the response carries a new one and the
        # old one stops working. Not persisting this is why long-lived setups
        # silently break after a while.
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self.config.refresh_token:
            self.config.refresh_token = new_refresh
            if self._persist:
                self.config.save()

        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "App-Version": self.config.app_version,
            "Operating-System": "iOs",
            "App": "com.lidl.eci.lidl.plus",
            "Accept-Language": self.config.language,
        }

    def _get(self, path: str) -> dict:
        return request_json(f"{TICKETS_API}{path}", headers=self._headers())

    def list_tickets(self, *, only_favourite: bool = False) -> Iterator[dict]:
        """Yield receipt summaries, newest first, across all pages."""
        country = self.config.country.upper()
        favourite = "true" if only_favourite else "false"
        page = 1
        seen = 0

        while True:
            data = self._get(
                f"/v2/{country}/tickets"
                f"?pageNumber={page}&onlyFavorite={favourite}"
            )
            tickets = data.get("tickets") or []
            if not tickets:
                return

            yield from tickets
            seen += len(tickets)

            total = data.get("totalCount")
            if total is not None and seen >= int(total):
                return
            page += 1

    def ticket(self, ticket_id: str) -> dict:
        """Fetch one receipt in full, as raw JSON straight from the API."""
        country = self.config.country.upper()
        return self._get(f"/v3/{country}/tickets/{ticket_id}")

    def latest_ticket(self) -> dict:
        for summary in self.list_tickets():
            return self.ticket(summary["id"])
        raise RuntimeError("No receipts found on this account.")
