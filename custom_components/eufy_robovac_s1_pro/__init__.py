"""
Quick and dirty module to support Eufy S1 Pro.
"""

import asyncio
import json
import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import CONF_COORDINATOR, CONF_DISCOVERED_DEVICES, DOMAIN, PLATFORMS, CONF_IP_ADDRESS
from .coordinators import EufyTuyaDataUpdateCoordinator
from . import room_clean as rc
from .discovery import discover
from .eufy_local_id_grabber.clients import EufyHomeSession, TuyaAPISession

logger = logging.getLogger(__name__)

SERVICE_DUMP_DPS = "dump_dps"
SERVICE_WRITE_DPS = "write_dps"
SERVICE_CLEAN_ROOMS = "clean_rooms"
SERVICE_CANCEL_CLEAN = "cancel_clean"
SERVICE_REFRESH_LOCAL_KEY = "refresh_local_key"

# Zwischenspeicher im Konfigurationseintrag: Gruppe, Geraete-ID und Local Key aus der Cloud.
# Der Local Key entsteht beim Koppeln und bleibt gueltig, bis der Roboter zurueckgesetzt oder
# neu gekoppelt wird - die Cloud wird daher nur beim allerersten Start (ohne Speicher) und
# ueber den Dienst refresh_local_key gebraucht.
CONF_LOCAL_CACHE = "local_cache"
# Ohne gespeicherten Schluessel fragt die Einrichtung die Cloud hoechstens alle 15 Minuten -
# Home Assistant wiederholt "noch nicht bereit" sonst alle 80 Sekunden, und eufy sperrt
# bei zu vielen Logins (HTTP 429) nur noch laenger.
CLOUD_RETRY_MIN = timedelta(minutes=15)

CLEAN_ROOMS_SCHEMA = vol.Schema(
    {
        vol.Required("rooms"): vol.All(cv.ensure_list, [vol.All(int, vol.Range(min=0, max=31))]),
        vol.Optional("delay", default=120): vol.All(int, vol.Range(min=0, max=3600)),
        vol.Optional("cycle", default=0): vol.All(int, vol.Range(min=0, max=127)),
        vol.Optional("repeats", default=1): vol.All(int, vol.Range(min=1, max=2)),
    }
)

WRITE_DPS_SCHEMA = vol.Schema(
    {
        vol.Required("dps_id"): vol.All(cv.string, vol.Length(min=1, max=4)),
        vol.Required("value"): vol.Any(cv.string, cv.boolean, int, float),
    }
)


def _fetch_cloud_devices(username: str, password: str) -> list[dict]:
    """Holt Gruppe, Geraete-ID und Local Key aller Geraete aus der eufy-/Tuya-Cloud (blockierend)."""
    client = EufyHomeSession(username, password)
    user_info = client.get_user_info()
    tuya_session = TuyaAPISession(
        username=f'eh-{user_info["id"]}', country_code=user_info["phone_code"]
    )
    devices = []
    for home in tuya_session.list_homes():
        for device in tuya_session.list_devices(home["groupId"]):
            devices.append(
                {
                    "group_id": home["groupId"],
                    "dev_id": device["devId"],
                    "local_key": device["localKey"],
                }
            )
    return devices


