import logging
from datetime import timedelta
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from tuya_sharing import (
    CustomerDevice,
    LoginControl,
    Manager,
    SharingDeviceListener,
    SharingTokenListener,
)

from .const import (
    CONF_DEVICE_CID,
    CONF_ENDPOINT,
    CONF_LOCAL_KEY,
    CONF_TERMINAL_ID,
    DATA_CLOUD,
    DOMAIN,
    TUYA_CLIENT_ID,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    TUYA_RESPONSE_QR_CODE,
    TUYA_RESPONSE_RESULT,
    TUYA_RESPONSE_SUCCESS,
    TUYA_SCHEMA,
)

_LOGGER = logging.getLogger(__name__)

AUTH_CACHE_STORE_VERSION = 1
AUTH_CACHE_STORE_KEY = f"{DOMAIN}.auth_cache"
CLOUD_SYNC_INTERVAL = timedelta(minutes=5)

HUB_CATEGORIES = [
    "wgsxj",  # Gateway camera
    "lyqwg",  # Router
    "bywg",  # IoT edge gateway
    "zigbee",  # Gateway
    "wg2",  # Gateway
    "dgnzk",  # Multi-function controller
    "videohub",  # Videohub
    "xnwg",  # Virtual gateway
    "qtyycp",  # Voice gateway composite solution
    "alexa_yywg",  # Gateway with Alexa
    "gywg",  # Industrial gateway
    "cnwg",  # Energy gateway
    "wfcon",  # new type of Zigbee gateway
]


def _get_auth_store(hass: HomeAssistant) -> Store | None:
    """Return a Home Assistant Store for cloud auth cache if available."""
    config = getattr(hass, "config", None)
    if config is None or not hasattr(config, "path"):
        return None
    try:
        if not isinstance(config.path("."), str):
            return None
        return Store(hass, AUTH_CACHE_STORE_VERSION, AUTH_CACHE_STORE_KEY)
    except Exception:
        return None


