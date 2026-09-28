from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN


class CoordinatorTuyaDeviceUniqueIDMixin:
    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.tuya_client.device_id)},
            manufacturer="Eufy",
            # Kein via_device mehr: Es zeigte auf dasselbe Geraet (Selbstbezug, ohne Nutzen)
            # und ist seit HA 2026.9 abgekuendigt (entfaellt mit 2027.8).
        )

    @property
    def unique_id(self) -> str:
        slug = self.name.lower().replace(" ", "_")

        return self.coordinator.tuya_client.device_id + "-" + slug
