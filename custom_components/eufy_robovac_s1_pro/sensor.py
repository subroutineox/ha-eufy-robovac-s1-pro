from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.icon import icon_for_battery_level
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import base64
import logging

from .const import CONF_COORDINATOR, CONF_DISCOVERED_DEVICES, DOMAIN
from .coordinators import EufyTuyaDataUpdateCoordinator
from .mixins import CoordinatorTuyaDeviceUniqueIDMixin
# vacuum.pyから状態判定関数と説明文をインポート
from .vacuum import decode_dps153_to_state, SUBSTATUS_DESCRIPTIONS, RobovacState

_LOGGER = logging.getLogger(__name__)


def decode_varint(data: bytes, start_pos: int) -> tuple[int, int]:
    """Decode Protocol Buffer varint format.
    
    Returns:
        tuple: (decoded_value, next_position)
    """
    value = 0
    shift = 0
    pos = start_pos
    
    while pos < len(data):
        byte = data[pos]
        value |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):  # MSB is 0, so this is the last byte
            break
        shift += 7
    
    return value, pos


def parse_dps167_statistics(dps167_value: str) -> dict[str, int | None]:
    """Parse statistics from DPS 167.
    
    Based on detailed analysis of S1 Pro data:
    - Total count: Last field (varint, can be 1 or 2 bytes)
    - Total area: 2-byte varint at fixed position 14-15
    - Total time: Not yet identified in DPS 167
    
    Args:
        dps167_value: Base64-encoded DPS 167 value
        
    Returns:
        dict with keys: total_count, total_area, total_time_mins
    """
    stats = {
        "total_count": None,
        "total_area": None,
        "total_time_mins": None,
    }
    
    try:
        # Decode base64
        data = base64.b64decode(dps167_value)
        
        if len(data) == 0:
            return stats
        
        # 1. Total count is in the last field as varint
        # The last field has tag 0x18 (field #3, wire_type=0)
        # It can be 1 byte (0-127) or 2+ bytes (128+)
        
        # Find the last field by looking for tag 0x18 from the end
        if len(data) >= 2 and data[-2] == 0x18:
            # Tag found, next byte is the value (1-byte varint)
            stats["total_count"] = data[-1]
        elif len(data) >= 3 and data[-3] == 0x18:
            # Tag found, next 2 bytes are the value (2-byte varint)
            byte1 = data[-2]
            byte2 = data[-1]
            if byte1 & 0x80:  # MSB set = multi-byte varint
                stats["total_count"] = (byte1 & 0x7F) + (byte2 << 7)
            else:
                # Single byte value
                stats["total_count"] = byte1
        elif len(data) >= 4 and data[-4] == 0x18:
            # Tag found, next 3 bytes are the value (3-byte varint, for 16384+)
            byte1 = data[-3]
            byte2 = data[-2]
            byte3 = data[-1]
            if (byte1 & 0x80) and (byte2 & 0x80):
                stats["total_count"] = (byte1 & 0x7F) + ((byte2 & 0x7F) << 7) + (byte3 << 14)
            elif byte1 & 0x80:
                # 2-byte varint
                stats["total_count"] = (byte1 & 0x7F) + (byte2 << 7)
            else:
                # Single byte
                stats["total_count"] = byte1
        
        # 2. Total area is at fixed position 14-15 as 2-byte varint
        # Confirmed positions for data length 18-19 bytes
        if len(data) >= 16:
            byte1 = data[14]
            byte2 = data[15]
            
            # Decode 2-byte varint
            if byte1 & 0x80:  # MSB set = multi-byte varint
                area = (byte1 & 0x7F) + (byte2 << 7)
                stats["total_area"] = area
            else:
                # Single byte value (unlikely for area, but handle it)
                stats["total_area"] = byte1
        
        # 3. Total time: not yet reliably identified
                
    except Exception as e:
        _LOGGER.debug(f"Error parsing DPS 167: {e}")
    
    return stats


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    discovered_devices = hass.data[DOMAIN][config_entry.entry_id][CONF_DISCOVERED_DEVICES]

    devices = []

    for device_id, props in discovered_devices.items():
        coordinator = props[CONF_COORDINATOR]
        
        # Always add battery sensor (S1 Pro uses DPS 8)
        devices.append(BatteryPercentageSensor(coordinator=coordinator))
        
        # Add running status sensor (DPS 153 with DPS 2 fallback)
        devices.append(
            RunningStatusSensor(
                coordinator=coordinator,
            )
        )
        
        # Add statistics sensors (from DPS 167)
        devices.append(TotalCleaningCountSensor(coordinator=coordinator))
        devices.append(TotalCleaningAreaSensor(coordinator=coordinator))
        # TODO: Uncomment when time data position is identified
        # devices.append(TotalCleaningTimeSensor(coordinator=coordinator))

    if devices:
        return async_add_devices(devices)


