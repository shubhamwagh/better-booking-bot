"""Typed credential/card settings, loaded from the environment or .env.

Deliberately excludes CONFIG_PATH - that one is mutated at runtime (CLI
--config flag, per-test monkeypatching) and needs to be re-read on every
call, not fixed at process start like credentials are.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from better_bot.checkout import CardDetails


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    better_username: str
    better_password: str

    card_cvv: str = ""
    card_number: str | None = None
    card_expiry: str | None = None
    billing_first_name: str | None = None
    billing_last_name: str | None = None
    billing_address1: str | None = None
    billing_address2: str | None = None
    billing_city: str | None = None
    billing_postcode: str | None = None
    save_card: bool = False

    def to_card(self) -> CardDetails:
        return CardDetails(
            cvv=self.card_cvv,
            number=self.card_number,
            expiry=self.card_expiry,
            first_name=self.billing_first_name,
            last_name=self.billing_last_name,
            address1=self.billing_address1,
            address2=self.billing_address2,
            city=self.billing_city,
            postcode=self.billing_postcode,
            save_card=self.save_card,
        )


class NtfySettings(BaseSettings):
    """Self-hosted ntfy config for phone push on booking outcomes.

    Kept separate from Settings - all fields are optional, so notify.send()
    can always instantiate this even when BETTER_USERNAME/PASSWORD (required
    on Settings) aren't set, e.g. in tests or a CI environment with no .env.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ntfy_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_token: str | None = None
