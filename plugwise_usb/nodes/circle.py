"""Plugwise Circle node class."""

from __future__ import annotations

from asyncio import Task, create_task, gather
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
import logging
from typing import Any, TypeVar, cast

from ..api import NodeEvent, NodeFeature, NodeInfo
from ..constants import (
    MAX_TIME_DRIFT,
    MINIMAL_POWER_UPDATE,
    PULSES_PER_KW_SECOND,
    SECOND_IN_NANOSECONDS,
)
from ..exceptions import NodeError
from ..messages.requests import (
    CircleClockGetRequest,
    CircleClockSetRequest,
    CircleEnergyLogsRequest,
    CirclePowerUsageRequest,
    CircleRelayInitStateRequest,
    CircleRelaySwitchRequest,
    EnergyCalibrationRequest,
    NodeInfoRequest,
)
from ..messages.responses import (
    CircleClockResponse,
    CircleEnergyLogsResponse,
    CirclePowerUsageResponse,
    CircleRelayInitStateResponse,
    EnergyCalibrationResponse,
    NodeInfoResponse,
    NodeResponse,
    NodeResponseType,
)
from ..nodes import EnergyStatistics, PlugwiseNode, PowerStatistics
from .helpers import EnergyCalibration, raise_not_loaded
from .helpers.firmware import CIRCLE_FIRMWARE_SUPPORT
from .helpers.pulses import PulseLogRecord, calc_log_address

CACHE_CURRENT_LOG_ADDRESS = "current_log_address"
CACHE_CALIBRATION_GAIN_A = "calibration_gain_a"
CACHE_CALIBRATION_GAIN_B = "calibration_gain_b"
CACHE_CALIBRATION_NOISE = "calibration_noise"
CACHE_CALIBRATION_TOT = "calibration_tot"
CACHE_ENERGY_COLLECTION = "energy_collection"
CACHE_RELAY = "relay"
CACHE_RELAY_INIT = "relay_init"

FuncT = TypeVar("FuncT", bound=Callable[..., Any])
_LOGGER = logging.getLogger(__name__)


def raise_calibration_missing(func: FuncT) -> FuncT:
    """Validate energy calibration settings are available."""

    @wraps(func)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if args[0].calibrated is None:
            raise NodeError("Energy calibration settings are missing")
        return func(*args, **kwargs)

    return cast(FuncT, decorated)


