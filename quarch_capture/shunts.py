"""External-shunt resistance and QIS current conversions; no supply switching."""

import math
import re
from types import SimpleNamespace
import xml.etree.ElementTree as ET

from .channels import CHANNEL_REF, canonical_channel_ref, channel_name


def resistance_ohm(reply):
    match = re.fullmatch(r"\s*([+\-0-9.eE]+)\s*(MicroOhm|MilliOhm|Ohm|KiloOhm|MegaOhm)\s*",
                         reply, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid shunt resistance response: {reply!r}")
    scales = {"microohm": 1e-6, "milliohm": 1e-3, "ohm": 1,
              "kiloohm": 1e3, "megaohm": 1e6}
    value = float(match[1]) * scales[match[2].lower()]
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid shunt resistance: {reply!r}")
    return value


def external_shunt_channels(xml):
    root = ET.fromstring(xml)
    if not root.findall("./Features/Feature/Param[Name='ExternalShunt']/Value[.='1']"):
        return ()
    return tuple(channel_name(c.findtext("Param[Name='Name']/Value"))
                 for c in root.findall("./Channels/Channel")
                 if c.findtext("Param[Name='Type']/Value") == "dV")


def synthetic_definitions(power, active_channels=None):
    """Add missing I=dV/R channels before the fixture's dependent power channels."""
    definitions = [d for d in power.parse_synthetic_channels_from_instrument() if d.function.strip()]
    channels = external_shunt_channels(power.fixture_definition)
    if active_channels is not None:
        active = tuple(active_channels)
        if not active or len(set(active)) != len(active) or not set(active).issubset(channels):
            raise ValueError("Active channels must be unique external-shunt inputs from this fixture")
        restricted = []
        for definition in definitions:
            match = CHANNEL_REF.match(definition.function.strip())
            name, group = match.groups() if match else (None, None)
            if name in channels and name not in active:
                continue
            if (name, group) == ("Tot", "power"):
                definition = SimpleNamespace(number=0, function=(
                    "chan(Tot,power) sum(" + ",".join(f"chan({ch},power)" for ch in active) + ")"))
            restricted.append(definition)
        definitions, channels = restricted, active
    destinations = {canonical_channel_ref(CHANNEL_REF.match(d.function.strip()).groups())
                    for d in definitions if CHANNEL_REF.match(d.function.strip())}
    shunts, currents = {}, []
    for name in channels:
        value = resistance_ohm(power.send_command(f"FIX:{name}:SHUNT?"))
        shunts[name] = value
        if value > 0 and (name, "current") not in destinations:
            # QIS handles input/output SI prefixes; scalar 1/R uses ohms.
            currents.append(SimpleNamespace(number=0, function=(
                f"chan({name},current) Multiplier(A, micro, {1 / value:.17g}, chan({name},dV))")))
    return currents + definitions, shunts


def configure_shunts(device, values):
    """Set caller-supplied resistances and read back; never infer a resistor value."""
    if device.send_command("REC?").strip().upper() != "OFF":
        raise RuntimeError("Stop recording before configuring external shunts")
    if not values or not set(values).issubset(external_shunt_channels(device.send_command("FIX:CHAN:XML?"))):
        raise ValueError("Shunt settings must select external-shunt inputs from this fixture")
    actual = {}
    for name, value in values.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name}: shunt resistance must be finite and positive")
    for name, value in values.items():
        old = resistance_ohm(device.send_command(f"FIX:{name}:SHUNT?"))
        if not math.isclose(old, value, rel_tol=1e-9, abs_tol=1e-15):
            reply = device.send_command(f"FIX:{name}:SHUNT {value * 1000:.12g} MilliOhm").strip()
            if reply.upper() != "OK":
                raise RuntimeError(f"{name}: shunt configuration failed: {reply}")
        actual[name] = resistance_ohm(device.send_command(f"FIX:{name}:SHUNT?"))
        if not math.isclose(actual[name], value, rel_tol=1e-9, abs_tol=1e-15):
            raise RuntimeError(f"{name}: shunt readback differs from requested resistance")
    return actual
