"""Driver del sensor de distancia VL53L0X (ToF, ~3 a 120 cm) sobre un bus I2C abstracto.

Adaptado de Adafruit_CircuitPython_VL53L0X (MIT, © 2017 Tony DiCola para Adafruit Industries), que a su vez
es un port de https://github.com/pololu/vl53l0x-arduino. La secuencia de inicialización, la tabla de ajuste
de ST y el cálculo de tiempos son los originales; aquí solo cambia el acceso al bus (sin CircuitPython ni
Blinka) y el modo de uso: distancia continua y sin bloquear (`read_mm()` devuelve None si aún no hay dato).
"""
from __future__ import annotations

import math
import time
from typing import Protocol

DEFAULT_ADDRESS = 0x29
NO_TARGET_MM = 8190          # a partir de aquí el sensor dice "nada a la vista" (8190/8191) o "tiempo agotado" (65535)

_SYSRANGE_START = 0x00
_SYSTEM_SEQUENCE_CONFIG = 0x01
_SYSTEM_INTERRUPT_CONFIG_GPIO = 0x0A
_GPIO_HV_MUX_ACTIVE_HIGH = 0x84
_SYSTEM_INTERRUPT_CLEAR = 0x0B
_RESULT_INTERRUPT_STATUS = 0x13
_RESULT_RANGE_STATUS = 0x14
_I2C_SLAVE_DEVICE_ADDRESS = 0x8A
_MSRC_CONFIG_CONTROL = 0x60
_FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT = 0x44
_PRE_RANGE_CONFIG_VCSEL_PERIOD = 0x50
_PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI = 0x51
_FINAL_RANGE_CONFIG_VCSEL_PERIOD = 0x70
_FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI = 0x71
_MSRC_CONFIG_TIMEOUT_MACROP = 0x46
_GLOBAL_CONFIG_SPAD_ENABLES_REF_0 = 0xB0
_GLOBAL_CONFIG_REF_EN_START_SELECT = 0xB6
_DYNAMIC_SPAD_NUM_REQUESTED_REF_SPAD = 0x4E
_DYNAMIC_SPAD_REF_EN_START_OFFSET = 0x4F
_VCSEL_PERIOD_PRE_RANGE = 0
_VCSEL_PERIOD_FINAL_RANGE = 1

# Tabla de ajuste de ST (registro, valor), copiada literalmente de la librería original.
_TUNING = (
    (0xFF, 0x01), (0x00, 0x00), (0xFF, 0x00), (0x09, 0x00), (0x10, 0x00), (0x11, 0x00),
    (0x24, 0x01), (0x25, 0xFF), (0x75, 0x00), (0xFF, 0x01), (0x4E, 0x2C), (0x48, 0x00),
    (0x30, 0x20), (0xFF, 0x00), (0x30, 0x09), (0x54, 0x00), (0x31, 0x04), (0x32, 0x03),
    (0x40, 0x83), (0x46, 0x25), (0x60, 0x00), (0x27, 0x00), (0x50, 0x06), (0x51, 0x00),
    (0x52, 0x96), (0x56, 0x08), (0x57, 0x30), (0x61, 0x00), (0x62, 0x00), (0x64, 0x00),
    (0x65, 0x00), (0x66, 0xA0), (0xFF, 0x01), (0x22, 0x32), (0x47, 0x14), (0x49, 0xFF),
    (0x4A, 0x00), (0xFF, 0x00), (0x7A, 0x0A), (0x7B, 0x00), (0x78, 0x21), (0xFF, 0x01),
    (0x23, 0x34), (0x42, 0x00), (0x44, 0xFF), (0x45, 0x26), (0x46, 0x05), (0x40, 0x40),
    (0x0E, 0x06), (0x20, 0x1A), (0x43, 0x40), (0xFF, 0x00), (0x34, 0x03), (0x35, 0x44),
    (0xFF, 0x01), (0x31, 0x04), (0x4B, 0x09), (0x4C, 0x05), (0x4D, 0x04), (0xFF, 0x00),
    (0x44, 0x00), (0x45, 0x20), (0x47, 0x08), (0x48, 0x28), (0x67, 0x00), (0x70, 0x04),
    (0x71, 0x01), (0x72, 0xFE), (0x76, 0x00), (0x77, 0x00), (0xFF, 0x01), (0x0D, 0x01),
    (0xFF, 0x00), (0x80, 0x01), (0x01, 0xF8), (0xFF, 0x01), (0x8E, 0x01), (0x00, 0x01),
    (0xFF, 0x00), (0x80, 0x00),
)