class PlugwiseCircle(PlugwiseNode):
    """Plugwise Circle node."""

    _retrieve_energy_logs_task: None | Task = None
    _last_energy_log_requested: bool = False

    @property
    def calibrated(self) -> bool:
        """State of calibration."""
        if self._calibration is not None:
            return True
        return False

    @property
    def energy(self) -> EnergyStatistics | None:
        """"Return energy statistics."""
        return self._energy_counters.energy_statistics

    @property
    @raise_not_loaded
    def relay(self) -> bool:
        """Current value of relay."""
        return bool(self._relay)

    @relay.setter
    @raise_not_loaded
    def relay(self, state: bool) -> None:
        """Request to change relay state."""
        create_task(self.switch_relay(state))

    @raise_not_loaded
    async def relay_off(self) -> None:
        """Switch relay off."""
        await self.switch_relay(False)

    @raise_not_loaded
    async def relay_on(self) -> None:
        """Switch relay on."""
        await self.switch_relay(True)

    @property
    def relay_init(
        self,
    ) -> bool | None:
        """Request the relay states at startup/power-up."""
        if NodeFeature.RELAY_INIT not in self._features:
            raise NodeError(
                f"Initial state of relay is not supported for device {self.name}"
            )
        return self._relay_init_state

    @relay_init.setter
    def relay_init(self, state: bool) -> None:
        """Request to configure relay states at startup/power-up."""
        if NodeFeature.RELAY_INIT not in self._features:
            raise NodeError(
                "Configuring initial state of relay "
                + f"is not supported for device {self.name}"
            )
        create_task(self._relay_init_set(state))

    async def calibration_update(self) -> bool:
        """Retrieve and update calibration settings. Returns True if successful."""
        _LOGGER.debug(
            "Start updating energy calibration for %s",
            self._mac_in_str,
        )
        calibration_response: EnergyCalibrationResponse | None = (
            await self._send(EnergyCalibrationRequest(self._mac_in_bytes))
        )
        if calibration_response is None:
            _LOGGER.warning(
                "Retrieving energy calibration information for %s failed",
                self.name,
            )
            await self._available_update_state(False)
            return False
        await self._available_update_state(True)
        await self._calibration_update_state(
            calibration_response.gain_a,
            calibration_response.gain_b,
            calibration_response.off_noise,
            calibration_response.off_tot,
        )
        _LOGGER.debug(
            "Updating energy calibration for %s succeeded",
            self._mac_in_str,
        )
        return True

    async def _calibration_load_from_cache(self) -> bool:
        """Load calibration settings from cache."""
        cal_gain_a: float | None = None
        cal_gain_b: float | None = None
        cal_noise: float | None = None
        cal_tot: float | None = None
        if (gain_a := self._get_cache(CACHE_CALIBRATION_GAIN_A)) is not None:
            cal_gain_a = float(gain_a)
        if (gain_b := self._get_cache(CACHE_CALIBRATION_GAIN_B)) is not None:
            cal_gain_b = float(gain_b)
        if (noise := self._get_cache(CACHE_CALIBRATION_NOISE)) is not None:
            cal_noise = float(noise)
        if (tot := self._get_cache(CACHE_CALIBRATION_TOT)) is not None:
            cal_tot = float(tot)

        # Restore calibration
        result = await self._calibration_update_state(
            cal_gain_a,
            cal_gain_b,
            cal_noise,
            cal_tot,
        )
        if result:
            _LOGGER.debug(
                "Restore calibration settings from cache for %s was successful",
                self._mac_in_str
            )
            return True
        _LOGGER.info(
            "Failed to restore calibration settings from cache for %s",
            self.name
        )
        return False

    async def _calibration_update_state(
        self,
        gain_a: float | None,
        gain_b: float | None,
        off_noise: float | None,
        off_tot: float | None,
    ) -> bool:
        """Process new energy calibration settings. Returns True if successful."""
        if (
            gain_a is None or
            gain_b is None or
            off_noise is None or
            off_tot is None
        ):
            return False
        self._calibration = EnergyCalibration(
            gain_a=gain_a,
            gain_b=gain_b,
            off_noise=off_noise,
            off_tot=off_tot
        )
        # Forward calibration config to energy collection
        self._energy_counters.calibration = self._calibration

        if self._cache_enabled:
            self._set_cache(CACHE_CALIBRATION_GAIN_A, gain_a)
            self._set_cache(CACHE_CALIBRATION_GAIN_B, gain_b)
            self._set_cache(CACHE_CALIBRATION_NOISE, off_noise)
            self._set_cache(CACHE_CALIBRATION_TOT, off_tot)
            await self.save_cache()
        return True

    @raise_calibration_missing
    async def power_update(self) -> PowerStatistics | None:
        """Update the current power usage statistics.

        Return power usage or None if retrieval failed
        """
        # Debounce power
        if self.skip_update(self._power, MINIMAL_POWER_UPDATE):
            return self._power

        request = CirclePowerUsageRequest(self._mac_in_bytes)
        response: CirclePowerUsageResponse | None = await self._send(request)
        if response is None or response.timestamp is None:
            _LOGGER.debug(
                "No response for async_power_update() for %s",
                self._mac_in_str
            )
            await self._available_update_state(False)
            return None
        await self._available_update_state(True)

        # Update power stats
        self._power.last_second = self._calc_watts(
            response.pulse_1s, 1, response.offset
        )
        self._power.last_8_seconds = self._calc_watts(
            response.pulse_8s, 8, response.offset
        )
        self._power.timestamp = response.timestamp
        await self.publish_feature_update_to_subscribers(
            NodeFeature.POWER, self._power
        )

        # Forward pulse interval counters to pulse Collection
        self._energy_counters.add_pulse_stats(
            response.consumed_counter,
            response.produced_counter,
            response.timestamp,
        )
        await self.publish_feature_update_to_subscribers(
            NodeFeature.ENERGY, self._energy_counters.energy_statistics
        )
        return self._power

    @raise_not_loaded
    @raise_calibration_missing
    async def energy_update(
        self
    ) -> EnergyStatistics | None:
        """Return updated energy usage statistics."""
        if self._current_log_address is None:
            _LOGGER.debug(
                "Unable to update energy logs for node %s because last_log_address is unknown.",
                self._mac_in_str,
            )
            if await self.node_info_update() is None:
                _LOGGER.warning("Unable to return energy statistics for %s, because it is not responding", self.name)
                return None
        # request node info update every 30 minutes.
        elif not self.skip_update(self._node_info, 1800):
            if await self.node_info_update() is None:
                _LOGGER.warning("Unable to return energy statistics for %s, because it is not responding", self.name)
                return None

        # Always request last energy log records at initial startup
        if not self._last_energy_log_requested:
            self._last_energy_log_requested = await self.energy_log_update(self._current_log_address)

        if self._energy_counters.log_rollover:
            if await self.node_info_update() is None:
                _LOGGER.debug(
                    "async_energy_update | %s | Log rollover | node_info_update failed", self._mac_in_str,
                )
                return None

            if not await self.energy_log_update(self._current_log_address):
                _LOGGER.debug(
                    "async_energy_update | %s | Log rollover | energy_log_update failed", self._mac_in_str,
                )
                return None

            if self._energy_counters.log_rollover:
                # Retry with previous log address as Circle node pointer to self._current_log_address
                # could be rolled over while the last log is at previous address/slot
                _prev_log_address, _ = calc_log_address(self._current_log_address, 1, -4)
                if not await self.energy_log_update(_prev_log_address):
                    _LOGGER.debug(
                        "async_energy_update | %s | Log rollover | energy_log_update %s failed",
                        self._mac_in_str,
                        _prev_log_address,
                    )
                    return None

        if (
            missing_addresses := self._energy_counters.log_addresses_missing
        ) is not None:
            if len(missing_addresses) == 0:
                await self.power_update()
                _LOGGER.debug(
                    "async_energy_update for %s | no missing log records",
                    self._mac_in_str,
                )
                return self._energy_counters.energy_statistics
            if len(missing_addresses) == 1:
                if await self.energy_log_update(missing_addresses[0]):
                    await self.power_update()
                    _LOGGER.debug(
                        "async_energy_update for %s | single energy log is missing | %s",
                        self._mac_in_str,
                        missing_addresses,
                    )
                    return self._energy_counters.energy_statistics

        # Create task to request remaining missing logs
        if (
            self._retrieve_energy_logs_task is None
            or self._retrieve_energy_logs_task.done()
        ):
            _LOGGER.debug(
                "Create task to update energy logs for node %s",
                self._mac_in_str,
            )
            self._retrieve_energy_logs_task = create_task(self.get_missing_energy_logs())
        else:
            _LOGGER.debug(
                "Skip creating task to update energy logs for node %s",
                self._mac_in_str,
            )
        _LOGGER.warning("Unable to return energy statistics for %s, collecting required data...", self.name)
        return None

    async def get_missing_energy_logs(self) -> None:
        """Task to retrieve missing energy logs."""

        self._energy_counters.update()
        if self._energy_counters.log_addresses_missing is None:
            _LOGGER.debug(
                "Start with initial energy request for the last 10 log addresses for node %s.",
                self._mac_in_str,
            )
            total_addresses = 11
            log_address = self._current_log_address
            log_update_tasks = []
            while total_addresses > 0:
                log_update_tasks.append(self.energy_log_update(log_address))
                log_address, _ = calc_log_address(log_address, 1, -4)
                total_addresses -= 1

            if not all(await gather(*log_update_tasks)):
                _LOGGER.info(
                    "Failed to request one or more update energy log for %s",
                    self._mac_in_str,
                )

            if self._cache_enabled:
                await self._energy_log_records_save_to_cache()
            return
        if self._energy_counters.log_addresses_missing is not None:
            _LOGGER.debug("Task created to get missing logs of %s", self._mac_in_str)
        if (
            missing_addresses := self._energy_counters.log_addresses_missing
        ) is not None:
            _LOGGER.debug(
                "Task Request %s missing energy logs for node %s | %s",
                str(len(missing_addresses)),
                self._mac_in_str,
                str(missing_addresses),
            )

            missing_addresses = sorted(missing_addresses, reverse=True)
            await gather(
                *[
                    self.energy_log_update(address)
                    for address in missing_addresses
                ]
            )

        if self._cache_enabled:
            await self._energy_log_records_save_to_cache()

    async def energy_log_update(self, address: int) -> bool:
        """Request energy log statistics from node. Returns true if successful."""
        _LOGGER.info(
            "Request of energy log at address %s for node %s",
            str(address),
            self.name,
        )
        request = CircleEnergyLogsRequest(self._mac_in_bytes, address)
        response: CircleEnergyLogsResponse | None = None
        if (response := await self._send(request)) is None:
            _LOGGER.debug(
                "Retrieving of energy log at address %s for node %s failed",
                str(address),
                self._mac_in_str,
            )
            return False

        await self._available_update_state(True)
        energy_record_update = False

        # Forward historical energy log information to energy counters
        # Each response message contains 4 log counters (slots) of the
        # energy pulses collected during the previous hour of given timestamp
        for _slot in range(4, 0, -1):
            _log_timestamp: datetime | None = getattr(
                response, "logdate%d" % (_slot,)
            ).value
            _log_pulses: int = getattr(response, "pulses%d" % (_slot,)).value
            if _log_timestamp is None:
                self._energy_counters.add_empty_log(response.log_address, _slot)
            else:
                if await self._energy_log_record_update_state(
                    response.log_address,
                    _slot,
                    _log_timestamp.replace(tzinfo=UTC),
                    _log_pulses,
                    import_only=True
                ):
                    energy_record_update = True
        self._energy_counters.update()
        if energy_record_update: 
            await self.save_cache()
        return True

    async def _energy_log_records_load_from_cache(self) -> bool:
        """Load energy_log_record from cache."""
        if self._get_cache(CACHE_ENERGY_COLLECTION) is None:
            _LOGGER.info(
                "Failed to restore energy log records from cache for node %s",
                self.name
            )
            return False
        restored_logs: dict[int, list[int]] = {}
        log_data = self._get_cache(CACHE_ENERGY_COLLECTION).split("|")
        for log_record in log_data:
            log_fields = log_record.split(":")
            if len(log_fields) == 4:
                timestamp_energy_log = log_fields[2].split("-")
                if len(timestamp_energy_log) == 6:
                    address = int(log_fields[0])
                    slot = int(log_fields[1])
                    self._energy_counters.add_pulse_log(
                        address=address,
                        slot=slot,
                        timestamp=datetime(
                            year=int(timestamp_energy_log[0]),
                            month=int(timestamp_energy_log[1]),
                            day=int(timestamp_energy_log[2]),
                            hour=int(timestamp_energy_log[3]),
                            minute=int(timestamp_energy_log[4]),
                            second=int(timestamp_energy_log[5]),
                            tzinfo=UTC
                        ),
                        pulses=int(log_fields[3]),
                        import_only=True,
                    )
                    if restored_logs.get(address) is None:
                        restored_logs[address] = []
                    restored_logs[address].append(slot)

        self._energy_counters.update()

        # Create task to retrieve remaining (missing) logs
        if self._energy_counters.log_addresses_missing is None:
            return False
        if len(self._energy_counters.log_addresses_missing) > 0:
            missing_addresses = sorted(
                self._energy_counters.log_addresses_missing, reverse=True
            )[:5]
            for address in missing_addresses:
                _LOGGER.debug(
                    "Create task to request energy log %s for %s",
                    address,
                    self._mac_in_str
                )
                create_task(self.energy_log_update(address))
            return False
        return True

    async def _energy_log_records_save_to_cache(self) -> None:
        """Save currently collected energy logs to cached file."""
        if not self._cache_enabled:
            return
        logs: dict[int, dict[int, PulseLogRecord]] = (
            self._energy_counters.get_pulse_logs()
        )
        cached_logs = ""
        for address in sorted(logs.keys(), reverse=True):
            for slot in sorted(logs[address].keys(), reverse=True):
                log = logs[address][slot]
                if cached_logs != "":
                    cached_logs += "|"
                cached_logs += f"{address}:{slot}:{log.timestamp.year}"
                cached_logs += f"-{log.timestamp.month}-{log.timestamp.day}"
                cached_logs += f"-{log.timestamp.hour}-{log.timestamp.minute}"
                cached_logs += f"-{log.timestamp.second}:{log.pulses}"
        self._set_cache(CACHE_ENERGY_COLLECTION, cached_logs)

    async def _energy_log_record_update_state(
        self,
        address: int,
        slot: int,
        timestamp: datetime,
        pulses: int,
        import_only: bool = False,
    ) -> bool:
        """Process new energy log record. Returns true if record is new or changed."""
        self._energy_counters.add_pulse_log(
            address,
            slot,
            timestamp,
            pulses,
            import_only=import_only
        )
        if not self._cache_enabled:
            return False
        log_cache_record = f"{address}:{slot}:{timestamp.year}"
        log_cache_record += f"-{timestamp.month}-{timestamp.day}"
        log_cache_record += f"-{timestamp.hour}-{timestamp.minute}"
        log_cache_record += f"-{timestamp.second}:{pulses}"
        if (cached_logs := self._get_cache(CACHE_ENERGY_COLLECTION)) is not None:
            if log_cache_record not in cached_logs:
                _LOGGER.debug(
                    "Add logrecord (%s, %s) to log cache of %s",
                    str(address),
                    str(slot),
                    self._mac_in_str
                )
                self._set_cache(
                    CACHE_ENERGY_COLLECTION, cached_logs + "|" + log_cache_record
                )
                return True
            return False
        _LOGGER.debug(
            "No existing energy collection log cached for %s",
            self._mac_in_str
        )
        self._set_cache(CACHE_ENERGY_COLLECTION, log_cache_record)
        return True

    async def switch_relay(self, state: bool) -> bool | None:
        """Switch state of relay.

        Return new state of relay
        """
        _LOGGER.debug("switch_relay() start")
        response: NodeResponse | None = await self._send(
            CircleRelaySwitchRequest(self._mac_in_bytes, state),
        )
        if (
            response is None
            or response.ack_id == NodeResponseType.RELAY_SWITCH_FAILED
        ):
            _LOGGER.warning(
                "Request to switch relay for %s failed",
                self.name,
            )
            return None

        if response.ack_id == NodeResponseType.RELAY_SWITCHED_OFF:
            await self._relay_update_state(
                state=False, timestamp=response.timestamp
            )
            return False
        if response.ack_id == NodeResponseType.RELAY_SWITCHED_ON:
            await self._relay_update_state(
                state=True, timestamp=response.timestamp
            )
            return True
        _LOGGER.warning(
            "Unexpected NodeResponseType %s response for CircleRelaySwitchRequest at node %s...",
            str(response.ack_id),
            self.name,
        )
        return None

    async def _relay_load_from_cache(self) -> bool:
        """Load relay state from cache."""
        if self._relay is not None:
            # State already known, no need to load from cache
            return True
        if (cached_relay_data := self._get_cache(CACHE_RELAY)) is not None:
            _LOGGER.debug(
                "Restore relay state cache for node %s",
                self._mac_in_str
            )
            relay_state = False
            if cached_relay_data == "True":
                relay_state = True
            await self._relay_update_state(relay_state)
            return True
        _LOGGER.debug(
            "Failed to restore relay state from cache for node %s, try to request node info...",
            self._mac_in_str
        )
        if await self.node_info_update() is None:
            return False
        return True

    async def _relay_update_state(
        self, state: bool, timestamp: datetime | None = None
    ) -> None:
        """Process relay state update."""
        self._relay_state.relay_state = state
        self._relay_state.timestamp = timestamp
        state_update = False
        if state:
            self._set_cache(CACHE_RELAY, "True")
            if (self._relay is None or not self._relay):
                state_update = True
        if not state:
            self._set_cache(CACHE_RELAY, "False")
            if (self._relay is None or self._relay):
                state_update = True
        self._relay = state
        if state_update:
            await self.publish_feature_update_to_subscribers(
                NodeFeature.RELAY, self._relay_state
            )
            await self.save_cache()

    async def clock_synchronize(self) -> bool:
        """Synchronize clock. Returns true if successful."""
        clock_response: CircleClockResponse | None = await self._send(
            CircleClockGetRequest(self._mac_in_bytes)
        )
        if clock_response is None or clock_response.timestamp is None:
            return False
        _dt_of_circle = datetime.now(tz=UTC).replace(
            hour=clock_response.time.hour.value,
            minute=clock_response.time.minute.value,
            second=clock_response.time.second.value,
            microsecond=0,
            tzinfo=UTC,
        )
        clock_offset = (
            clock_response.timestamp.replace(microsecond=0) - _dt_of_circle
        )
        if (clock_offset.seconds > MAX_TIME_DRIFT) or (
            clock_offset.seconds < -(MAX_TIME_DRIFT)
        ):
            _LOGGER.info(
                "Reset clock of node %s because time has drifted %s sec",
                self._mac_in_str,
                str(clock_offset.seconds),
            )
            node_response: NodeResponse | None = await self._send(
                CircleClockSetRequest(
                    self._mac_in_bytes,
                    datetime.now(tz=UTC),
                    self._node_protocols.max
                )
            )
            if (
                node_response is None
                or node_response.ack_id != NodeResponseType.CLOCK_ACCEPTED
            ):
                _LOGGER.warning(
                    "Failed to (re)set the internal clock of node %s",
                    self._mac_in_str,
                )
                return False
        return True

    async def load(self) -> bool:
        """Load and activate Circle node features."""
        if self._loaded:
            return True
        if self._cache_enabled:
            _LOGGER.debug(
                "Load Circle node %s from cache", self._mac_in_str
            )
            if await self._load_from_cache():
                self._loaded = True
                self._setup_protocol(
                    CIRCLE_FIRMWARE_SUPPORT,
                    (
                        NodeFeature.RELAY,
                        NodeFeature.RELAY_INIT,
                        NodeFeature.ENERGY,
                        NodeFeature.POWER,
                    ),
                )
                if await self.initialize():
                    await self._loaded_callback(NodeEvent.LOADED, self.mac)
                    return True
            _LOGGER.debug(
                "Load Circle node %s from cache failed",
                self._mac_in_str,
            )
        else:
            _LOGGER.debug("Load Circle node %s", self._mac_in_str)

        # Check if node is online
        if not self._available and not await self.is_online():
            _LOGGER.debug(
                "Failed to load Circle node %s because it is not online",
                self._mac_in_str
            )
            return False

        # Get node info
        if self.skip_update(self._node_info, 30) and await self.node_info_update() is None:
            _LOGGER.debug(
                "Failed to load Circle node %s because it is not responding to information request",
                self._mac_in_str
            )
            return False
        self._loaded = True
        self._setup_protocol(
            CIRCLE_FIRMWARE_SUPPORT, (
                NodeFeature.RELAY,
                NodeFeature.RELAY_INIT,
                NodeFeature.ENERGY,
                NodeFeature.POWER,
            )
        )
        if not await self.initialize():
            return False
        await self._loaded_callback(NodeEvent.LOADED, self.mac)
        return True

    async def _load_from_cache(self) -> bool:
        """Load states from previous cached information. Returns True if successful."""
        if not await super()._load_from_cache():
            return False

        # Calibration settings
        if not await self._calibration_load_from_cache():
            _LOGGER.debug(
                "Node %s failed to load calibration from cache",
                self._mac_in_str
            )
            return False
        # Energy collection
        if await self._energy_log_records_load_from_cache():
            _LOGGER.debug(
                "Node %s failed to load energy_log_records from cache",
                self._mac_in_str,
            )
        # Relay
        if await self._relay_load_from_cache():
            _LOGGER.debug(
                "Node %s failed to load relay state from cache",
                self._mac_in_str,
            )
        # Relay init config if feature is enabled
        if (
            NodeFeature.RELAY_INIT in self._features
        ):
            if await self._relay_init_load_from_cache():
                _LOGGER.debug(
                    "Node %s failed to load relay_init state from cache",
                    self._mac_in_str,
                )
        return True

    @raise_not_loaded
    async def initialize(self) -> bool:
        """Initialize node."""
        if self._initialized:
            _LOGGER.debug("Already initialized node %s", self._mac_in_str)
            return True
        self._initialized = True

        if not self._calibration and not await self.calibration_update():
            _LOGGER.debug(
                "Failed to initialized node %s, no calibration",
                self._mac_in_str
            )
            self._initialized = False
            return False
        if self.skip_update(self._node_info, 30) and await self.node_info_update() is None:
            _LOGGER.debug(
                "Failed to retrieve node info for %s",
                self._mac_in_str
            )
        if not await self.clock_synchronize():
            _LOGGER.debug(
                "Failed to initialized node %s, failed clock sync",
                self._mac_in_str
            )
            self._initialized = False
            return False
        if (
            NodeFeature.RELAY_INIT in self._features and
            self._relay_init_state is None
        ):
            if (state := await self._relay_init_get()) is not None:
                self._relay_init_state = state
            else:
                _LOGGER.debug(
                    "Failed to initialized node %s, relay init",
                    self._mac_in_str
                )
                self._initialized = False
                return False
        return True

    async def node_info_update(
        self, node_info: NodeInfoResponse | None = None
    ) -> NodeInfo | None:
        """Update Node (hardware) information."""
        if node_info is None:
            if self.skip_update(self._node_info, 30):
                return self._node_info
            node_info: NodeInfoResponse = await self._send(
                NodeInfoRequest(self._mac_in_bytes)
            )
        if node_info is None:
            return None
        await super().node_info_update(node_info)
        await self._relay_update_state(
            node_info.relay_state, timestamp=node_info.timestamp
        )
        if (
            self._current_log_address is not None
            and (self._current_log_address > node_info.current_logaddress_pointer or self._current_log_address == 1)
        ):
            # Rollover of log address
            _LOGGER.debug(
                "Rollover log address from %s into %s for node %s",
                self._current_log_address,
                node_info.current_logaddress_pointer,
                self._mac_in_str
            )
        if self._current_log_address != node_info.current_logaddress_pointer:
            self._current_log_address = node_info.current_logaddress_pointer
            self._set_cache(
                CACHE_CURRENT_LOG_ADDRESS, node_info.current_logaddress_pointer
            )
            await self.save_cache()
        return self._node_info

    async def _node_info_load_from_cache(self) -> bool:
        """Load node info settings from cache."""
        result = await super()._node_info_load_from_cache()
        if (
            current_log_address := self._get_cache(CACHE_CURRENT_LOG_ADDRESS)
        ) is not None:
            self._current_log_address = int(current_log_address)
            return result
        return False

    async def unload(self) -> None:
        """Deactivate and unload node features."""
        self._loaded = False
        if self._retrieve_energy_logs_task is not None and not self._retrieve_energy_logs_task.done():
            self._retrieve_energy_logs_task.cancel()
            await self._retrieve_energy_logs_task
        if self._cache_enabled:
            await self._energy_log_records_save_to_cache()
        await super().unload()

    async def switch_init_relay(self, state: bool) -> bool:
        """Switch state of initial power-up relay state. Returns new state of relay."""
        await self._relay_init_set(state)
        return self._relay_init_state

    async def _relay_init_get(self) -> bool | None:
        """Get current configuration of the power-up state of the relay. Returns None if retrieval failed."""
        if NodeFeature.RELAY_INIT not in self._features:
            raise NodeError(
                "Retrieval of initial state of relay is not "
                + f"supported for device {self.name}"
            )
        response: CircleRelayInitStateResponse | None = await self._send(
            CircleRelayInitStateRequest(self._mac_in_bytes, False, False),
        )
        if response is None:
            return None
        await self._relay_init_update_state(response.relay.value == 1)
        return self._relay_init_state

    async def _relay_init_set(self, state: bool) -> bool | None:
        """Configure relay init state."""
        if NodeFeature.RELAY_INIT not in self._features:
            raise NodeError(
                "Configuring of initial state of relay is not"
                + f"supported for device {self.name}"
            )
        response: CircleRelayInitStateResponse | None = await self._send(
            CircleRelayInitStateRequest(self._mac_in_bytes, True, state),
        )
        if response is None:
            return None
        await self._relay_init_update_state(response.relay.value == 1)
        return self._relay_init_state

    async def _relay_init_load_from_cache(self) -> bool:
        """Load relay init state from cache. Returns True if retrieval was successful."""
        if (cached_relay_data := self._get_cache(CACHE_RELAY_INIT)) is not None:
            relay_init_state = False
            if cached_relay_data == "True":
                relay_init_state = True
            await self._relay_init_update_state(relay_init_state)
            return True
        return False

    async def _relay_init_update_state(self, state: bool) -> None:
        """Process relay init state update."""
        state_update = False
        if state:
            self._set_cache(CACHE_RELAY_INIT, "True")
            if self._relay_init_state is None or not self._relay_init_state:
                state_update = True
        if not state:
            self._set_cache(CACHE_RELAY_INIT, "False")
            if self._relay_init_state is None or self._relay_init_state:
                state_update = True
        if state_update:
            self._relay_init_state = state
            await self.publish_feature_update_to_subscribers(
                NodeFeature.RELAY_INIT, self._relay_init_state
            )
            await self.save_cache()

    @raise_calibration_missing
    def _calc_watts(
        self, pulses: int, seconds: int, nano_offset: int
    ) -> float | None:
        """Calculate watts based on energy usages."""
        if self._calibration is None:
            return None

        pulses_per_s = self._correct_power_pulses(pulses, nano_offset) / float(
            seconds
        )
        corrected_pulses = seconds * (
            (
                (
                    ((pulses_per_s + self._calibration.off_noise) ** 2)
                    * self._calibration.gain_b
                )
                + (
                    (pulses_per_s + self._calibration.off_noise)
                    * self._calibration.gain_a
                )
            )
            + self._calibration.off_tot
        )

        # Fix minor miscalculations
        if (
            calc_value := corrected_pulses / PULSES_PER_KW_SECOND / seconds * (
                1000
            )
        ) >= 0.0:
            return calc_value
        _LOGGER.debug(
            "Correct negative power %s to 0.0 for %s",
            str(corrected_pulses / PULSES_PER_KW_SECOND / seconds * 1000),
            self._mac_in_str
        )
        return 0.0

    def _correct_power_pulses(self, pulses: int, offset: int) -> float:
        """Correct pulses based on given measurement time offset (ns)."""

        # Sometimes the circle returns -1 for some of the pulse counters
        # likely this means the circle measures very little power and is
        # suffering from rounding errors. Zero these out. However, negative
        # pulse values are valid for power producing appliances, like
        # solar panels, so don't complain too loudly.
        if pulses == -1:
            _LOGGER.debug(
                "Power pulse counter for node %s of value of -1, corrected to 0",
                self._mac_in_str,
            )
            return 0.0
        if pulses != 0:
            if offset != 0:
                return (
                    pulses * (SECOND_IN_NANOSECONDS + offset)
                ) / SECOND_IN_NANOSECONDS
            return pulses
        return 0.0

    @raise_not_loaded
    async def get_state(
        self, features: tuple[NodeFeature]
    ) -> dict[NodeFeature, Any]:
        """Update latest state for given feature."""
        states: dict[NodeFeature, Any] = {}
        if not self._available:
            if not await self.is_online():
                _LOGGER.debug(
                    "Node %s did not respond, unable to update state",
                    self._mac_in_str
                )
                for feature in features:
                    states[feature] = None
                states[NodeFeature.AVAILABLE] = False
                return states

        for feature in features:
            if feature not in self._features:
                raise NodeError(
                    f"Update of feature '{feature}' is not supported for {self.name}"
                )
            if feature == NodeFeature.ENERGY:
                states[feature] = await self.energy_update()
                _LOGGER.debug(
                    "async_get_state %s - energy: %s",
                    self._mac_in_str,
                    states[feature],
                )
            elif feature == NodeFeature.RELAY:
                states[feature] = self._relay_state
                _LOGGER.debug(
                    "async_get_state %s - relay: %s",
                    self._mac_in_str,
                    states[feature],
                )
            elif feature == NodeFeature.RELAY_INIT:
                states[feature] = self._relay_init_state
            elif feature == NodeFeature.POWER:
                states[feature] = await self.power_update()
                _LOGGER.debug(
                    "async_get_state %s - power: %s",
                    self._mac_in_str,
                    states[feature],
                )
            else:
                state_result = await super().get_state((feature,))
                states[feature] = state_result[feature]

        states[NodeFeature.AVAILABLE] = self._available
        return states