async def async_load_auth_cache(hass: HomeAssistant) -> dict[str, Any] | None:
    """Load cached cloud authentication from memory or persistent storage."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if cached := domain_data.get("auth_cache"):
        return cached
    store = _get_auth_store(hass)
    if store is None:
        return None
    try:
        loaded = await store.async_load()
        if isinstance(loaded, dict) and loaded.get("user_code"):
            domain_data["auth_cache"] = loaded
            return loaded
    except Exception as err:
        _LOGGER.debug("Failed to load cloud auth cache: %s", err)
    return None


async def async_save_auth_cache(
    hass: HomeAssistant, auth_data: dict[str, Any] | None
) -> None:
    """Persist or clear cloud authentication in storage."""
    store = _get_auth_store(hass)
    if store is None:
        return
    try:
        if auth_data:
            await store.async_save(auth_data)
        else:
            await store.async_remove()
    except Exception as err:
        _LOGGER.debug("Failed to save cloud auth cache: %s", err)


async def _async_poll_restored_subdevice(hass: HomeAssistant, local_dev) -> None:
    """Trigger a local status query after a subdevice comes back online."""
    async with local_dev._api_lock:
        await hass.async_add_executor_job(lambda: local_dev._api.status(nowait=True))


@callback
def _apply_cloud_subdevice_online(
    hass: HomeAssistant, cloud_device: CustomerDevice
) -> None:
    """Apply cloud online/offline state to matching local gateway subdevices."""
    cid = getattr(cloud_device, "node_id", None) or getattr(cloud_device, "uuid", None)
    online = getattr(cloud_device, "online", None)
    if not cid or not isinstance(online, bool):
        return

    domain_data = hass.data.get(DOMAIN, {})
    if not isinstance(domain_data, dict):
        return

    for bucket in domain_data.values():
        if not isinstance(bucket, dict):
            continue
        local_dev = bucket.get("device")
        if local_dev is None or getattr(local_dev, "dev_cid", None) != cid:
            continue
        was_online = getattr(local_dev, "_online", True)
        local_dev.set_online(online)
        if online and not was_online and hasattr(hass, "async_create_task"):
            hass.async_create_task(_async_poll_restored_subdevice(hass, local_dev))


class Cloud:
    """Optional Tuya cloud interface for getting device information."""

    def __init__(self, hass: HomeAssistant):
        self.__login_control = LoginControl()
        self.__authentication = {}
        self.__user_code = None
        self.__qr_code = None
        self.__hass = hass
        self.__error_code = None
        self.__error_msg = None
        # Restore cached authentication
        if cached := self.__hass.data[DOMAIN].get("auth_cache"):
            self.__authentication = cached

    async def async_get_qr_code(self, user_code: str | None = None) -> bool:
        """Get QR code from Tuya server for user code authentication."""
        if not user_code:
            user_code = self.__user_code
            if not user_code:
                _LOGGER.error("Cannot get QR code without a user code")
                return False, {TUYA_RESPONSE_MSG: "QR code requires a user code"}

        response = await self.__hass.async_add_executor_job(
            self.__login_control.qr_code,
            TUYA_CLIENT_ID,
            TUYA_SCHEMA,
            user_code,
        )
        if response.get(TUYA_RESPONSE_SUCCESS, False):
            self.__user_code = user_code
            self.__qr_code = response[TUYA_RESPONSE_RESULT][TUYA_RESPONSE_QR_CODE]
            return self.__qr_code

        _LOGGER.error("Failed to get QR code: %s", response)
        self.__error_code = response.get(TUYA_RESPONSE_CODE, {})
        self.__error_msg = response.get(TUYA_RESPONSE_MSG, "Unknown error")

        return False

    async def async_login(self) -> bool:
        """Login to the Tuya cloud."""
        if not self.__user_code or not self.__qr_code:
            _LOGGER.warning("Login attempted without successful QR scan")
            return False, {}

        success, info = await self.__hass.async_add_executor_job(
            self.__login_control.login_result,
            self.__qr_code,
            TUYA_CLIENT_ID,
            self.__user_code,
        )
        if success:
            self.__authentication = {
                "user_code": self.__user_code,
                "terminal_id": info[CONF_TERMINAL_ID],
                "endpoint": info[CONF_ENDPOINT],
                "token_info": {
                    "t": info["t"],
                    "uid": info["uid"],
                    "expire_time": info["expire_time"],
                    "access_token": info["access_token"],
                    "refresh_token": info["refresh_token"],
                },
            }
            self.__hass.data[DOMAIN]["auth_cache"] = self.__authentication
            await async_save_auth_cache(self.__hass, self.__authentication)
        else:
            _LOGGER.warning("Login failed: %s", info)
            self.__error_code = info.get(TUYA_RESPONSE_CODE, {})
            self.__error_msg = info.get(TUYA_RESPONSE_MSG, "Unknown error")
            # Ensure expired authentication is cleared on next attempt
            self.__hass.data[DOMAIN]["auth_cache"] = None
            self.__authentication = {}
            await async_save_auth_cache(self.__hass, None)
        return success

    async def async_get_devices(self) -> dict[str, Any]:
        """Get all devices associated with the account."""
        token_listener = TokenListener(self.__hass)
        manager = Manager(
            TUYA_CLIENT_ID,
            self.__authentication["user_code"],
            self.__authentication["terminal_id"],
            self.__authentication["endpoint"],
            self.__authentication["token_info"],
            token_listener,
        )

        listener = DeviceListener(self.__hass, manager)
        manager.add_device_listener(listener)

        # Get all devices from Tuya cloud
        await self.__hass.async_add_executor_job(manager.update_device_cache)

        # Register known device IDs
        cloud_devices = {}
        domain_data = self.__hass.data.get(DOMAIN)
        for device in manager.device_map.values():
            _apply_cloud_subdevice_online(self.__hass, device)
            cloud_device = {
                "category": device.category,
                "id": device.id,
                "ip": device.ip,
                CONF_LOCAL_KEY: device.local_key
                if hasattr(device, CONF_LOCAL_KEY)
                else "",
                "name": device.name,
                "node_id": device.node_id if hasattr(device, "node_id") else "",
                "online": device.online,
                "product_id": device.product_id,
                "product_name": device.product_name,
                "uid": device.uid,
                "uuid": device.uuid,
                "support_local": device.support_local,
                CONF_DEVICE_CID: None,
                "version": None,
                "is_hub": (
                    device.category in HUB_CATEGORIES
                    or not hasattr(device, "local_key")
                ),
            }
            _LOGGER.debug("Found device: %s", cloud_device["product_name"])

            existing_id = domain_data.get(cloud_device["id"]) if domain_data else None
            existing_uuid = (
                domain_data.get(cloud_device["uuid"]) if domain_data else None
            )
            existing = existing_id or existing_uuid
            cloud_device["exists"] = existing and existing.get("device")
            if hasattr(device, "node_id"):
                index = "/".join(
                    [
                        cloud_device["id"],
                        cloud_device["node_id"],
                    ]
                )
            else:
                index = cloud_device["id"]
            cloud_devices[index] = cloud_device

        return cloud_devices

    async def async_get_datamodel(self, device_id) -> dict[str, Any] | None:
        """Get the data model for the specified device (QueryThingsDataModel)."""
        token_listener = TokenListener(self.__hass)
        manager = Manager(
            TUYA_CLIENT_ID,
            self.__authentication["user_code"],
            self.__authentication["terminal_id"],
            self.__authentication["endpoint"],
            self.__authentication["token_info"],
            token_listener,
        )
        response = await self.__hass.async_add_executor_job(
            manager.customer_api.get,
            f"/v1.0/m/life/devices/{device_id}/status",
        )
        _LOGGER.debug("Datamodel response: %s", response)
        if response.get("result"):
            response = response["result"]
        transform = []
        for entry in response.get("dpStatusRelationDTOS"):
            if entry["supportLocal"]:
                transform.append(
                    {
                        "id": entry["dpId"],
                        "name": entry["dpCode"],
                        "type": entry["valueType"],
                        "format": entry["valueDesc"],
                        "enumMap": entry["enumMappingMap"],
                    }
                )
        return transform

    def logout(self) -> None:
        """Logout from the Tuya cloud."""
        _LOGGER.debug("Logging out from Tuya cloud")
        # Clear authentication cache
        self.__hass.data[DOMAIN]["auth_cache"] = None
        self.__authentication = {}
        async_stop_cloud_monitor(self.__hass)
        if _get_auth_store(self.__hass) is not None and hasattr(
            self.__hass, "async_create_task"
        ):
            self.__hass.async_create_task(async_save_auth_cache(self.__hass, None))

    @property
    def is_authenticated(self) -> bool:
        """Is the cloud account authenticated?"""
        return True if self.__authentication else False

    @property
    def last_error(self) -> dict[str, Any] | None:
        """The last cloud error code and message, if any."""
        if self.__error_code is not None:
            return {
                TUYA_RESPONSE_MSG: self.__error_msg,
                TUYA_RESPONSE_CODE: self.__error_code,
            }


class DeviceListener(SharingDeviceListener):
    """Device update listener."""

    def __init__(
        self,
        hass: HomeAssistant,
        manager: Manager,
    ):
        self.__hass = hass
        self._manager = manager

    def update_device(
        self,
        device: CustomerDevice,
        updated_status_properties: list[str] | None = None,
        dp_timestamps: dict | None = None,
    ) -> None:
        """Device status has updated."""
        _LOGGER.debug(
            "Received update for device %s: %s (properties %s)",
            device.id,
            self._manager.device_map[device.id].status
            if device.id in self._manager.device_map
            else None,
            updated_status_properties,
        )
        loop = getattr(self.__hass, "loop", None)
        if loop is not None and hasattr(loop, "call_soon_threadsafe"):
            loop.call_soon_threadsafe(
                _apply_cloud_subdevice_online, self.__hass, device
            )
        else:
            _apply_cloud_subdevice_online(self.__hass, device)

    def add_device(self, device: CustomerDevice) -> None:
        """A new device has been added."""
        _LOGGER.debug(
            "Received add device %s: %s",
            device.id,
            self._manager.device_map[device.id].status,
        )

    def remove_device(self, device_id: str) -> None:
        """A device has been removed."""
        _LOGGER.debug(
            "Received remove device %s: %s",
            device_id,
            self._manager.device_map[device_id].status,
        )


class TokenListener(SharingTokenListener):
    """Listener for upstream token updates."""

    def __init__(self, hass: HomeAssistant):
        self.__hass = hass

    def update_token(self, token_info: dict[str, Any]) -> None:
        """Update the token information."""
        _LOGGER.debug("Token updated")
        domain_data = getattr(self.__hass, "data", {}).get(DOMAIN)
        if isinstance(domain_data, dict):
            auth_cache = domain_data.get("auth_cache")
            if isinstance(auth_cache, dict) and isinstance(
                auth_cache.get("token_info"), dict
            ):
                auth_cache["token_info"].update(token_info)
                loop = getattr(self.__hass, "loop", None)
                if (
                    _get_auth_store(self.__hass) is not None
                    and loop is not None
                    and hasattr(loop, "call_soon_threadsafe")
                ):
                    loop.call_soon_threadsafe(
                        self.__hass.async_create_task,
                        async_save_auth_cache(self.__hass, auth_cache),
                    )


class TuyaCloudMonitor:
    """Monitor Tuya Cloud for subdevice online/offline status updates."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._manager: Manager | None = None
        self._unsub_interval = None
        self._unsub_stop = None
        self._starting = False

    async def async_start(self) -> None:
        """Initialize the Tuya Sharing manager and start MQTT + periodic sync."""
        if self._manager is not None or self._starting:
            self.sync_devices()
            return

        auth = await async_load_auth_cache(self._hass)
        if not auth:
            return

        self._starting = True
        try:
            token_listener = TokenListener(self._hass)
            manager = Manager(
                TUYA_CLIENT_ID,
                auth["user_code"],
                auth["terminal_id"],
                auth["endpoint"],
                auth["token_info"],
                token_listener,
            )
            listener = DeviceListener(self._hass, manager)
            manager.add_device_listener(listener)

            await self._hass.async_add_executor_job(manager.update_device_cache)
            for dev in manager.device_map.values():
                dev.set_up = True
            await self._hass.async_add_executor_job(manager.refresh_mq)

            self._manager = manager
            self.sync_devices()

            if self._unsub_interval is None:
                self._unsub_interval = async_track_time_interval(
                    self._hass, self._async_periodic_sync, CLOUD_SYNC_INTERVAL
                )
            if self._unsub_stop is None and hasattr(self._hass, "bus"):
                self._unsub_stop = self._hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self.async_stop
                )
        except Exception as err:
            _LOGGER.warning("Failed to start Tuya cloud status monitor: %s", err)
        finally:
            self._starting = False

    @callback
    def sync_devices(self) -> None:
        """Apply known cloud online states to all registered local subdevices."""
        if self._manager is None:
            return
        for dev in self._manager.device_map.values():
            _apply_cloud_subdevice_online(self._hass, dev)

    async def _async_periodic_sync(self, now=None) -> None:
        """Periodically refresh the cloud device cache and sync online states."""
        if self._manager is None:
            return
        try:
            await self._hass.async_add_executor_job(self._manager.update_device_cache)
            self.sync_devices()
        except Exception as err:
            _LOGGER.debug("Periodic Tuya cloud status sync failed: %s", err)

    @callback
    def async_stop(self, event=None) -> None:
        """Stop the cloud monitor and MQTT listener."""
        for attr in ("_unsub_interval", "_unsub_stop"):
            unsub = getattr(self, attr)
            if unsub is not None:
                unsub()
                setattr(self, attr, None)
        if self._manager is not None and self._manager.mq is not None:
            self._manager.mq.stop()
            self._manager.mq = None
        self._manager = None


async def async_start_cloud_monitor(hass: HomeAssistant) -> None:
    """Start or sync the shared cloud subdevice monitor if subdevices exist."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    has_subdevices = any(
        isinstance(b, dict)
        and b.get("device") is not None
        and getattr(b["device"], "dev_cid", None)
        for b in domain_data.values()
    )
    if not has_subdevices:
        return

    monitor: TuyaCloudMonitor | None = domain_data.get(DATA_CLOUD)
    if monitor is None:
        monitor = TuyaCloudMonitor(hass)
        domain_data[DATA_CLOUD] = monitor
    await monitor.async_start()


@callback
def async_stop_cloud_monitor(hass: HomeAssistant) -> None:
    """Stop the shared cloud subdevice monitor if running."""
    domain_data = hass.data.get(DOMAIN, {})
    if not isinstance(domain_data, dict):
        return
    monitor = domain_data.pop(DATA_CLOUD, None)
    if monitor is not None:
        monitor.async_stop()
