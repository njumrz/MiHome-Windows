// SPDX-License-Identifier: GPL-3.0-or-later

import java.time.Duration;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

import micam.miss.MediaPacket;
import micam.miss.MissClient;
import micam.server.HttpStreamServer;

/** Publishes each camera stream to its own loopback player on a shared MISS session. */
final class CameraPairedMedia implements AutoCloseable {
    private static final int CHANGE_QUALITY = 0x109;
    private static final int SECOND_LENS_FLAG = 0x00400000;
    private static final long FRAME_TIMEOUT = Duration.ofSeconds(20).toNanos();
    private static final long QUALITY_TIMEOUT = Duration.ofSeconds(12).toNanos();

    record Endpoint(int port, String path) {
        Endpoint {
            if (port < 1 || port > 65535 || !path.matches("/camera-[a-f0-9]{32}\\.ts")) {
                throw new IllegalArgumentException("Invalid loopback stream endpoint");
            }
        }

        HttpStreamServer open() throws Exception {
            HttpStreamServer http = new HttpStreamServer(port, path, null);
            http.start();
            return http;
        }
    }

    private static final class Feed {
        final int lens;
        HttpStreamServer http;
        volatile int quality;
        boolean ready;
        boolean failed;
        long lastFrame;
        long bytes;
        long lastByteReport;

        Feed(int lens, int quality) {
            this.lens = lens;
            this.quality = quality;
        }
    }

    private record Change(Feed feed, Endpoint endpoint, int previousQuality, long deadline, boolean accepted) {}

    private final Feed[] feeds;
    private final MissClient camera;
    private final boolean channelIndex;
    private final BooleanSupplier audio;
    private final Consumer<String> report;
    private Change change;

    CameraPairedMedia(MissClient camera, int firstQuality, int secondQuality, boolean channelIndex,
                      BooleanSupplier audio, Consumer<String> report) {
        this.camera = camera;
        this.channelIndex = channelIndex;
        this.audio = audio;
        this.report = report;
        feeds = new Feed[]{new Feed(1, firstQuality), new Feed(2, secondQuality)};
    }

    void start(Endpoint first, Endpoint second) throws Exception {
        feeds[0].http = first.open();
        feeds[1].http = second.open();
        feeds[0].lastFrame = feeds[1].lastFrame = System.nanoTime();
        requestVideo();
    }

    void requestVideo() {
        camera.sendEncrypted(MissClient.CMD_VIDEO_START,
                "{\"videoquality\":%d,\"videoquality2\":%d,\"enableaudio\":%d}"
                        .formatted(feeds[0].quality, feeds[1].quality, audio.getAsBoolean() ? 1 : 0));
    }

    private void requestQuality() {
        camera.sendEncrypted(CHANGE_QUALITY, "{\"videoquality\":%d,\"videoquality2\":%d}"
                .formatted(feeds[0].quality, feeds[1].quality));
    }

    void changeQuality(int lens, int quality, Endpoint endpoint) {
        if (change != null) return;
        Feed feed = feeds[lens - 1];
        change = new Change(feed, endpoint, feed.quality, System.nanoTime() + QUALITY_TIMEOUT, true);
        boolean sameQuality = feed.quality == quality;
        feed.quality = quality;
        if (channelIndex && !sameQuality) requestQuality();
        else requestVideo();
    }

    static int lensOf(MediaPacket packet, boolean channelIndex) {
        if (!channelIndex) return (packet.flags() & SECOND_LENS_FLAG) == 0 ? 1 : 2;
        return switch ((packet.flags() >>> 21) & 63) {
            case 0 -> 1;
            case 10 -> 2;
            default -> 0;
        };
    }

    void publish(MediaPacket packet) throws Exception {
        int lens = lensOf(packet, channelIndex);
        if (lens == 0) return;
        Feed feed = feeds[lens - 1];
        long now = System.nanoTime();
        if ((!feed.ready || feed.failed) && !packet.isKeyframe()) return;
        feed.lastFrame = now;

        boolean changed = change != null && change.feed() == feed && packet.isKeyframe();
        if (changed) {
            HttpStreamServer replacement = change.endpoint().open();
            feed.http.close();
            feed.http = replacement;
        }
        feed.http.publish(packet);
        if (changed) {
            report.accept("FEED QUALITY %d %d %d".formatted(feed.lens, feed.quality, change.accepted() ? 1 : 0));
            change = null;
        }
        if (changed || !feed.ready || feed.failed) {
            feed.ready = true;
            feed.failed = false;
            report.accept("FEED READY " + feed.lens);
        }
        feed.bytes += packet.payload().length;
        if (now - feed.lastByteReport >= Duration.ofSeconds(1).toNanos()) {
            feed.lastByteReport = now;
            report.accept("FEED BYTES %d %d".formatted(feed.lens, feed.bytes));
        }
    }

    String timeout() {
        long now = System.nanoTime();
        if (change != null && now >= change.deadline()) {
            Feed feed = change.feed();
            if (change.accepted()) {
                feed.quality = change.previousQuality();
                change = new Change(feed, change.endpoint(), feed.quality, now + QUALITY_TIMEOUT, false);
                if (channelIndex) requestQuality();
                else requestVideo();
            } else {
                report.accept("FEED QUALITY %d %d 0".formatted(feed.lens, feed.quality));
                change = null;
                report.accept("FEED FAILED " + feed.lens);
                feed.failed = true;
            }
        }
        if (now - feeds[0].lastFrame >= FRAME_TIMEOUT && now - feeds[1].lastFrame >= FRAME_TIMEOUT) {
            return feeds[0].ready || feeds[1].ready ? "ERROR STREAM" : "ERROR MEDIA";
        }
        for (Feed feed : feeds) {
            if (!feed.failed && now - feed.lastFrame >= FRAME_TIMEOUT) {
                if (change != null && change.feed() == feed) continue;
                feed.failed = true;
                report.accept("FEED FAILED " + feed.lens);
            }
        }
        return null;
    }

    @Override
    public void close() {
        for (Feed feed : feeds) {
            if (feed.http != null) feed.http.close();
        }
    }
}
