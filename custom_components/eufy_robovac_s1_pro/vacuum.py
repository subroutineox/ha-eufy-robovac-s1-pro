import logging
from typing import Any
import asyncio
import base64
from enum import Enum

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_COORDINATOR, CONF_DISCOVERED_DEVICES, DOMAIN

logger = logging.getLogger(__name__)


# S1 Pro actual fan speed mappings (from app testing)
HA_TO_EUFY_FAN_SPEED_MAP = {
    "Quiet": ("gentle", "Quiet"),      # DPS 9: gentle, DPS 158: Quiet
    "Standard": ("normal", "Standard"), # DPS 9: normal, DPS 158: Standard  
    "Turbo": ("strong", "Turbo"),      # DPS 9: strong, DPS 158: Turbo
    "Maximum": ("max", "Max")           # DPS 9: max, DPS 158: Max
}

# Reverse mapping for display
EUFY_TO_HA_FAN_SPEED_MAP = {
    "gentle": "Quiet",
    "normal": "Standard",
    "strong": "Turbo",
    "max": "Maximum",
    "Quiet": "Quiet",
    "Standard": "Standard",
    "Turbo": "Turbo",
    "Max": "Maximum",
    "middle": "Standard",  # Fallback
}

# S1 Pro Command definitions for DPS 152 (from actual app logs)
S1_PRO_COMMANDS = {
    "start": "AA==",        # æŽƒé™¤é–‹å§‹
    "cleaning": "AggO",     # æŽƒé™¤ä¸­
    "pause": "AggN",        # ä¸€æ™‚åœæ­¢
    "return": "AggG",       # ã‚¹ãƒ†ãƒ¼ã‚·ãƒ§ãƒ³å¸°é‚„
}


class RobovacState(Enum):
    """ãƒ­ãƒœãƒƒãƒˆæŽƒé™¤æ©Ÿã®çŠ¶æ…‹å®šç¾©"""
    CLEANING = "cleaning"
    PAUSED = "paused"
    RETURNING = "returning"
    DOCKED = "docked"
    ERROR = "error"
    UNKNOWN = "unknown"


