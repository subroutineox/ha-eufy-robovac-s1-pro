import logging

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .const import DOMAIN, CONF_IP_ADDRESS
from .eufy_local_id_grabber.clients import EufyHomeSession

logger = logging.getLogger(__name__)

# Neues Schema mit optionalem IP-Feld
EUFY_LOGIN_SCHEMA = vol.Schema({
    vol.Required("username"): str, 
    vol.Required("password"): str,
    vol.Optional(CONF_IP_ADDRESS, default=""): str  # HIER NEU
})


class EufyVacuumConfigFlow(ConfigFlow, domain=DOMAIN):
    async def async_step_user(self, user_input: dict[str, str] | None = None) -> data_entry_flow.FlowResult:
        errors = {}

        if user_input is not None:
            username = user_input["username"]
            password = user_input["password"]
            ip_address = user_input.get(CONF_IP_ADDRESS, "").strip() # HIER NEU

            await self.async_set_unique_id(username)
            self._abort_if_unique_id_configured()

            client = EufyHomeSession(username, password)

            try:
                await self.hass.async_add_executor_job(client.get_user_info)
            except Exception:
                logger.exception("Error when logging in with %s", username)

                # TODO: proper exception handling
                errors["base"] = "Username or password is incorrect"
            else:
                return self.async_create_entry(
                    title=username,
                    # HIER NEU: Wir speichern die IP mit in der Config
                    data={
                        CONF_EMAIL: username, 
                        CONF_PASSWORD: password, 
                        CONF_IP_ADDRESS: ip_address
                    },
                )

        return self.async_show_form(step_id="user", data_schema=EUFY_LOGIN_SCHEMA, errors=errors)