class Vl53Error(RuntimeError):
    """Fallo al hablar con el sensor: no responde, no es un VL53L0X o se agotó un tiempo de espera."""


class I2cBus(Protocol):
    def read(self, addr: int, reg: int, n: int) -> bytes: ...
    def write(self, addr: int, reg: int, data: bytes) -> None: ...
    def close(self) -> None: ...


class SmbusI2c:
    """Bus I2C real (/dev/i2c-N) con smbus2. Escribe el registro y lee en dos transacciones (con STOP en medio)."""

    def __init__(self, bus: int = 1):
        from smbus2 import SMBus  # noqa: PLC0415  (import tardío: solo hace falta en el robot)
        self._msg = __import__("smbus2").i2c_msg
        self._bus = SMBus(bus)

    def read(self, addr: int, reg: int, n: int) -> bytes:
        self._bus.i2c_rdwr(self._msg.write(addr, [reg]))
        rd = self._msg.read(addr, n)
        self._bus.i2c_rdwr(rd)
        return bytes(rd)

    def write(self, addr: int, reg: int, data: bytes) -> None:
        self._bus.i2c_rdwr(self._msg.write(addr, [reg, *data]))

    def close(self) -> None:
        self._bus.close()


def _decode_timeout(val: int) -> float:
    # formato: "(LSByte * 2^MSByte) + 1"
    return float(val & 0xFF) * math.pow(2.0, ((val & 0xFF00) >> 8)) + 1


def _encode_timeout(timeout_mclks: float) -> int:
    timeout_mclks = int(timeout_mclks) & 0xFFFF
    ls_byte = 0
    ms_byte = 0
    if timeout_mclks > 0:
        ls_byte = timeout_mclks - 1
        while ls_byte > 255:
            ls_byte >>= 1
            ms_byte += 1
        return ((ms_byte << 8) | (ls_byte & 0xFF)) & 0xFFFF
    return 0


