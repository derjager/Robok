"""Simuladores de bus I2C, GPIO (XSHUT) y sensores VL53L0X para probar el driver y el arreglo sin hardware.

El dispositivo modela lo que el driver necesita: registros por página (0xFF selecciona página), ID, SPAD,
inicio/fin de medición, interrupción y el cambio de dirección por 0x8A. Un sensor solo responde si su XSHUT
está alto; al bajarlo vuelve a 0x29 y pierde su configuración, como el real.
"""
from __future__ import annotations


class FakeVl53Device:
    def __init__(self, mm: int = 500, spad: int = 0x8C):
        self.mm, self.spad = mm, spad
        self.hold = 0                    # sondeos de "listo" que deben pasar antes de dar dato
        self.powered = False
        self.busy = False                # simula un sensor colgado: no responde aunque tenga energía
        self.log: list[tuple] = []
        self.reset()

    def reset(self) -> None:
        self.addr, self.page = 0x29, 0
        self.regs: dict[tuple[int, int], int] = {(0, 0xC0): 0xEE, (0, 0xC1): 0xAA, (0, 0xC2): 0x10, (1, 0x91): 0x3C}
        for i in range(6):
            self.regs[(0, 0xB0 + i)] = 0xFF
        self.ready = False
        self.continuous = False
        self.polls = 0

    def _rd(self, reg: int) -> int:
        if reg == 0xFF:
            return self.page
        if reg == 0x13:                  # estado de interrupción: ¿hay dato?
            if self.ready:
                return 0x01
            if self.continuous:
                self.polls += 1
                if self.polls > self.hold:
                    self.ready = True
                    return 0x01
            return 0x00
        if reg == 0x00 and self.page == 0:
            return 0x00                  # bit0 a 0: la medición ya arrancó
        if reg == 0x83:
            return self.regs.get((self.page, reg), 1) or 1
        if reg == 0x92:
            return self.spad
        if reg in (0x1E, 0x1F):
            return (self.mm >> 8) & 0xFF if reg == 0x1E else self.mm & 0xFF
        return self.regs.get((self.page, reg), 0)

    def _wr(self, reg: int, val: int) -> None:
        if reg == 0xFF:
            self.page = val
        elif reg == 0x8A:
            self.addr = val & 0x7F
        elif reg == 0x00 and self.page == 0 and val & 0x03:
            self.continuous = bool(val & 0x02)
            self.ready, self.polls = not (self.continuous and self.hold > 0), 0   # el 1.er dato tarda `hold` sondeos
        elif reg == 0x0B and self.page == 0 and val & 0x01:
            self.ready, self.polls = False, 0
        elif reg == 0x00 and self.page == 0:
            self.continuous = False
        self.regs[(self.page, reg)] = val

    def read(self, reg: int, n: int) -> bytes:
        self.log.append(("r", reg, n))
        return bytes(self._rd(reg + i) for i in range(n))

    def write(self, reg: int, data: bytes) -> None:
        self.log.append(("w", reg, bytes(data)))
        for i, b in enumerate(data):
            self._wr(reg + i, b)


class FakeI2cBus:
    def __init__(self, devices: dict[str, FakeVl53Device] | None = None):
        self.devices = devices or {}
        self.closed = False
        self.fail_all = False

    def _target(self, addr: int) -> FakeVl53Device:
        if self.fail_all:
            raise OSError(5, "Input/output error")
        hits = [d for d in self.devices.values() if d.powered and not d.busy and d.addr == addr]
        if not hits:
            raise OSError(121, "Remote I/O error")
        if len(hits) > 1:
            raise OSError(5, f"colisión en 0x{addr:02X}: {len(hits)} dispositivos responden a la vez")
        return hits[0]

    def read(self, addr: int, reg: int, n: int) -> bytes:
        return self._target(addr).read(reg, n)

    def write(self, addr: int, reg: int, data: bytes) -> None:
        self._target(addr).write(reg, data)

    def close(self) -> None:
        self.closed = True


class FakeXshutGpio:
    """Conecta cada pin XSHUT con su sensor: alto = encendido, bajo = apagado y reiniciado."""

    def __init__(self, pin_to_device: dict[int, FakeVl53Device], fail_claim: bool = False):
        self.map, self.levels, self.closed = pin_to_device, {}, False
        self.fail_claim = fail_claim
        self.calls: list[tuple] = []

    def claim_output(self, pin: int, level: int = 0) -> None:
        if self.fail_claim:
            raise OSError("GPIO ocupado")
        self.calls.append(("claim", pin, level))
        self.write(pin, level)

    def write(self, pin: int, level: int) -> None:
        self.calls.append(("write", pin, level))
        self.levels[pin] = level
        dev = self.map.get(pin)
        if dev is None:
            return
        if level and not dev.powered:
            dev.powered = True
            dev.reset()
        elif not level:
            dev.powered = False
            dev.reset()

    def pwm(self, pin: int, hz: float, duty_pct: float) -> None:
        raise AssertionError("los XSHUT no usan PWM")

    def close(self) -> None:
        self.closed = True
