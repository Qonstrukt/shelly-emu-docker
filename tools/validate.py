#!/usr/bin/env python3
"""Exercise the Shelly Gen2 HTTP surface of an emulated device (or a real Shelly).

Usage:  python3 tools/validate.py http://<ip>  [--switch]

Checks the endpoints Sigenergy / mySigen are known to use and prints a compact report.
Pass --switch to also toggle switch:0 off and on again (it changes real state!).
"""
import json
import sys
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1"
DO_SWITCH = "--switch" in sys.argv


def get(path):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, json.loads(r.read().decode())


def post(body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + "/rpc", data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, json.loads(r.read().decode())


def check(name, ok, detail=""):
    print(f"[{'OK ' if ok else 'FAIL'}] {name} {detail}")
    return ok


status, headers, info = get("/shelly")
check("GET /shelly", status == 200 and info.get("gen") == 2, f"id={info.get('id')} app={info.get('app')} ver={info.get('ver')}")
check("Server header", headers.get("Server") == "ShellyHTTP/1.0.0", headers.get("Server"))
check("auth disabled", info.get("auth_en") is False)

_, _, dev = get("/rpc/Shelly.GetDeviceInfo")
check("Shelly.GetDeviceInfo", dev.get("id") == info.get("id") and dev.get("fw_id"), dev.get("fw_id"))

_, _, st = get("/rpc/Shelly.GetStatus")
check("Shelly.GetStatus", "sys" in st and "wifi" in st, "components: " + ", ".join(sorted(st)))

_, _, cfg = get("/rpc/Shelly.GetConfig")
check("Shelly.GetConfig", "sys" in cfg)

_, _, comps = get("/rpc/Shelly.GetComponents")
check("Shelly.GetComponents", "components" in comps, f"total={comps.get('total')}")

_, _, wifi = get("/rpc/Wifi.GetStatus")
check("Wifi.GetStatus", "status" in wifi, f"{wifi.get('status')} {wifi.get('sta_ip')}")
_, _, wifi2 = get("/rpc/WiFi.GetStatus")  # Sigen spells it this way
check("WiFi.GetStatus (alias)", "status" in wifi2)

_, _, sysst = get("/rpc/Sys.GetStatus")
check("Sys.GetStatus", "uptime" in sysst, f"uptime={sysst.get('uptime')}")

_, _, methods = get("/rpc/Shelly.ListMethods")
check("Shelly.ListMethods", "methods" in methods, f"{len(methods.get('methods', []))} methods")

_, _, env = post({"id": 1, "src": "validate", "method": "Shelly.GetDeviceInfo"})
check("POST /rpc envelope", env.get("id") == 1 and env.get("src") == info.get("id") and "result" in env, f"dst={env.get('dst')}")

_, _, unknown = post({"id": 2, "src": "validate", "method": "Nope.Nope"})
check("unknown method -> error", "error" in unknown, str(unknown.get("error")))

if "switch:0" in st:
    _, _, sw = get("/rpc/Switch.GetStatus?id=0")
    check("Switch.GetStatus id=0", "output" in sw and "apower" in sw, f"output={sw.get('output')} apower={sw.get('apower')}")
    _, _, swc = get("/rpc/Switch.GetConfig?id=0")
    check("Switch.GetConfig id=0", "in_mode" in swc)
    if DO_SWITCH:
        was = sw["output"]
        _, _, r = get(f"/rpc/Switch.Set?id=0&on={'false' if was else 'true'}")
        check("Switch.Set toggle", r.get("was_on") == was, str(r))
        _, _, r = get(f"/rpc/Switch.Set?id=0&on={'true' if was else 'false'}")
        check("Switch.Set restore", r.get("was_on") == (not was), str(r))

if "em:0" in st:
    _, _, em = get("/rpc/EM.GetStatus?id=0")
    check("EM.GetStatus", "total_act_power" in em, f"total_act_power={em.get('total_act_power')}")
    _, _, emd = get("/rpc/EMData.GetStatus?id=0")
    check("EMData.GetStatus", "total_act" in emd, f"total_act={emd.get('total_act')}")
    print("Modbus TCP: python3 tools/modbus_smoke.py", BASE.split("//")[1])