def _timeout_mclks_to_microseconds(timeout_period_mclks: int, vcsel_period_pclks: int) -> int:
    macro_period_ns = ((2304 * (vcsel_period_pclks) * 1655) + 500) // 1000
    return ((timeout_period_mclks * macro_period_ns) + (macro_period_ns // 2)) // 1000


def _timeout_microseconds_to_mclks(timeout_period_us: int, vcsel_period_pclks: int) -> int:
    macro_period_ns = ((2304 * (vcsel_period_pclks) * 1655) + 500) // 1000
    return ((timeout_period_us * 1000) + (macro_period_ns // 2)) // macro_period_ns


class VL53L0X:
    def __init__(self, bus: I2cBus, address: int = DEFAULT_ADDRESS, io_timeout_s: float = 0.5):
        self._bus = bus
        self.address = address
        self.io_timeout_s = io_timeout_s
        self._data_ready = False
        self._continuous = False
        self._budget_us = 0
        self._init_sensor()

    # --- acceso a registros -------------------------------------------------------------------
    def _r8(self, reg: int) -> int:
        return self._bus.read(self.address, reg, 1)[0]

    def _r16(self, reg: int) -> int:
        hi, lo = self._bus.read(self.address, reg, 2)
        return (hi << 8) | lo

    def _w8(self, reg: int, val: int) -> None:
        self._bus.write(self.address, reg, bytes([val & 0xFF]))

    def _w16(self, reg: int, val: int) -> None:
        self._bus.write(self.address, reg, bytes([(val >> 8) & 0xFF, val & 0xFF]))

    def _wait(self, cond, what: str) -> None:
        start = time.monotonic()
        while not cond():
            if self.io_timeout_s > 0 and time.monotonic() - start >= self.io_timeout_s:
                raise Vl53Error(f"tiempo agotado esperando al VL53L0X ({what})")

    # --- inicialización (secuencia de Pololu/Adafruit) ------------------------------------------
    def _init_sensor(self) -> None:
        try:
            ident = self._bus.read(self.address, 0xC0, 3)
        except OSError as e:
            raise Vl53Error(f"no responde en 0x{self.address:02X}: {e}") from e
        if tuple(ident) != (0xEE, 0xAA, 0x10):
            raise Vl53Error(f"0x{self.address:02X} no es un VL53L0X (ID {ident.hex()}); revisa el cableado")
        try:
            self._configure()
        except OSError as e:
            raise Vl53Error(f"error de I2C al inicializar el VL53L0X: {e}") from e

    def _configure(self) -> None:
        for reg, val in ((0x88, 0x00), (0x80, 0x01), (0xFF, 0x01), (0x00, 0x00)):   # modo I2C estándar
            self._w8(reg, val)
        self._stop_variable = self._r8(0x91)
        for reg, val in ((0x00, 0x01), (0xFF, 0x00), (0x80, 0x00)):
            self._w8(reg, val)
        self._w8(_MSRC_CONFIG_CONTROL, self._r8(_MSRC_CONFIG_CONTROL) | 0x12)   # sin límites SIGNAL_RATE_MSRC/PRE_RANGE
        self._set_signal_rate_limit(0.25)
        self._w8(_SYSTEM_SEQUENCE_CONFIG, 0xFF)
        spad_count, spad_is_aperture = self._get_spad_info()
        ref_spad_map = bytearray(self._bus.read(self.address, _GLOBAL_CONFIG_SPAD_ENABLES_REF_0, 6))
        for reg, val in ((0xFF, 0x01), (_DYNAMIC_SPAD_REF_EN_START_OFFSET, 0x00),
                         (_DYNAMIC_SPAD_NUM_REQUESTED_REF_SPAD, 0x2C), (0xFF, 0x00),
                         (_GLOBAL_CONFIG_REF_EN_START_SELECT, 0xB4)):
            self._w8(reg, val)
        first_spad_to_enable = 12 if spad_is_aperture else 0
        spads_enabled = 0
        for i in range(48):
            if i < first_spad_to_enable or spads_enabled == spad_count:
                ref_spad_map[i // 8] &= ~(1 << (i % 8)) & 0xFF
            elif (ref_spad_map[i // 8] >> (i % 8)) & 0x1 > 0:
                spads_enabled += 1
        self._bus.write(self.address, _GLOBAL_CONFIG_SPAD_ENABLES_REF_0, bytes(ref_spad_map))
        for reg, val in _TUNING:
            self._w8(reg, val)
        self._w8(_SYSTEM_INTERRUPT_CONFIG_GPIO, 0x04)
        self._w8(_GPIO_HV_MUX_ACTIVE_HIGH, self._r8(_GPIO_HV_MUX_ACTIVE_HIGH) & ~0x10 & 0xFF)   # activo en bajo
        self._w8(_SYSTEM_INTERRUPT_CLEAR, 0x01)
        self._budget_us = self._get_timing_budget()
        self._w8(_SYSTEM_SEQUENCE_CONFIG, 0xE8)
        self._set_timing_budget(self._budget_us)
        self._w8(_SYSTEM_SEQUENCE_CONFIG, 0x01)
        self._single_ref_calibration(0x40)
        self._w8(_SYSTEM_SEQUENCE_CONFIG, 0x02)
        self._single_ref_calibration(0x00)
        self._w8(_SYSTEM_SEQUENCE_CONFIG, 0xE8)     # restaura la configuración de secuencia

    def _set_signal_rate_limit(self, mcps: float) -> None:
        self._w16(_FINAL_RANGE_CONFIG_MIN_COUNT_RATE_RTN_LIMIT, int(mcps * (1 << 7)))

    def _get_spad_info(self) -> tuple[int, bool]:
        for reg, val in ((0x80, 0x01), (0xFF, 0x01), (0x00, 0x00), (0xFF, 0x06)):
            self._w8(reg, val)
        self._w8(0x83, self._r8(0x83) | 0x04)
        for reg, val in ((0xFF, 0x07), (0x81, 0x01), (0x80, 0x01), (0x94, 0x6B), (0x83, 0x00)):
            self._w8(reg, val)
        self._wait(lambda: self._r8(0x83) != 0x00, "SPAD")
        self._w8(0x83, 0x01)
        tmp = self._r8(0x92)
        count, is_aperture = tmp & 0x7F, ((tmp >> 7) & 0x01) == 1
        for reg, val in ((0x81, 0x00), (0xFF, 0x06)):
            self._w8(reg, val)
        self._w8(0x83, self._r8(0x83) & ~0x04 & 0xFF)
        for reg, val in ((0xFF, 0x01), (0x00, 0x01), (0xFF, 0x00), (0x80, 0x00)):
            self._w8(reg, val)
        return count, is_aperture

    def _single_ref_calibration(self, vhv_init_byte: int) -> None:
        self._w8(_SYSRANGE_START, 0x01 | vhv_init_byte & 0xFF)
        self._wait(lambda: (self._r8(_RESULT_INTERRUPT_STATUS) & 0x07) != 0, "calibración")
        self._w8(_SYSTEM_INTERRUPT_CLEAR, 0x01)
        self._w8(_SYSRANGE_START, 0x00)

    # --- tiempos (cálculo de ST, sin cambios) --------------------------------------------------
    def _get_vcsel_pulse_period(self, kind: int) -> int:
        if kind == _VCSEL_PERIOD_PRE_RANGE:
            return (((self._r8(_PRE_RANGE_CONFIG_VCSEL_PERIOD)) + 1) & 0xFF) << 1
        if kind == _VCSEL_PERIOD_FINAL_RANGE:
            return (((self._r8(_FINAL_RANGE_CONFIG_VCSEL_PERIOD)) + 1) & 0xFF) << 1
        return 255

    def _get_sequence_step_enables(self) -> tuple[bool, bool, bool, bool, bool]:
        cfg = self._r8(_SYSTEM_SEQUENCE_CONFIG)
        return ((cfg >> 4) & 0x1 > 0, (cfg >> 3) & 0x1 > 0, (cfg >> 2) & 0x1 > 0,
                (cfg >> 6) & 0x1 > 0, (cfg >> 7) & 0x1 > 0)

    def _get_sequence_step_timeouts(self, pre_range: bool):
        pre_vcsel = self._get_vcsel_pulse_period(_VCSEL_PERIOD_PRE_RANGE)
        msrc_mclks = (self._r8(_MSRC_CONFIG_TIMEOUT_MACROP) + 1) & 0xFF
        msrc_us = _timeout_mclks_to_microseconds(msrc_mclks, pre_vcsel)
        pre_mclks = _decode_timeout(self._r16(_PRE_RANGE_CONFIG_TIMEOUT_MACROP_HI))
        pre_us = _timeout_mclks_to_microseconds(pre_mclks, pre_vcsel)
        final_vcsel = self._get_vcsel_pulse_period(_VCSEL_PERIOD_FINAL_RANGE)
        final_mclks = _decode_timeout(self._r16(_FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI))
        if pre_range:
            final_mclks -= pre_mclks
        final_us = _timeout_mclks_to_microseconds(final_mclks, final_vcsel)
        return msrc_us, pre_us, final_us, final_vcsel, pre_mclks

    def _get_timing_budget(self) -> int:
        budget_us = 1910 + 960
        tcc, dss, msrc, pre_range, final_range = self._get_sequence_step_enables()
        msrc_us, pre_us, final_us, _, _ = self._get_sequence_step_timeouts(pre_range)
        if tcc:
            budget_us += msrc_us + 590
        if dss:
            budget_us += 2 * (msrc_us + 690)
        elif msrc:
            budget_us += msrc_us + 660
        if pre_range:
            budget_us += pre_us + 660
        if final_range:
            budget_us += final_us + 550
        return budget_us

    def _set_timing_budget(self, budget_us: int) -> None:
        used_us = 1320 + 960
        tcc, dss, msrc, pre_range, final_range = self._get_sequence_step_enables()
        msrc_us, pre_us, _, final_vcsel, pre_mclks = self._get_sequence_step_timeouts(pre_range)
        if tcc:
            used_us += msrc_us + 590
        if dss:
            used_us += 2 * (msrc_us + 690)
        elif msrc:
            used_us += msrc_us + 660
        if pre_range:
            used_us += pre_us + 660
        if final_range:
            used_us += 550
            if used_us > budget_us:
                raise Vl53Error("presupuesto de tiempo del VL53L0X demasiado pequeño")
            final_mclks = _timeout_microseconds_to_mclks(budget_us - used_us, final_vcsel)
            if pre_range:
                final_mclks += pre_mclks
            self._w16(_FINAL_RANGE_CONFIG_TIMEOUT_MACROP_HI, _encode_timeout(final_mclks))
            self._budget_us = budget_us

    @property
    def timing_budget_us(self) -> int:
        return self._budget_us

    # --- medición ----------------------------------------------------------------------------
    def _start_sequence(self, mode: int) -> None:
        for reg, val in ((0x80, 0x01), (0xFF, 0x01), (0x00, 0x00), (0x91, self._stop_variable),
                         (0x00, 0x01), (0xFF, 0x00), (0x80, 0x00), (_SYSRANGE_START, mode)):
            self._w8(reg, val)

    def start_continuous(self) -> None:
        """Mide sin parar (uno cada ~budget); lee con `read_mm()`."""
        try:
            self._start_sequence(0x02)
            self._wait(lambda: (self._r8(_SYSRANGE_START) & 0x01) == 0, "inicio")
        except OSError as e:
            raise Vl53Error(f"error de I2C al iniciar la medición: {e}") from e
        self._continuous, self._data_ready = True, False

    def stop_continuous(self) -> None:
        for reg, val in ((_SYSRANGE_START, 0x01), (0xFF, 0x01), (0x00, 0x00), (0x91, 0x00), (0x00, 0x01), (0xFF, 0x00)):
            self._w8(reg, val)
        self._continuous, self._data_ready = False, False

    def read_mm(self) -> int | None:
        """Última distancia en mm si hay una lista, None si el sensor aún no termina otra medición.

        8190 o más (o 65535) significa que no hay nada dentro del alcance. Lanza Vl53Error si falla el bus.
        """
        try:
            if not self._data_ready and self._r8(_RESULT_INTERRUPT_STATUS) & 0x07 == 0:
                return None
            mm = self._r16(_RESULT_RANGE_STATUS + 10)
            self._w8(_SYSTEM_INTERRUPT_CLEAR, 0x01)
        except OSError as e:
            raise Vl53Error(f"error de I2C al leer el VL53L0X: {e}") from e
        self._data_ready = False
        return mm

    def set_address(self, new_address: int) -> None:
        """Cambia la dirección (se pierde al quitarle la alimentación / bajar XSHUT)."""
        self._w8(_I2C_SLAVE_DEVICE_ADDRESS, new_address & 0x7F)
        self.address = new_address

    def ident_ok(self) -> bool:
        try:
            return tuple(self._bus.read(self.address, 0xC0, 3)) == (0xEE, 0xAA, 0x10)
        except OSError:
            return False