def decode_dps153_to_state(dps153_value: str) -> tuple[RobovacState, str]:
    """
    dps153ã®å€¤ã‹ã‚‰ãƒ­ãƒœãƒƒãƒˆæŽƒé™¤æ©Ÿã®çŠ¶æ…‹ã¨ã‚µãƒ–ã‚¹ãƒ†ãƒ¼ã‚¿ã‚¹ã‚’åˆ¤å®š
    
    ã“ã®é–¢æ•°ã¯ç’°å¢ƒã‚„è¨­å®šã®é•ã„ã«å¯¾å¿œã§ãã‚‹ã‚ˆã†ã€ãƒã‚¤ãƒˆãƒ‘ã‚¿ãƒ¼ãƒ³ã®
    æ™®éçš„ãªç‰¹å¾´ã«åŸºã¥ã„ã¦åˆ¤å®šã‚’è¡Œã„ã¾ã™ã€‚
    
    åˆ¤å®šãƒ­ã‚¸ãƒƒã‚¯:
    1. Cleaning: Byte[1]=0x0a, Byte[2]=0x00, Byte[3]=0x10, Byte[4]=0x05, length=7
    2. Paused: Byte[1]=0x0a, Byte[2]=0x00, Byte[3]=0x10, Byte[4]=0x05, length>=9, Byte[6]=0x02
    3. Returning: Byte[1]=0x10, Byte[2]=0x07, Byte[3]=0x42
    4. Docked: ä¸Šè¨˜ä»¥å¤–ã®å ´åˆ
    
    Args:
        dps153_value: Base64ã‚¨ãƒ³ã‚³ãƒ¼ãƒ‰ã•ã‚ŒãŸdps153ã®å€¤ã€ã¾ãŸã¯ãƒã‚¤ãƒˆåˆ—
        
    Returns:
        (RobovacState, substatus_str): åˆ¤å®šã•ã‚ŒãŸçŠ¶æ…‹ã¨ã‚µãƒ–ã‚¹ãƒ†ãƒ¼ã‚¿ã‚¹æ–‡å­—åˆ—ã®ã‚¿ãƒ—ãƒ«
    """
    try:
        # Base64æ–‡å­—åˆ—ã®å ´åˆã¯ãƒ‡ã‚³ãƒ¼ãƒ‰
        if isinstance(dps153_value, str):
            decoded = base64.b64decode(dps153_value)
        else:
            decoded = dps153_value
        
        # æœ€ä½Žé™ã®é•·ã•ãƒã‚§ãƒƒã‚¯
        if len(decoded) < 3:
            logger.warning(f"dps153 data too short: {len(decoded)} bytes")
            return RobovacState.UNKNOWN, "unknown"
        
        byte1 = decoded[1]
        byte2 = decoded[2]
        
        # ãƒ‡ãƒãƒƒã‚°ãƒ­ã‚°
        hex_str = ' '.join([f"{b:02x}" for b in decoded])
        logger.debug(f"dps153 decoded: {hex_str}")
        
        # ========== ä¸»è¦ãªçŠ¶æ…‹åˆ¤å®š ==========
        
        # Byte[1]=0x0a, Byte[2]=0x00 ã®ãƒ‘ã‚¿ãƒ¼ãƒ³
        # (Cleaning, Paused, ãƒ¢ãƒƒãƒ—é–¢é€£Docked)
        if byte1 == 0x0a and byte2 == 0x00:
            if len(decoded) >= 5:
                byte3 = decoded[3]
                byte4 = decoded[4]
                
                # Cleaning/Pausedã®ãƒ‘ã‚¿ãƒ¼ãƒ³
                if byte3 == 0x10 and byte4 == 0x05:
                    # Pausedã®åˆ¤å®š
                    if len(decoded) >= 7 and decoded[6] == 0x02:
                        return RobovacState.PAUSED, "paused"
                    else:
                        return RobovacState.CLEANING, "cleaning"
                
                # ãƒ¢ãƒƒãƒ—é–¢é€£Dockedã®ãƒ‘ã‚¿ãƒ¼ãƒ³
                elif byte3 == 0x10 and byte4 == 0x09:
                    substatus = _get_docked_substatus(decoded)
                    return RobovacState.DOCKED, substatus
        
        # Returning ã®åˆ¤å®š
        if byte1 == 0x10 and byte2 == 0x07:
            if len(decoded) >= 4 and decoded[3] == 0x42:
                return RobovacState.RETURNING, "returning"
        
        # Docked (ãã®ä»–) ã®åˆ¤å®š
        if byte1 == 0x10:
            substatus = _get_docked_substatus(decoded)
            return RobovacState.DOCKED, substatus
        
        # ãƒ‡ãƒ•ã‚©ãƒ«ãƒˆã¯Docked (æœªçŸ¥ã®ãƒ‘ã‚¿ãƒ¼ãƒ³ã§ã‚‚å®‰å…¨å´ã«å€’ã™)
        logger.warning(f"Unknown dps153 pattern, defaulting to DOCKED: {hex_str}")
        return RobovacState.DOCKED, "idle"
        
    except Exception as e:
        logger.error(f"Error decoding dps153: {e}", exc_info=True)
        return RobovacState.UNKNOWN, "error"


