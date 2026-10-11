"""Config Flow für BGG Collection Sync."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BggAuthError, BggClient, BggError, BggNotFoundError
from .const import (
    CONF_END_SCENE,
    CONF_INCLUDE_EXPANSIONS,
    CONF_INTERVAL_HOURS,
    CONF_MEDIA_PLAYER,
    CONF_ONLY_OWNED,
    CONF_TOKEN,
    CONF_USERNAME,
    DEFAULT_INCLUDE_EXPANSIONS,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_ONLY_OWNED,
    DOMAIN,
)


class BggConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _validate(self, username: str, token: str | None) -> str | None:
        client = BggClient(async_get_clientsession(self.hass), username, token)
        try:
            await client.validate_user()
        except BggAuthError:
            return "invalid_auth"
        except BggNotFoundError:
            return "user_not_found"
        except BggError:
            return "cannot_connect"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            token = (user_input.get(CONF_TOKEN) or "").strip() or None
            await self.async_set_unique_id(username.lower())
            self._abort_if_unique_id_configured()
            if error := await self._validate(username, token):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=username, data={CONF_USERNAME: username, CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Optional(CONF_TOKEN): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if error := await self._validate(entry.data[CONF_USERNAME], token):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> BggOptionsFlow:
        return BggOptionsFlow()


class BggOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_INTERVAL_HOURS,
                        default=opts.get(CONF_INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                    vol.Required(
                        CONF_ONLY_OWNED,
                        default=opts.get(CONF_ONLY_OWNED, DEFAULT_ONLY_OWNED),
                    ): bool,
                    vol.Required(
                        CONF_INCLUDE_EXPANSIONS,
                        default=opts.get(CONF_INCLUDE_EXPANSIONS, DEFAULT_INCLUDE_EXPANSIONS),
                    ): bool,
                    vol.Optional(
                        CONF_MEDIA_PLAYER,
                        description={"suggested_value": opts.get(CONF_MEDIA_PLAYER)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="media_player")
                    ),
                    vol.Optional(
                        CONF_END_SCENE,
                        description={"suggested_value": opts.get(CONF_END_SCENE)},
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain=["scene", "script"])
                    ),
                }
            ),
        )
