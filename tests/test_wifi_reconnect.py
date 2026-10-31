"""A layout write may close the whole Wi-Fi tunnel before verification."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ios_homescreen.device import DeviceError, open_device
from ios_homescreen.transport import Link, Transport


def provider(identifier="phone", broken=False):
    return SimpleNamespace(all_values={"UniqueDeviceID": identifier}, broken=broken)


class SpringBoard:
    def __init__(self, lockdown):
        self.lockdown = lockdown

    async def connect(self):
        if self.lockdown.broken:
            raise ConnectionError("userspace dial plane is closed")

    async def get_icon_state(self, **kwargs):
        return [[], [{"displayIdentifier": "kept.app"}]]

    async def close(self):
        pass


class WifiReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_reopens_closed_tunnel_pins_phone_and_closes_fresh_link(self):
        old_close, fresh_close = AsyncMock(), AsyncMock()
        connect = AsyncMock(side_effect=[
            Link(provider(broken=True), old_close, kind="wifi"),
            Link(provider(), fresh_close, kind="wifi"),
        ])
        with patch("ios_homescreen.device.connect", connect), patch(
            "ios_homescreen.device._springboard", SpringBoard
        ):
            device = await open_device(Transport(wifi=True))
            self.assertEqual(await device.read_state(), [[], [{"displayIdentifier": "kept.app"}]])
            self.assertEqual(connect.await_args_list[1].args[0].udid, "phone")
            old_close.assert_awaited_once()
            await device.close()
            fresh_close.assert_awaited_once()

    async def test_usb_failure_does_not_open_another_phone(self):
        connect = AsyncMock(return_value=Link(provider(broken=True), AsyncMock(), kind="usb"))
        with patch("ios_homescreen.device.connect", connect), patch(
            "ios_homescreen.device._springboard", SpringBoard
        ):
            device = await open_device(Transport())
            with self.assertRaises(DeviceError):
                await device.read_state()
            self.assertEqual(connect.await_count, 1)

    async def test_wrong_phone_is_rejected_and_closed(self):
        wrong_close = AsyncMock()
        connect = AsyncMock(side_effect=[
            Link(provider(broken=True), AsyncMock(), kind="wifi"),
            Link(provider("different-phone"), wrong_close, kind="wifi"),
        ])
        with patch("ios_homescreen.device.connect", connect), patch(
            "ios_homescreen.device._springboard", SpringBoard
        ):
            device = await open_device(Transport(wifi=True))
            with self.assertRaises(DeviceError) as error:
                await device.read_state()
            self.assertEqual(error.exception.code, "device_changed")
            wrong_close.assert_awaited_once()

    async def test_cancellation_does_not_reconnect(self):
        connect = AsyncMock(return_value=Link(provider(), AsyncMock(), kind="wifi"))
        with patch("ios_homescreen.device.connect", connect):
            device = await open_device(Transport(wifi=True))
            device._read_once = AsyncMock(side_effect=asyncio.CancelledError)
            with self.assertRaises(asyncio.CancelledError):
                await device.read_state()
            self.assertEqual(connect.await_count, 1)