class BaseDPSensorEntity(CoordinatorTuyaDeviceUniqueIDMixin, CoordinatorEntity, SensorEntity):
    
    def __init__(
        self,
        *args,
        name: str,
        icon: str | None,
        dps_id: str,
        coordinator: EufyTuyaDataUpdateCoordinator,
        **kwargs,
    ):
        self._attr_name = name
        self._attr_icon = icon
        self.dps_id = dps_id
        super().__init__(*args, coordinator=coordinator, **kwargs)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.data is not None and self.dps_id in self.coordinator.data

    @property
    def native_value(self):
        if self.coordinator.data:
            value = self.coordinator.data.get(self.dps_id)
            if converter := getattr(self, "parse_value", None):
                try:
                    return converter(value)
                except Exception:
                    return value
            return value
        return None


class BatteryPercentageSensor(CoordinatorTuyaDeviceUniqueIDMixin, CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_name = "Battery"

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.data is not None and ("8" in self.coordinator.data or "163" in self.coordinator.data)

    @property
    def icon(self) -> str:
        # Check if charging based on DPS 5 (mode)
        mode = (self.coordinator.data or {}).get("5", "")
        charging = mode in ["charge", "docked", "Charging"]
        
        return icon_for_battery_level(self.native_value, charging=charging)

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data:
            # S1 Pro uses DPS 8 for battery
            value = self.coordinator.data.get("8")
            if value is not None:
                try:
                    battery = int(value)
                    if 0 <= battery <= 100:
                        return battery
                except (ValueError, TypeError):
                    pass
            
            # Fallback to DPS 163
            value = self.coordinator.data.get("163")
            if value is not None:
                try:
                    battery = int(value)
                    if 0 <= battery <= 100:
                        return battery
                except (ValueError, TypeError):
                    pass
        return None



class RunningStatusSensor(CoordinatorTuyaDeviceUniqueIDMixin, CoordinatorEntity, RestoreEntity, SensorEntity):
    """Sensor that shows detailed running status based on DPS 153.
    
    RestoreEntity を使用して再起動後もDPSが読めるまで最終値を保持します。
    """
    
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Running Status"
    _attr_icon = "mdi:robot-vacuum"
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._restored_value = None
    
    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._restored_value = last_state.state
            _LOGGER.debug("Restored Running Status: %s", self._restored_value)
    
    @property
    def available(self) -> bool:
        """Available if we have live data or a restored value."""
        has_live = self.coordinator.data is not None and ("153" in self.coordinator.data or "2" in self.coordinator.data)
        return has_live or self._restored_value is not None
    
    @property
    def native_value(self) -> str:
        """Return the detailed running status based on DPS 153."""
        if not self.coordinator.data:
            return self._restored_value or "Unknown"
        
        # Check DPS 153 first (most reliable for S1 Pro)
        dps153 = self.coordinator.data.get("153", "")
        
        if dps153:
            # 新しいバイトパターン判定ロジックを使用
            detected_state, substatus = decode_dps153_to_state(dps153)
            
            # サブステータスの説明文を取得
            status_description = SUBSTATUS_DESCRIPTIONS.get(substatus, "Unknown")
            
            _LOGGER.debug(
                f"Running Status: state={detected_state.value}, "
                f"substatus={substatus}, description={status_description}"
            )
            
            return status_description
        
        # Fallback to DPS 2 if DPS 153 is not available
        dps2 = self.coordinator.data.get("2")
        if dps2 is True:
            return "Running"
        elif dps2 is False:
            return "Stopped"
        
        return "Unknown"
    
    @property
    def icon(self) -> str:
        """Return icon based on current state."""
        if not self.coordinator.data:
            return "mdi:robot-vacuum"
        
        dps153 = self.coordinator.data.get("153", "")
        
        if dps153:
            detected_state, substatus = decode_dps153_to_state(dps153)
            
            # 状態に応じたアイコンを返す
            if detected_state == RobovacState.CLEANING:
                return "mdi:robot-vacuum"
            elif detected_state == RobovacState.PAUSED:
                return "mdi:pause-circle"
            elif detected_state == RobovacState.RETURNING:
                return "mdi:home-import-outline"
            elif detected_state == RobovacState.DOCKED:
                # サブステータスに応じたアイコン
                if substatus in ["charging", "fully_charged"]:
                    return "mdi:battery-charging"
                elif substatus == "dust_collecting":
                    return "mdi:delete-empty"
                elif substatus in ["mop_washing", "mop_washing_pre"]:
                    return "mdi:spray-bottle"
                elif substatus == "mop_drying":
                    return "mdi:fan"
                elif substatus == "water_refilling":
                    return "mdi:water"
                else:
                    return "mdi:home"
            elif detected_state == RobovacState.ERROR:
                return "mdi:alert-circle"
        
        return "mdi:robot-vacuum"


class TotalCleaningCountSensor(CoordinatorTuyaDeviceUniqueIDMixin, CoordinatorEntity, RestoreEntity, SensorEntity):
    """Sensor for total number of cleaning sessions from DPS 167.
    
    累積値のため RestoreEntity を使用して再起動後も最終値を保持します。
    """
    
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Total Cleaning Count"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_valid_count = None
    
    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._last_valid_count = int(last_state.state)
                _LOGGER.debug(
                    "Restored Total Cleaning Count: %s", self._last_valid_count
                )
            except (ValueError, TypeError):
                pass
    
    @property
    def available(self) -> bool:
        """Available if we have live data or a restored value."""
        has_live = self.coordinator.data is not None and "167" in self.coordinator.data
        return has_live or self._last_valid_count is not None
    
    @property
    def native_value(self) -> int | None:
        """Return the total cleaning count."""
        if not self.coordinator.data:
            return self._last_valid_count
        
        dps167 = self.coordinator.data.get("167", "")
        if not dps167:
            return self._last_valid_count
        
        stats = parse_dps167_statistics(dps167)
        new_count = stats.get("total_count")
        
        if new_count is None:
            return self._last_valid_count
        
        if self._last_valid_count is None or new_count >= self._last_valid_count:
            self._last_valid_count = new_count
            return new_count
        else:
            return self._last_valid_count


class TotalCleaningAreaSensor(CoordinatorTuyaDeviceUniqueIDMixin, CoordinatorEntity, RestoreEntity, SensorEntity):
    """Sensor for total cleaned area from DPS 167.
    
    累積値のため RestoreEntity を使用して再起動後も最終値を保持します。
    """
    
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Total Cleaning Area"
    _attr_icon = "mdi:texture-box"
    _attr_native_unit_of_measurement = "m²"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_valid_area = None
    
    async def async_added_to_hass(self) -> None:
        """Restore last known value on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                self._last_valid_area = int(last_state.state)
                _LOGGER.debug(
                    "Restored Total Cleaning Area: %s m²", self._last_valid_area
                )
            except (ValueError, TypeError):
                pass
    
    @property
    def available(self) -> bool:
        """Available if we have live data or a restored value."""
        has_live = self.coordinator.data is not None and "167" in self.coordinator.data
        return has_live or self._last_valid_area is not None
    
    @property
    def native_value(self) -> int | None:
        """Return the total cleaning area in square meters."""
        if not self.coordinator.data:
            return self._last_valid_area
        
        dps167 = self.coordinator.data.get("167", "")
        if not dps167:
            return self._last_valid_area
        
        stats = parse_dps167_statistics(dps167)
        new_area = stats.get("total_area")
        
        if new_area is None:
            return self._last_valid_area
        
        if self._last_valid_area is None or new_area >= self._last_valid_area:
            self._last_valid_area = new_area
            return new_area
        else:
            return self._last_valid_area


# TODO: Uncomment when time data position is identified in DPS 167 or DPS 168
# class TotalCleaningTimeSensor(CoordinatorTuyaDeviceUniqueIDMixin, CoordinatorEntity, SensorEntity):
#     """Sensor for total cleaning time from DPS 167.
#     
#     NOTE: The exact position of time data has not been reliably identified yet.
#     Current investigation shows:
#     - Not found as simple varint at any fixed position
#     - May be split into hours/minutes components
#     - May be stored in seconds (requires 3-byte varint)
#     - May be in DPS 168 instead of DPS 167
#     
#     TODO: Analyze logs with larger time differences to identify the pattern.
#     """
#     
#     _attr_entity_category = EntityCategory.DIAGNOSTIC
#     _attr_name = "Total Cleaning Time"
#     _attr_icon = "mdi:clock-outline"
#     _attr_device_class = SensorDeviceClass.DURATION
#     _attr_native_unit_of_measurement = UnitOfTime.MINUTES
#     _attr_state_class = SensorStateClass.TOTAL_INCREASING
#     
#     @property
#     def available(self) -> bool:
#         """Return if entity is available."""
#         return self.coordinator.data is not None and "167" in self.coordinator.data
#     
#     @property
#     def native_value(self) -> int | None:
#         """Return the total cleaning time in minutes."""
#         if not self.coordinator.data:
#             return None
#         
#         dps167 = self.coordinator.data.get("167", "")
#         if not dps167:
#             return None
#         
#         stats = parse_dps167_statistics(dps167)
#         return stats.get("total_time_mins")
