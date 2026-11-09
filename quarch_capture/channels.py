"""Discover power channels from the instrument's own fixture definitions."""

import re
import xml.etree.ElementTree as ET


AC_CHANNELS = ("L1", "L2", "L3")
CHANNEL_REF = re.compile(r"chan\(\s*([^,\s]+)\s*,\s*([^\s)]+)\s*\)", re.IGNORECASE)


def channel_name(value):
    """Keep fixture spelling, with legacy case-insensitive AC channel names."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+][A-Za-z0-9_.+-]{0,63}", value):
        raise ValueError("Use a channel from --inspect, e.g. L2, 3.3V, VIO_1V8 or Tot")
    if value.upper() in AC_CHANNELS:
        return value.upper()
    return "Tot" if value.casefold() == "tot" else value


def instrument_model(identity):
    match = re.search(r"\bQTL(2312|2843)\b", identity)
    if not match:
        raise RuntimeError("Selected device is not a supported QTL2312 or QTL2843")
    return "QTL" + match.group(1)


def available_power_channels(definitions):
    channels = []
    for definition in definitions:
        match = CHANNEL_REF.match(definition.function.strip())
        if match is None:
            continue
        name, group = match.groups()
        if group == "VA" and name.endswith("_PInst") and name[:-6] in AC_CHANNELS:
            name = name[:-6]
        elif group != "power":
            continue
        name = channel_name(name)
        if name not in channels:
            channels.append(name)
    if not channels:
        raise ValueError("Fixture has no supported power channels; check the fixture connection and firmware")
    return tuple(channels)


def selected_synthetic_functions(definitions, channel, include_details):
    """Select power plus its prerequisites; never multiply resampled V and I."""
    channel = channel_name(channel)
    functions = {}
    for definition in definitions:
        refs = CHANNEL_REF.findall(definition.function)
        if refs:
            if refs[0] in functions:
                raise ValueError(f"Duplicate synthetic channel: {refs[0]}")
            functions[refs[0]] = (definition.function, refs[1:])
    target = (channel + "_PInst", "VA") if channel in AC_CHANNELS else (channel, "power")
    if target not in functions:
        raise ValueError(f"Instrument fixture has no instantaneous-power definition for {channel}")
    selected, visiting = {}, set()

    def add(key):
        if key in selected:
            return
        if key in visiting:
            raise ValueError("Cyclic fixture power-channel dependencies")
        visiting.add(key)
        function, dependencies = functions[key]
        for dependency in dependencies:
            if dependency in functions:
                add(dependency)
        visiting.remove(key)
        selected[key] = function

    if include_details and channel in AC_CHANNELS:
        for key in functions:
            if key[0].startswith((channel + "_", channel + "V_")):
                add(key)
    add(target)
    return list(selected.values())


def fixture_analog_groups(xml):
    """Read group IDs and base rates instead of assuming every PAM is 250 kHz."""
    root = ET.fromstring(xml)

    def params(element):
        return {p.findtext("Name"): p.findtext("Value") for p in element.findall("Param")}

    status = root.find("General")
    if status is not None and params(status).get("FixtureStatus", "OK") != "OK":
        raise ValueError("Fixture reports an unhealthy or disconnected state")
    analog_ids = set()
    for element in root.findall("./Channels/Channel"):
        data = params(element)
        if data.get("Type") in ("V", "A"):
            analog_ids.add(int(data["Group"]))
    groups = {}
    for element in root.findall("./Groups/Group"):
        data = params(element)
        number = int(data["Number"])
        if number in analog_ids:
            rate = int(data["SampleBase"]) * 10 ** int(data["SampleExponent"])
            if rate <= 0:
                raise ValueError("Invalid analog group sample rate")
            groups[number] = rate
    if analog_ids != groups.keys() or not groups:
        raise ValueError("Fixture does not provide complete analog sampling groups")
    return groups


def power_column(headers, channel):
    """Return a unique power column and its scale to watts, including DC uW."""
    selected = []
    for index, header in enumerate(headers):
        parts = header.rsplit(" ", 2)
        if len(parts) != 3:
            continue
        name, group, unit = parts
        if channel in AC_CHANNELS:
            matches = (name, group) == (channel + "_PInst", "VA")
            scales = {"VA": 1, "mVA": 1e-3, "uVA": 1e-6}
        else:
            matches = (name, group) == (channel, "power")
            scales = {"W": 1, "mW": 1e-3, "uW": 1e-6, "µW": 1e-6, "μW": 1e-6}
        if matches:
            if unit not in scales:
                raise ValueError(f"Unsupported power unit: {unit}")
            selected.append((index, scales[unit]))
    if len(selected) != 1:
        raise ValueError(f"QIS did not provide a unique selected power channel: {channel}")
    return selected[0]


def selected_columns(headers, channel):
    if channel in AC_CHANNELS:
        return [i for i, c in enumerate(headers) if c.startswith((channel + " ", channel + "_", channel + "V_"))]
    return [i for i, c in enumerate(headers) if c.rsplit(" ", 2)[0] == channel]