def _get_docked_substatus(decoded: bytes) -> str:
    """
    DockedçŠ¶æ…‹ã®è©³ç´°ãªã‚µãƒ–ã‚¹ãƒ†ãƒ¼ã‚¿ã‚¹ã‚’å–å¾—
    
    Args:
        decoded: ãƒ‡ã‚³ãƒ¼ãƒ‰ã•ã‚ŒãŸdps153ã®ãƒã‚¤ãƒˆåˆ—
        
    Returns:
        ã‚µãƒ–ã‚¹ãƒ†ãƒ¼ã‚¿ã‚¹æ–‡å­—åˆ—
    """
    if len(decoded) < 3:
        return "unknown"
    
    byte1 = decoded[1]
    byte2 = decoded[2]
    
    # Byte[1]=0x10 ã®å ´åˆ
    if byte1 == 0x10:
        if byte2 == 0x03:
            # å……é›»é–¢é€£
            if len(decoded) >= 5:
                if decoded[4] == 0x00:
                    return "charging"
                elif decoded[4] == 0x02:
                    return "fully_charged"
            return "charging"
        
        elif byte2 == 0x09:
            # ãƒ¢ãƒƒãƒ—é–¢é€£æ“ä½œ
            if len(decoded) >= 4:
                byte3 = decoded[3]
                
                if byte3 == 0xfa:
                    return "dust_collecting"
                elif byte3 == 0x1a:
                    return "mop_drying"
                elif byte3 == 0x3a:
                    return "mop_washing"
            
            return "mop_operations"
    
    # Byte[1]=0x0a ã®å ´åˆ (çµ¦æ°´ä¸­ã€æŽƒé™¤å‰ãƒ¢ãƒƒãƒ—æ´—æµ„ä¸­ãªã©)
    if byte1 == 0x0a and byte2 == 0x00:
        if len(decoded) >= 5 and decoded[3] == 0x10 and decoded[4] == 0x09:
            if len(decoded) >= 12 and decoded[11] == 0x3a:
                return "mop_washing_pre"
            return "water_refilling"
    
    return "idle"