@callback
def _async_store_cache(hass: HomeAssistant, entry: ConfigEntry, devices: list[dict]) -> None:
    """Geraeteschluessel im Konfigurationseintrag ablegen."""
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_LOCAL_CACHE: {
                "devices": devices,
                "updated": dt_util.now().isoformat(timespec="seconds"),
            },
        },
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Set up Eufy Vacuum entities from a config entry.
    """

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(entry.entry_id, {})

    username = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    manual_ip = entry.data.get(CONF_IP_ADDRESS, "").strip() # HIER NEU

    cache = entry.data.get(CONF_LOCAL_CACHE) or {}
    cloud_devices = cache.get("devices") or []
    if cloud_devices:
        logger.info(
            "Nutze gespeicherte Geraeteschluessel (%s Geraet(e), Stand %s) - kein Cloud-Login noetig",
            len(cloud_devices),
            cache.get("updated", "?"),
        )
    else:
        last_fail = hass.data.get(f"{DOMAIN}_cloud_last_fail")
        if last_fail and dt_util.utcnow() - last_fail < CLOUD_RETRY_MIN:
            naechster = dt_util.as_local(last_fail + CLOUD_RETRY_MIN).strftime("%H:%M")
            raise ConfigEntryNotReady(
                f"eufy-Cloud zuletzt nicht erreichbar - naechster Versuch ab {naechster}"
            )
        try:
            cloud_devices = await hass.async_add_executor_job(
                _fetch_cloud_devices, username, password
            )
        except Exception as err:
            hass.data[f"{DOMAIN}_cloud_last_fail"] = dt_util.utcnow()
            raise ConfigEntryNotReady(
                f"eufy-/Tuya-Cloud nicht erreichbar, Home Assistant versucht es erneut: {err}"
            ) from err
        hass.data.pop(f"{DOMAIN}_cloud_last_fail", None)
        if cloud_devices:
            _async_store_cache(hass, entry, cloud_devices)
            logger.info("Geraeteschluessel aus der Cloud geholt und gespeichert (%s Geraet(e))", len(cloud_devices))

    try:
        hass.data[DOMAIN][entry.entry_id].setdefault(CONF_DISCOVERED_DEVICES, {})

        # HIER NEU: Wir machen den Scan nur noch, wenn keine IP angegeben wurde
        detected_devices = {}
        if not manual_ip:
            detected_devices = await discover()
            logger.debug("Detected devices on local network: %s", list(detected_devices.keys()))
        else:
            logger.debug("Manual IP provided: %s. Skipping UDP discovery.", manual_ip)

        for device in cloud_devices:
            group_id = device["group_id"]
            device_id = device["dev_id"]
            local_key = device["local_key"]

            device_ip = None

            # HIER NEU: Weiche für IP-Zuweisung
            if manual_ip:
                device_ip = manual_ip
                logger.debug("Using manually configured IP %s for device ID %s", device_ip, device_id)
            else:
                logger.debug("Looking for device_id '%s' in detected devices", device_id)
                discovered_device = detected_devices.pop(device_id, None)
                if discovered_device:
                    device_ip = discovered_device["ip"]
                    logger.debug("Found matching discovered device at %s for device ID %s", device_ip, device_id)

            if device_ip:
                hass_entity_id = f'{group_id}-{device_id}'

                coordinator = EufyTuyaDataUpdateCoordinator(
                    hass,
                    logger=logger,
                    name=DOMAIN,
                    update_interval=timedelta(seconds=30),
                    host=device_ip,
                    device_id=device_id,
                    local_key=local_key,
                )

                # Try to get initial data, but don't fail if it doesn't work
                try:
                    await coordinator.async_config_entry_first_refresh()
                except Exception as e:
                    logger.warning(
                        "Could not get initial data for device %s at %s: %s",
                        device_id,
                        device_ip,
                        e,
                    )
                    # Still add the device, it might come online later

                hass.data[DOMAIN][entry.entry_id][CONF_DISCOVERED_DEVICES][hass_entity_id] = {
                    CONF_COORDINATOR: coordinator
                }
            else:
                logger.warning(
                    "Could not find device %s on the local network. "
                    "Available devices: %s. Device may be offline or on a different network.",
                    device_id,
                    list(detected_devices.keys()) if detected_devices else "none",
                )

    except Exception:
        # TODO: raise proper exception
        logger.exception("Exception when trying to set up devices")
        raise
    else:
        # Forward the setup to each platform - use the correct method
        # Try the newer API first, then fallback to older methods
        try:
            # For Home Assistant 2023.8+
            await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        except AttributeError:
            # Fallback for single platform setup
            for platform in PLATFORMS:
                try:
                    hass.async_create_task(
                        hass.config_entries.async_forward_entry_setup(entry, platform)
                    )
                except Exception as e:
                    logger.error("Failed to setup platform %s: %s", platform, e)
                    # Continue with other platforms even if one fails

        _async_register_services(hass)

        return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services (idempotent across config entries)."""
    if hass.services.has_service(DOMAIN, SERVICE_DUMP_DPS):
        return

    async def _handle_dump_dps(call: ServiceCall) -> None:
        """Dump every coordinator's current DPS dict to the HA log at INFO."""
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            logger.info("dump_dps: no Eufy RoboVac S1 Pro entries are loaded")
            return
        for entry_id, entry_data in entries.items():
            for entity_id, info in entry_data.get(CONF_DISCOVERED_DEVICES, {}).items():
                coordinator = info.get(CONF_COORDINATOR)
                if coordinator is None:
                    continue
                logger.info(
                    "dump_dps[%s/%s]: %s",
                    entry_id,
                    entity_id,
                    json.dumps(coordinator.data or {}, default=str, ensure_ascii=False),
                )

    async def _handle_write_dps(call: ServiceCall) -> None:
        """Write a raw value to a Tuya DPS on every coordinator (Phase 0 only)."""
        dps_id = str(call.data["dps_id"])
        value = call.data["value"]
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            logger.warning("write_dps: no Eufy RoboVac S1 Pro entries are loaded")
            return
        for entry_id, entry_data in entries.items():
            for entity_id, info in entry_data.get(CONF_DISCOVERED_DEVICES, {}).items():
                coordinator = info.get(CONF_COORDINATOR)
                if coordinator is None:
                    continue
                logger.info(
                    "write_dps[%s/%s]: writing DPS %s = %r (Phase 0 trial)",
                    entry_id,
                    entity_id,
                    dps_id,
                    value,
                )
                try:
                    await coordinator.tuya_client.async_set({dps_id: value})
                except Exception:
                    logger.exception(
                        "write_dps[%s/%s]: write failed for DPS %s = %r",
                        entry_id,
                        entity_id,
                        dps_id,
                        value,
                    )


    def _coordinators():
        """Momentaufnahme der Coordinatoren.

        Bewusst eine Liste: waehrend der Verarbeitung wird hass.data
        veraendert, und ueber ein Dictionary zu iterieren, das sich dabei
        aendert, laesst Python abbrechen.
        """
        found = []
        for entry_data in list(hass.data.get(DOMAIN, {}).values()):
            if not isinstance(entry_data, dict):
                continue
            for info in list(entry_data.get(CONF_DISCOVERED_DEVICES, {}).values()):
                coordinator = info.get(CONF_COORDINATOR)
                if coordinator is not None:
                    found.append(coordinator)
        return found

    async def _handle_clean_rooms(call: ServiceCall) -> None:
        """Raumreinigung ueber eine einmalige Aufgabe ausloesen."""
        rooms = [int(r) for r in call.data["rooms"]]
        delay = int(call.data.get("delay", 120))
        cycle = int(call.data.get("cycle", 0))
        repeats = int(call.data.get("repeats", 1))
        for coordinator in _coordinators():
            # Saugstufe, Wassermenge und Wischen aus dem Live-Zustand uebernehmen,
            # damit eine Einstellung fuer manuelle und geplante Reinigung gilt.
            params = rc.params_from_dps(coordinator.data)
            value, start = rc.clean_rooms_value(
                rooms, delay=delay, cycle=cycle, repeats=repeats, **params
            )
            names = ", ".join(rc.ROOMS.get(r, str(r)) for r in rooms)
            logger.info(
                "clean_rooms: %s um %02d:%02d (in %s s), fan=%s water=%s mop=%s x%s",
                names, start.hour, start.minute, delay,
                params["fan"], params["water"], params["mop"], repeats,
            )
            try:
                await coordinator.tuya_client.async_set({rc.DPS_TIMER: value})
            except Exception:
                logger.exception("clean_rooms: Schreiben auf DPS %s fehlgeschlagen", rc.DPS_TIMER)
                continue
            hass.data[f"{DOMAIN}_pending_clean"] = (start.hour, start.minute)
            hass.bus.async_fire(
                f"{DOMAIN}_clean_scheduled",
                {"rooms": rooms, "names": names, "start": start.isoformat()},
            )

    async def _handle_cancel_clean(call: ServiceCall) -> None:
        """Loescht die zuletzt selbst geplante Aufgabe.

        Nur diese eine - vom Benutzer in der App angelegte Einmal-Aufgaben
        bleiben unangetastet. Ohne gemerkte Startzeit passiert nichts.
        """
        pending = hass.data.get(f"{DOMAIN}_pending_clean")
        if pending is None:
            logger.info("cancel_clean: keine selbst geplante Aufgabe bekannt")
            hass.bus.async_fire(f"{DOMAIN}_clean_cancelled", {"removed": 0})
            return
        hour, minute = pending
        removed = 0
        for coordinator in _coordinators():
            # Der zwischengespeicherte Stand kennt die eben angelegte Aufgabe
            # womoeglich noch nicht - deshalb vorher aktiv nachfragen.
            try:
                await coordinator.tuya_client.async_set(
                    {rc.DPS_TIMER: rc.request(rc.M_INQUIRY)}
                )
                await asyncio.sleep(1)
                await coordinator.async_request_refresh()
            except Exception:
                logger.exception("cancel_clean: Abfrage fehlgeschlagen")
            current = (coordinator.data or {}).get(rc.DPS_TIMER)
            if not current:
                logger.warning("cancel_clean: DPS %s ist unbekannt", rc.DPS_TIMER)
                continue
            try:
                timer_id = rc.find_timer_id(current, hour, minute)
            except Exception:
                logger.exception("cancel_clean: Aufgabenliste nicht lesbar")
                continue
            if timer_id is None:
                logger.info(
                    "cancel_clean: keine Einmal-Aufgabe um %02d:%02d gefunden", hour, minute
                )
                continue
            try:
                await coordinator.tuya_client.async_set(
                    {rc.DPS_TIMER: rc.delete_value(timer_id)}
                )
                removed += 1
            except Exception:
                logger.exception("cancel_clean: Loeschen von Aufgabe %s fehlgeschlagen", timer_id)
        if removed:
            hass.data.pop(f"{DOMAIN}_pending_clean", None)
        logger.info("cancel_clean: %s Aufgabe(n) geloescht", removed)
        hass.bus.async_fire(f"{DOMAIN}_clean_cancelled", {"removed": removed})

    async def _handle_refresh_local_key(call: ServiceCall) -> None:
        """Local Key neu aus der Cloud holen (nach neuem Koppeln) und die Integration neu laden.

        Klappt der Abruf nicht, bleibt der gespeicherte Schluessel unangetastet.
        """
        for entry in hass.config_entries.async_entries(DOMAIN):
            try:
                devices = await hass.async_add_executor_job(
                    _fetch_cloud_devices, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]
                )
            except Exception as err:
                logger.exception("refresh_local_key: Cloud-Abruf fehlgeschlagen - gespeicherter Schluessel bleibt")
                persistent_notification.async_create(
                    hass,
                    f"Der Schluessel konnte nicht aus der Cloud geholt werden: {err}\n\n"
                    "Der gespeicherte Schluessel bleibt unveraendert.",
                    title="eufy S1 Pro: Schluessel nicht aktualisiert",
                    notification_id="eufy_s1_pro_refresh_key",
                )
                continue
            if not devices:
                logger.warning("refresh_local_key: Cloud lieferte keine Geraete - gespeicherter Schluessel bleibt")
                continue
            old = {
                d.get("dev_id"): d.get("local_key")
                for d in (entry.data.get(CONF_LOCAL_CACHE) or {}).get("devices", [])
            }
            changed = len(old) != len(devices) or any(
                old.get(d["dev_id"]) != d["local_key"] for d in devices
            )
            _async_store_cache(hass, entry, devices)
            persistent_notification.async_create(
                hass,
                "Neuer Schluessel gespeichert, die Integration wird neu geladen."
                if changed
                else "Der Schluessel ist unveraendert, die Integration wird neu geladen.",
                title="eufy S1 Pro: Schluessel aus der Cloud geholt",
                notification_id="eufy_s1_pro_refresh_key",
            )
            logger.info("refresh_local_key: Schluessel %s", "geaendert" if changed else "unveraendert")
            hass.config_entries.async_schedule_reload(entry.entry_id)

    hass.services.async_register(DOMAIN, SERVICE_DUMP_DPS, _handle_dump_dps)
    hass.services.async_register(
        DOMAIN, SERVICE_WRITE_DPS, _handle_write_dps, schema=WRITE_DPS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAN_ROOMS, _handle_clean_rooms, schema=CLEAN_ROOMS_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_CLEAN, _handle_cancel_clean)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_LOCAL_KEY, _handle_refresh_local_key)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Try multiple unload methods for compatibility
    try:
        # For newer Home Assistant versions
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except AttributeError:
        # Fallback for older versions
        try:
            unload_ok = all(
                await asyncio.gather(
                    *[
                        hass.config_entries.async_forward_entry_unload(entry, platform)
                        for platform in PLATFORMS
                    ]
                )
            )
        except Exception as e:
            logger.error("Error unloading platforms: %s", e)
            unload_ok = False
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    
    return unload_ok
