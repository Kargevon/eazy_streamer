import unittest

from eazy_streamer import ffmpeg_cmd as F
from eazy_streamer.devices import Device, pick_capture

URL = "rtmps://example.invalid:8443/live/KEY?t=SECRET"


class BuildTest(unittest.TestCase):
    def test_preview_only_has_no_network_output(self):
        args = F.build(None, None, F.quality("mid"))
        self.assertEqual(args[-1], "pipe:1")
        self.assertNotIn("flv", args)

    def test_live_sends_flv_and_keeps_preview(self):
        args = F.build(None, URL, F.quality("high"))
        self.assertEqual(args[args.index("flv") + 1], URL)
        self.assertEqual(args[args.index("-b:v") + 1], "4500k")
        self.assertEqual(args[-1], "pipe:1")

    def test_url_check(self):
        self.assertTrue(F.is_stream_url(URL))
        self.assertTrue(F.is_stream_url("  rtmp://host/app/key "))
        self.assertFalse(F.is_stream_url("https://host/live"))
        self.assertFalse(F.is_stream_url("rtmps://"))

    def test_error_text_never_shows_the_key(self):
        text = F.explain("Error opening output %s: Some new failure" % URL, URL)
        self.assertNotIn("SECRET", text)
        self.assertEqual(F.explain("x: Connection refused", URL), "сервер відхилив з'єднання")


class CaptureSizeTest(unittest.TestCase):
    DEV = Device("k", "/dev/video0", "card", {
        "YUYV": [(640, 480), (1920, 1080)],
        "MJPG": [(640, 480), (1280, 720), (1920, 1080), (640, 360)],
    })

    def test_smallest_wide_frame_that_is_enough(self):
        self.assertEqual(pick_capture(self.DEV, 480), ("MJPG", (1280, 720)))
        self.assertEqual(pick_capture(self.DEV, 720), ("MJPG", (1280, 720)))
        self.assertEqual(pick_capture(self.DEV, 1080), ("MJPG", (1920, 1080)))

    def test_card_smaller_than_wanted_gives_its_best(self):
        dev = Device("k", "/dev/video0", "card", {"YUYV": [(720, 576), (640, 480)]})
        self.assertEqual(pick_capture(dev, 1080), ("YUYV", (720, 576)))

    def test_free_size_card(self):
        self.assertEqual(pick_capture(Device("k", "p", "n", {"YUYV": []}), 720), ("YUYV", None))


if __name__ == "__main__":
    unittest.main()
