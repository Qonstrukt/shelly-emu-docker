# shelly-emu-docker: Shelly Gen2 emulator backed by Home Assistant

Run a **Shelly Gen2 device** on your LAN as a Docker container, with all data and control
coming from **Home Assistant**. Built so a **Sigenergy SigenStor / mySigen** installation
can manage any HA switch as a "Shelly smart load", or read any HA power source as a
"Shelly Pro 3EM" meter, without buying Shelly hardware. No Bluetooth needed.

This is the Python / container port of
[esphome-shelly-emu](https://github.com/Qonstrukt/esphome-shelly-emu), whose `pro4pm`
emulation has been verified end to end with a real SigenStor and the mySigen app
(discovery, pairing, per-channel power and energy, switching). The payloads here are a
one to one port of that component.

Emulated models:

| `model`     | Shelly device      | channels      | Sigen use                   |
|-------------|--------------------|---------------|-----------------------------|
| `pro1pm`    | Shelly Pro 1PM     | 1 switch      | smart load                  |
| `pro2pm`    | Shelly Pro 2PM     | 2 switch      | smart load                  |
| `pro4pm`    | Shelly Pro 4PM     | 4 switch      | smart load (verified on the ESPHome port) |
| `plus1pm`   | Shelly Plus 1PM    | 1 switch      | smart load                  |
| `plus2pm`   | Shelly Plus 2PM    | 2 switch      | smart load                  |
| `plusplugs` | Shelly Plus Plug S | 1 switch      | smart load                  |
| `pro3em`    | Shelly Pro 3EM     | 3-phase meter | energy meter (Modbus TCP)   |

What the service provides, per emulated device:

* **HTTP JSON-RPC** on port 80 (`GET /shelly`, `GET|POST /rpc`, `GET|POST /rpc/<Method>`)
  with the Shelly Gen2 payloads mySigen and the SigenStor expect (`Shelly.*`, `Switch.*`,
  `Input.*`, `Sys.*`, `Wifi.*`, `EM.*`, `EMData.*`, ...), `Server: ShellyHTTP/1.0.0`,
  `auth_en: false`, firmware `2.0.0` (older versions are rejected by mySigen).
* **mDNS** advertisement as `ShellyPro4PM-AABBCCDDEEFF.local` with `_shelly._tcp`
  (TXT `gen=2`, `app`, `ver`) and `_http._tcp`, instance name = Shelly id, IP TTL 255.
* **Modbus TCP** on port 502 (`pro3em`) with the Pro 3EM register map (FC04, CDAB float
  word order), which is how the SigenStor polls meters after enrollment.
* **Home Assistant bridge**: channel outputs are HA switches (or lights, input_booleans),
  power / voltage / energy values are HA sensors, read over the HA WebSocket API.
  `Switch.Set` from Sigen becomes `switch.turn_on` / `turn_off` in HA. The output is
  reported optimistically for a few seconds until HA confirms the new state.

Protocol findings and sources are in the ESPHome project's
[docs/DESIGN.md](https://github.com/Qonstrukt/esphome-shelly-emu/blob/main/docs/DESIGN.md).

## Quick start (one device, host networking)

1. In Home Assistant create a **long-lived access token** (your profile, Security tab).
2. Copy `config.example.yaml` to `config.yaml` and list the HA entities per channel:

   ```yaml
   devices:
     - model: pro4pm
       name: "Smart plugs"
       mac: "AABBCCDDEEFF"        # any 12 hex digits, unique on your LAN; fixes the Shelly id
       channels:
         - switch: switch.boiler
           name: Boiler
           power: sensor.boiler_power
           energy: sensor.boiler_energy
         - switch: switch.dryer
           name: Dryer
           power: sensor.dryer_power
   ```

3. Put the token in `.env` (`HA_TOKEN=eyJ...`), set `HA_URL` in `docker-compose.yml`, then:

   ```bash
   docker compose up -d --build
   ```

4. Check the log (`docker compose logs -f`) for `authenticated with Home Assistant`,
   `mDNS: ShellyPro4PM-....local (...) advertising _shelly._tcp` and `ready`. Then:

   ```bash
   python3 tools/validate.py http://<docker-host-ip>
   ```

5. mySigen: **Device tab, Add System Device**. The screen says "Auto-detecting nearby
   devices" and shows a Bluetooth icon, but it also scans the LAN: the emulator appears as
   e.g. "Shelly Pro 4PM, SN:AABBCCDDEEFF" with a Wi-Fi badge. Tick it and press **Add**.
   The first channel is added when pairing; the other channels of a multi-channel model
   are added afterwards from the Smart Load section.

The image is built from the `Dockerfile` (`python:3.12-slim`, runs as a non-root user that
may bind ports 80 and 502). CI publishes a prebuilt multi-arch image as
`ghcr.io/qonstrukt/shelly-emu-docker:latest`; swap `build: .` for `image:` in the compose
file to use it.

## Networking: one IP per emulated device

Sigen identifies a Shelly **by its IP address** and always talks to **port 80** (HTTP) and
**port 502** (Modbus TCP). It finds devices with mDNS multicast on the LAN. Consequences:

* **Docker bridge networking does not work.** Port mapping cannot make mDNS multicast cross
  the bridge, and the container would advertise an internal address. Use one of the modes
  below.
* **`network_mode: host`** (see `docker-compose.yml`): the container uses the Docker host's
  address. Simple and recommended for **one** emulated device per host. Ports 80 and 502 on
  the host must be free (no reverse proxy or web UI on port 80). Docker Desktop on macOS and
  Windows needs the "Enable host networking" option.
* **macvlan** (see `docker-compose.macvlan.yml`): every container gets its own LAN IP and
  MAC, so you can run **several** devices on one host, one container each. The Docker host
  itself cannot reach macvlan containers (kernel limitation); if Home Assistant runs on the
  same host, reach it through another address (a second interface, a reverse proxy, or a
  macvlan shim interface on the host). Reserve the addresses in your DHCP server.
* Several devices in **one** container only work when the host has several IP addresses and
  each device gets its own `bind:` address. The configuration refuses two devices on the
  same address and port.
* Prefer a wired or at least fixed-IP host: if the address changes, Sigen loses the device.

Use multi-channel models (`pro4pm` = 4 loads) to keep the number of IPs small.

## Configuration

`config.yaml` (mounted at `/config/config.yaml`, or pass `-c` / env `CONFIG`):

```yaml
homeassistant:
  url: http://homeassistant.local:8123   # or env HA_URL
  token: ...                              # or env HA_TOKEN / HA_TOKEN_FILE (recommended)
# log_level: INFO                         # or env LOG_LEVEL
# mdns_interfaces: [192.168.1.20]         # restrict the mDNS responder to these host addresses

devices:
  - model: pro4pm             # required, see table above
    name: "Smart plugs"       # Shelly device name (sys.device.name), shown in mySigen
    mac: "AABBCCDDEEFF"       # required, 12 hex digits; id becomes shellypro4pm-aabbccddeeff
    # bind: 0.0.0.0           # listen address (per device when one container hosts several)
    # ip: 192.168.1.20        # address advertised via mDNS and reported as sta_ip; auto-detected
    # http_port: 80           # Sigen assumes 80; only change for local testing
    # mdns: true
    # modbus: false           # Modbus TCP server; default true for pro3em
    # modbus_port: 502
    # timezone: Europe/Amsterdam
    # wifi_ssid: HomeAssistant      # cosmetic (Wifi.GetStatus)
    # model_code: SPSW-004PE16EU    # override the reported model code
    # firmware_version: "2.0.0"
    # fw_id: "20260710-101218/2.0.0-g87fbfa4"
    channels:                 # 1..N entries, N = channels of the model
      - switch: switch.boiler           # switch / light / input_boolean entity; its state is the output
        name: Boiler
        power: sensor.boiler_power      # W   (optional, default 0)
        energy: sensor.boiler_energy    # total energy (optional; integrated from power if absent)
        energy_unit: kWh                # kWh | Wh; default: the entity's unit_of_measurement, else kWh
        voltage: ...                    # V   (optional)
        current: ...                    # A   (optional, derived from P/V if absent)
        energy_returned: ...            # optional
        frequency: ...                  # Hz
        power_factor: ...
        temperature: ...                # degrees C
        input: binary_sensor.x          # reported as input:N (optional)

  - model: pro3em
    name: "Grid meter"
    mac: "AABBCCDDEE01"
    bind: 192.168.1.21        # second device in the same container needs its own address
    em:
      energy_unit: kWh
      neutral_current: ...    # optional
      temperature: ...        # optional
      phases:                 # 1..3 entries (a, b, c); missing phases report 0 W
        - power: sensor.p1_power_l1     # W, negative = export
          voltage: ...                  # V (default 230)
          current: ...                  # A (derived if absent)
          apparent_power: ...           # VA (derived if absent)
          power_factor: ...             # default 1
          frequency: ...                # default 50
          energy: ...                   # import counter
          energy_returned: ...          # export counter
```

Environment variables: `HA_URL`, `HA_TOKEN`, `HA_TOKEN_FILE`, `CONFIG`, `LOG_LEVEL`,
`ADVERTISE_IP` (sets `ip:` for devices that do not set it).

Notes:

* `mac` is just an identifier, it does not have to match a real interface. Keep it fixed:
  Sigen stores the Shelly id. Changing it means re-pairing.
* Sensors that are `unavailable` / `unknown` fall back to their defaults (0 W). While HA
  is unreachable the last known values are kept and the service reconnects with backoff.
* The HTTP API is unauthenticated, as Sigen requires. Run it on a trusted LAN / VLAN.
* `*.SetConfig` calls are acknowledged but ignored, except the device name.

## Testing without a Sigen or without Home Assistant

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[test]'
pytest                                          # unit tests (RPC dispatcher, Modbus, config, HTTP)

python3 tools/ha_stub.py &                      # fake HA on ws://127.0.0.1:8123, token "test"
HA_URL=http://127.0.0.1:8123 HA_TOKEN=test shelly-emu -c examples/stub-test.yaml &
python3 tools/validate.py http://127.0.0.1:8080 --switch   # pro4pm on port 8080, toggles switch:0
python3 tools/validate.py http://127.0.0.1:8081            # pro3em on port 8081
python3 tools/modbus_smoke.py 127.0.0.1 5020               # Modbus registers
```

Against a real installation: `python3 tools/validate.py http://<ip>` and, for `pro3em`,
`python3 tools/modbus_smoke.py <ip>`. `dns-sd -B _shelly._tcp` (macOS) or
`avahi-browse -r _shelly._tcp` (Linux) shows the mDNS advertisement.
`shelly-emu --check -c config.yaml` validates a configuration without starting anything.

## Troubleshooting

* **mySigen does not find the device.** The phone, the SigenStor and the container must be
  on the same L2 network (mDNS does not cross subnets). Check with `dns-sd` / `avahi-browse`
  from another machine. Bridge networking never works, see above. On a host with several
  interfaces set `ip:` (or `ADVERTISE_IP`) and `mdns_interfaces:` to the LAN address.
  Multiple mDNS responders on the host (avahi, systemd-resolved with mDNS) usually coexist
  because of `SO_REUSEPORT`, but if the advertisement is missing try `mdns_interfaces:`.
* **"This Shelly model requires a newer software version."** Keep `firmware_version` at
  `2.0.0` with the default `fw_id`.
* **Device found but pairing fails or channels stay empty.** Run `tools/validate.py` against
  the host IP on port 80. Every line must be `OK`. Make sure nothing else answers on port 80.
* **Switching does nothing in HA.** The token must belong to a user that may control the
  entity. Watch the log at `LOG_LEVEL=DEBUG`: `HA switch.x -> on` followed by
  `HA switch.x = on` means the round trip works.
* **Energy values look 1000x off.** Set `energy_unit` explicitly (`kWh` or `Wh`) when the
  entity has no `unit_of_measurement`.
* **Port 80 permission denied.** The image grants `CAP_NET_BIND_SERVICE` to the Python
  binary; when running outside Docker use `http_port`/`modbus_port` above 1024 (testing
  only, Sigen cannot use them) or run with the capability / as root.
* **Meter shows nothing in Sigen.** The SigenStor reads `pro3em` over Modbus TCP 502 after
  pairing; `tools/modbus_smoke.py <ip>` must print live values.

## Project layout

```
shelly_emu/
  models.py            model table (codes, app names, host prefixes)
  state.py             value snapshots
  device.py            emulated device: identity, live values, energy integration, optimistic output
  rpc.py               JSON-RPC dispatcher and Shelly payload builders
  modbus_registers.py  Pro 3EM register map and Modbus TCP frame handling (pure)
  http_server.py       aiohttp transport (/shelly, /rpc, /rpc/<Method>)
  mdns.py              zeroconf advertisement (_shelly._tcp, _http._tcp, TTL 255)
  modbus_server.py     asyncio Modbus TCP server
  ha/client.py         Home Assistant WebSocket client (subscribe_entities, call_service)
  config.py            YAML configuration
  runner.py            wires devices, HA and transports together
  cli.py               entry point (shelly-emu)
tests/                 pytest suite
tools/                 validate.py, modbus_smoke.py, ha_stub.py
examples/              ready-to-adapt configs
```

The emulator core (`models`, `state`, `device`, `rpc`, `modbus_registers`) has no network or
Home Assistant dependencies, so a Home Assistant custom integration can reuse it unchanged.

## Credits

Protocol knowledge comes from the Shelly Gen2 API documentation, Tasmota's Shelly emulation
driver, sigelly_emu (a Pro 3EM emulator for Sigenstor) and the uni-meter issue tracker. See
the ESPHome project's design notes for details and links.

## License

MIT
