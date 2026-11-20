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
        self.assertEqual(args[args.index("-b:v") + 1], "2500k")
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
        self.assertEqual(pick_capture(self.DEV, 480), ("MJPG", (1280, 720), None))
        self.assertEqual(pick_capture(self.DEV, 720), ("MJPG", (1280, 720), None))
        # однаковий кадр є в обох форматах — беремо нестиснений, його не треба розтискати
        self.assertEqual(pick_capture(self.DEV, 1080), ("YUYV", (1920, 1080), None))

    def test_asks_30_fps_not_60_and_skips_slow_raw(self):
        card = Device("k", "p", "hdmi", {"YUYV": [(1920, 1080)]},
                      {("YUYV", 1920, 1080): [60.0, 50.0, 30.0, 20.0, 10.0]})
        self.assertEqual(pick_capture(card, 1080), ("YUYV", (1920, 1080), 30))
        self.assertEqual(pick_capture(card, 480, preview=True), ("YUYV", (1920, 1080), 10.0))
        cam = Device("k", "p", "cam", {"YUYV": [(1280, 720)], "MJPG": [(1280, 720)]},
                     {("YUYV", 1280, 720): [10.0], ("MJPG", 1280, 720): [30.0, 15.0]})
        self.assertEqual(pick_capture(cam, 720), ("MJPG", (1280, 720), 30))

    def test_sends_as_many_frames_as_source_gives(self):
        pal = Device("pal", "/dev/video7", "analog", {"MJPG": [(640, 480)]}, {("MJPG", 640, 480): [25.0]})
        args = F.build(pal, URL, F.quality("low"))
        self.assertEqual(args[args.index("-fps_mode") + 1], "vfr")
        self.assertNotIn("-r", args)
        self.assertEqual(args[args.index("-g") + 1], "50")
        self.assertNotIn("framestep", " ".join(args))
        fast = Device("fast", "/dev/video6", "hdmi60", {"YUYV": [(1280, 720)]}, {("YUYV", 1280, 720): [60.0]})
        self.assertIn("framestep=2,", " ".join(F.build(fast, URL, F.quality("mid"))))

    def test_levels(self):
        F.HW_NODE, F.GPU_PIPE = "/dev/dri/renderD128", True
        try:
            card = Device("lvl", "/dev/video9", "hdmi", {"YUYV": [(1920, 1080)]})
            full = " ".join(F.build(card, URL, F.quality("mid")))
            self.assertIn("format=yuyv422,hwupload", full)
            self.assertIn("scale_vaapi=w=1280:h=720", full)
            self.assertEqual(F.lower_level(card), 1)
            self.assertNotIn("scale_vaapi", " ".join(F.build(card, URL, F.quality("mid"))))
            self.assertEqual(F.lower_level(card), 0)
            self.assertIn("libx264", F.build(card, URL, F.quality("mid")))
            cam = Device("lvl2", "/dev/video8", "cam", {"MJPG": [(1280, 720)]})
            self.assertIn("-hwaccel", F.build(cam, URL, F.quality("mid")))
        finally:
            F.HW_NODE, F.GPU_PIPE = None, False
            F.LEVEL_CAP.clear()

    def test_card_smaller_than_wanted_gives_its_best(self):
        dev = Device("k", "/dev/video0", "card", {"YUYV": [(720, 576), (640, 480)]})
        self.assertEqual(pick_capture(dev, 1080), ("YUYV", (720, 576), None))

    def test_bitrate_follows_real_source_size(self):
        small = Device("k", "/dev/video0", "cam", {"MJPG": [(640, 480)]})
        self.assertEqual(F.stream_kbit(small, F.quality("high")), 1000)
        self.assertEqual(F.stream_kbit(self.DEV, F.quality("high")), 4500)
        self.assertEqual(F.stream_kbit(None, F.quality("high")), 2500)     # тестова картинка 720p
        self.assertEqual(F.stream_kbit(None, F.quality("low")), 1000)

    def test_free_size_card(self):
        self.assertEqual(pick_capture(Device("k", "p", "n", {"YUYV": []}), 720), ("YUYV", None, None))


class NetParseTest(unittest.TestCase):
    def test_reads_sent_retrans_and_rtt_per_pid(self):
        from eazy_streamer.netwatch import parse
        text = (
            'ESTAB 0 0 10.0.0.2:42730 1.2.3.4:8443 users:(("ffmpeg",pid=501,fd=5))\n'
            '\t cubic rto:204 rtt:38.5/4.1 mss:1400 bytes_sent:1000000 bytes_retrans:2500 bytes_acked:99\n'
            'ESTAB 0 0 10.0.0.2:42731 1.2.3.5:8443 users:(("ffmpeg",pid=502,fd=5))\n'
            '\t cubic rto:204 rtt:0.6/0.9 bytes_sent:777 bytes_acked:778\n'
            'ESTAB 0 0 10.0.0.2:22 9.9.9.9:5555\n'
            '\t cubic rtt:1/1 bytes_sent:5\n')
        self.assertEqual(parse(text), {501: (1000000, 2500, 38.5), 502: (777, 0, 0.6)})


if __name__ == "__main__":
    unittest.main()