# ã‚µãƒ–ã‚¹ãƒ†ãƒ¼ã‚¿ã‚¹ã®äººé–“ãŒèª­ã‚ã‚‹èª¬æ˜Žæ–‡
SUBSTATUS_DESCRIPTIONS = {
    "charging": "Charging",
    "fully_charged": "Fully Charged",
    "dust_collecting": "Collecting Dust",
    "water_refilling": "Refilling Water",
    "mop_washing_pre": "Pre-washing Mop",
    "mop_washing": "Washing Mop",
    "mop_drying": "Drying Mop",
    "mop_operations": "Mop Operations",
    "cleaning": "Cleaning",
    "paused": "Paused",
    "returning": "Returning to Dock",
    "idle": "Idle",
    "unknown": "Unknown",
    "error": "Error",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    discovered_devices = hass.data[DOMAIN][config_entry.entry_id][CONF_DISCOVERED_DEVICES]

    logger.debug("Got discovered devices: %s", discovered_devices)

    return async_add_devices(
        [RobovacVacuum(coordinator=props[CONF_COORDINATOR]) for device_id, props in discovered_devices.items()]
    )


class RobovacVacuum(CoordinatorEntity, StateVacuumEntity):

    _attr_name = "Eufy Robovac S1 Pro"
    _attr_supported_features = (
        VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.FAN_SPEED
    )

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._last_command = None
        self._last_command_time = 0
        self._was_paused = False  # ä¸€æ™‚åœæ­¢çŠ¶æ…‹ã‚’è¨˜æ†¶
        self._substatus = None  # ã‚µãƒ–ã‚¹ãƒ†ãƒ¼ã‚¿ã‚¹ã‚’ä¿æŒ
        self._detected_state = None  # åˆ¤å®šã•ã‚ŒãŸçŠ¶æ…‹ã‚’ä¿æŒ

    @property
    def icon(self) -> str:
        if self.activity == VacuumActivity.ERROR:
            return "mdi:robot-vacuum-alert"
        return "mdi:robot-vacuum"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            manufacturer="Eufy",
            name=self.name,
            model="S1 Pro (T2080)",
        )

    @property
    def unique_id(self) -> str:
        return self.coordinator.tuya_client.device_id

    @property
    def activity(self) -> VacuumActivity | None:
        """Return the current activity of the vacuum."""
        if not self.coordinator.data:
            return None
            
        # S1 Pro status detection based on actual DPS values
        dps6 = self.coordinator.data.get("6", 0)      # Status indicator 1
        dps153 = self.coordinator.data.get("153", "")  # Actual status indicator (most reliable)
        
        logger.debug(f"Activity check - DPS 6: {dps6}, DPS 153: {dps153}")
        
        # Error detection
        if isinstance(dps6, int) and dps6 >= 100:
            self._detected_state = RobovacState.ERROR
            self._substatus = "error"
            return VacuumActivity.ERROR
        
        # Check DPS 153 status using improved pattern-based detection
        if dps153:
            detected_state, substatus = decode_dps153_to_state(dps153)
            
            # åˆ¤å®šçµæžœã‚’ä¿æŒ
            self._detected_state = detected_state
            self._substatus = substatus
            
            logger.debug(f"Detected state: {detected_state.value}, substatus: {substatus}")
            
            # çŠ¶æ…‹ã«å¿œã˜ãŸãƒ•ãƒ©ã‚°æ›´æ–°ã¨å€¤ã®è¿”å´
            if detected_state == RobovacState.CLEANING:
                self._was_paused = False
                return VacuumActivity.CLEANING
            elif detected_state == RobovacState.PAUSED:
                self._was_paused = True
                return VacuumActivity.PAUSED
            elif detected_state == RobovacState.RETURNING:
                self._was_paused = False
                return VacuumActivity.RETURNING
            elif detected_state == RobovacState.DOCKED:
                self._was_paused = False
                return VacuumActivity.DOCKED
            elif detected_state == RobovacState.ERROR:
                return VacuumActivity.ERROR
            else:  # UNKNOWN
                # æœªçŸ¥ã®çŠ¶æ…‹ã¯IDLEã¨ã—ã¦æ‰±ã†
                return VacuumActivity.IDLE
        
        # DPS 153ãŒåˆ©ç”¨ã§ããªã„å ´åˆã®ãƒ•ã‚©ãƒ¼ãƒ«ãƒãƒƒã‚¯
        # (äº’æ›æ€§ã®ãŸã‚ã«æ—§ãƒ­ã‚¸ãƒƒã‚¯ã‚’ä¸€éƒ¨æ®‹ã™)
        dps152 = self.coordinator.data.get("152", "")
        dps6 = self.coordinator.data.get("6", 0)
        dps7 = self.coordinator.data.get("7", 0)
        
        logger.debug(f"Fallback to DPS 152/6/7 - DPS 152: {dps152}, DPS 6: {dps6}, DPS 7: {dps7}")
        
        if dps152 == S1_PRO_COMMANDS["cleaning"] or dps152 == "AggO":
            self._was_paused = False
            return VacuumActivity.CLEANING
        elif dps152 == S1_PRO_COMMANDS["pause"] or dps152 == "AggN":
            self._was_paused = True
            return VacuumActivity.PAUSED
        elif dps152 == S1_PRO_COMMANDS["return"] or dps152 == "AggG":
            self._was_paused = False
            return VacuumActivity.RETURNING
        
        # Fallback to DPS 6/7 combination
        if dps6 == 2 and dps7 == 3:
            self._was_paused = False
            return VacuumActivity.CLEANING
        elif dps6 == 3 and dps7 == 4:
            self._was_paused = True
            return VacuumActivity.PAUSED
        elif dps6 == 1 and dps7 == 2:
            self._was_paused = False
            return VacuumActivity.RETURNING
        elif dps6 == 0 and dps7 == 0:
            battery = self._get_battery_level() or 0
            if battery >= 95:
                return VacuumActivity.DOCKED
            else:
                return VacuumActivity.IDLE
        
        # ãƒ‡ãƒ•ã‚©ãƒ«ãƒˆ
        return VacuumActivity.IDLE

    def _get_battery_level(self) -> int | None:
        """Battery level in percent.

        Note: Home Assistant removed battery support from vacuum entities.
        The value is kept here for internal logic and exposed as a state
        attribute only.
        """
        if self.coordinator.data:
            # S1 Pro uses DPS 8 for battery level (confirmed from logs)
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

    @property
    def state_attributes(self) -> dict[str, Any]:
        """Return the state attributes of the vacuum."""
        attrs = super().state_attributes or {}
        
        if self.coordinator.data:
            # Only include essential attributes for end users
            if error_code := self.error_code:
                attrs["error_code"] = error_code
            
            if (battery := self._get_battery_level()) is not None:
                attrs["battery_level"] = battery
            
            if self._substatus:
                attrs["substatus"] = self._substatus
                attrs["substatus_description"] = SUBSTATUS_DESCRIPTIONS.get(
                    self._substatus, self._substatus
                )
            
        return attrs
    
    def _is_running(self) -> bool:
        """Check if vacuum is actually running based on multiple indicators."""
        if self.coordinator.data:
            dps153 = self.coordinator.data.get("153", "")
            
            # Check DPS 153 first (most reliable)
            if dps153:
                detected_state, _ = decode_dps153_to_state(dps153)
                return detected_state == RobovacState.CLEANING
            
            # Fallback to DPS 152 if DPS 153 is not available
            dps152 = self.coordinator.data.get("152", "")
            if dps152 == S1_PRO_COMMANDS["cleaning"] or dps152 == "AggO":
                return True
            
            # Final fallback to DPS 6/7
            dps6 = self.coordinator.data.get("6", 0)
            dps7 = self.coordinator.data.get("7", 0)
            return (dps6 == 2 and dps7 == 3)
                
        return False

    @property
    def error_code(self) -> str | None:
        """Return error code if any."""
        if self.coordinator.data:
            # Check if DPS 6 has an error value (high numbers)
            error_code = self.coordinator.data.get("6")
            if isinstance(error_code, int) and error_code >= 100:
                return str(error_code)
        return None

    @property
    def fan_speed(self) -> str | None:
        """Return the current fan speed."""
        if self.coordinator.data:
            # Check DPS 9 first (primary)
            raw_speed = self.coordinator.data.get("9")
            if raw_speed and raw_speed in EUFY_TO_HA_FAN_SPEED_MAP:
                return EUFY_TO_HA_FAN_SPEED_MAP[raw_speed]
            
            # Check DPS 158 as fallback
            raw_speed = self.coordinator.data.get("158")
            if raw_speed and raw_speed in EUFY_TO_HA_FAN_SPEED_MAP:
                return EUFY_TO_HA_FAN_SPEED_MAP[raw_speed]
        return None

    @property
    def fan_speed_list(self) -> list[str]:
        """Get the list of available fan speeds."""
        return ["Quiet", "Standard", "Turbo", "Maximum"]

    async def _send_command(self, command: str) -> None:
        """Send command via DPS 152."""
        try:
            logger.info(f"Sending command via DPS 152: {command}")
            
            # Store last command for debugging
            self._last_command = command
            self._last_command_time = asyncio.get_event_loop().time()
            
            # Send command to DPS 152
            await self.coordinator.tuya_client.async_set({"152": command})
            
            # Wait for response
            await asyncio.sleep(0.5)
            
            # Also set appropriate mode for consistency
            if command == S1_PRO_COMMANDS["start"]:
                await self.coordinator.tuya_client.async_set({"5": "smart"})
            elif command == S1_PRO_COMMANDS["pause"]:
                await self.coordinator.tuya_client.async_set({"5": "pause"})
            elif command == S1_PRO_COMMANDS["return"]:
                await self.coordinator.tuya_client.async_set({"5": "charge"})
            
            # Wait and refresh state
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
            
        except Exception as e:
            logger.error(f"Failed to send command: {e}")
            raise

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the vacuum on and start cleaning."""
        logger.info("Starting vacuum cleaning via DPS 152")
        
        try:
            # Clear pause state
            self._was_paused = False
            
            # Send start command
            await self._send_command(S1_PRO_COMMANDS["start"])
            
            # Wait for state to stabilize
            await asyncio.sleep(2.0)
            
            # Send cleaning command to confirm
            await self._send_command(S1_PRO_COMMANDS["cleaning"])
            
            # Final refresh
            await self.coordinator.async_request_refresh()
            
            if self._is_running():
                logger.info("Vacuum started successfully")
            else:
                logger.warning("Vacuum may not have started properly")
                
        except Exception as e:
            logger.error(f"Failed to start vacuum: {e}")
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the vacuum off."""
        logger.info("Stopping vacuum")
        await self.async_pause()  # For S1 Pro, stop = pause

    async def async_start(self) -> None:
        """Start or resume cleaning."""
        logger.debug("Starting/resuming cleaning")
        
        # Check current state
        activity = self.activity
        current_dps152 = self.coordinator.data.get("152", "")
        current_dps153 = self.coordinator.data.get("153", "")
        
        # dps153ã®åˆ¤å®šã‚’æ–°ã—ã„ãƒ­ã‚¸ãƒƒã‚¯ã§è¡Œã†
        is_paused_by_dps153 = False
        if current_dps153:
            detected_state, _ = decode_dps153_to_state(current_dps153)
            is_paused_by_dps153 = (detected_state == RobovacState.PAUSED)
        
        # ä¸€æ™‚åœæ­¢çŠ¶æ…‹ã‹ã‚‰ã®å†é–‹ã‹ç¢ºèª
        if (activity == VacuumActivity.PAUSED or 
            self._was_paused or 
            is_paused_by_dps153 or
            current_dps152 == S1_PRO_COMMANDS["pause"]):
            # ä¸€æ™‚åœæ­¢ã‹ã‚‰ã®å†é–‹ - cleaningã‚³ãƒžãƒ³ãƒ‰ã®ã¿é€ä¿¡
            logger.info("Resuming from pause - sending cleaning command only")
            
            # æŽƒé™¤ä¸­ã‚³ãƒžãƒ³ãƒ‰ã‚’ç›´æŽ¥é€ä¿¡ï¼ˆã€ŒæŽƒé™¤ã‚’å†é–‹ã€ã®ã‚¢ãƒŠã‚¦ãƒ³ã‚¹ï¼‰
            await self._send_command(S1_PRO_COMMANDS["cleaning"])
            
            # ä¸€æ™‚åœæ­¢ãƒ•ãƒ©ã‚°ã‚’ã‚¯ãƒªã‚¢
            self._was_paused = False
            
        else:
            # æ–°è¦é–‹å§‹ï¼ˆã€ŒæŽƒé™¤ã‚’é–‹å§‹ã€ã®ã‚¢ãƒŠã‚¦ãƒ³ã‚¹ï¼‰
            logger.info("Starting new cleaning session")
            await self.async_turn_on()

    async def async_pause(self) -> None:
        """Pause the vacuum."""
        logger.debug("Pausing vacuum via DPS 152")
        
        try:
            # ä¸€æ™‚åœæ­¢çŠ¶æ…‹ã‚’è¨˜æ†¶
            self._was_paused = True
            
            # Send pause command
            await self._send_command(S1_PRO_COMMANDS["pause"])
            
            logger.info("Vacuum paused")
        except Exception as e:
            logger.error(f"Failed to pause vacuum: {e}")
            self._was_paused = False  # ã‚¨ãƒ©ãƒ¼æ™‚ã¯ãƒªã‚»ãƒƒãƒˆ

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the vacuum - S1 Pro doesn't have stop, using pause instead."""
        logger.debug("Stop requested - using pause for S1 Pro")
        await self.async_pause()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Return vacuum to base."""
        logger.debug("Returning to base via DPS 152")
        
        try:
            # Clear pause state
            self._was_paused = False
            
            # Send return command
            await self._send_command(S1_PRO_COMMANDS["return"])
            
            logger.info("Return to base command sent")
        except Exception as e:
            logger.error(f"Failed to return to base: {e}")

    async def async_clean_spot(self, **kwargs: Any) -> None:
        """Perform a spot clean-up - Not supported on S1 Pro."""
        logger.info("Spot cleaning is not supported on S1 Pro - ignoring request")
        # Do nothing, but don't raise an error for compatibility

    async def async_locate(self, **kwargs: Any) -> None:
        """Locate the vacuum (make it beep) - Not supported on S1 Pro."""
        logger.info("Locate function is not supported on S1 Pro - ignoring request")
        # Do nothing, but don't raise an error for compatibility

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set the vacuum's fan speed."""
        if fan_speed not in HA_TO_EUFY_FAN_SPEED_MAP:
            logger.error(f"Invalid fan speed: {fan_speed}")
            return
            
        logger.debug(f"Setting fan speed to {fan_speed}")
        
        try:
            dps9_value, dps158_value = HA_TO_EUFY_FAN_SPEED_MAP[fan_speed]
            
            # Set both DPS values for S1 Pro
            await self.coordinator.tuya_client.async_set({
                "9": dps9_value,
                "158": dps158_value
            })
            
            logger.info(f"Fan speed set to {fan_speed}")
        except Exception as e:
            logger.error(f"Failed to set fan speed: {e}")
