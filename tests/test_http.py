import asyncio

from aiohttp.test_utils import TestClient, TestServer

from shelly_emu.http_server import ShellyHttpServer, coerce_params


def test_coerce_params():
    assert coerce_params({"id": "0", "on": "true", "name": "abc", "x": "1.5", "method": "M"}, skip="method") == {
        "id": 0,
        "on": True,
        "name": "abc",
        "x": 1.5,
    }
    assert coerce_params({"s": '"quoted"'}) == {"s": '"quoted"'}


def test_http_surface(pro4pm, rpc4):
    async def scenario():
        server = ShellyHttpServer(pro4pm, rpc4, "127.0.0.1", 0)
        async with TestClient(TestServer(server.app)) as client:
            r = await client.get("/shelly")
            assert r.status == 200 and r.headers["Server"] == "ShellyHTTP/1.0.0"
            info = await r.json()
            assert info["id"] == "shellypro4pm-aabbccddeeff" and info["auth_en"] is False

            r = await client.get("/rpc/Switch.Set?id=1&on=true")
            assert await r.json() == {"was_on": False}
            assert pro4pm.commands == [(1, True)]
            r = await client.get("/rpc?method=Switch.GetStatus&id=1")
            assert (await r.json())["output"] is True
            r = await client.get('/rpc?method=Switch.GetStatus&params={"id":1}')
            assert (await r.json())["id"] == 1

            r = await client.post("/rpc/Switch.Set", json={"id": 0, "on": True})
            assert await r.json() == {"was_on": False}

            r = await client.post("/rpc", json={"id": 1, "src": "t", "method": "Shelly.GetDeviceInfo"})
            env = await r.json()
            assert env["id"] == 1 and env["src"] == info["id"] and env["dst"] == "t" and env["result"]["gen"] == 2

            r = await client.post("/rpc", json=[{"id": 1, "method": "RPC.Ping"}, {"id": 2, "method": "Nope"}])
            batch = await r.json()
            assert batch[0]["result"] == {} and batch[1]["error"]["code"] == 404

            r = await client.get("/rpc/Nope.Nope")
            assert r.status == 404 and (await r.json())["code"] == 404
            r = await client.get("/rpc/Switch.Set?id=9&on=true")
            assert r.status == 500 and (await r.json())["code"] == -105
            r = await client.get("/rpc")
            assert r.status == 400
            r = await client.post("/rpc", data=b"{not json")
            assert r.status == 400 and r.headers["Server"] == "ShellyHTTP/1.0.0"
            r = await client.get("/nope")
            assert r.status == 404

    asyncio.run(scenario())
