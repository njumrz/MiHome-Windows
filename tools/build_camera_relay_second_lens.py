# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the optional dual-lens adapter from a local MijiaDeck v1.1.2 checkout."""

import argparse
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

_CLASS_NAME = "CameraRelayDualLens"
_JAR_NAME = "camera-relay-dual-lens-v10.jar"
_CHECK = "CAMERA_RELAY_DUAL_LENS"


def build(jdk: Path, source: Path, relay_dir: Path) -> Path:
    relay_dir = relay_dir.resolve()
    upstream = relay_dir / "camera-relay.jar"
    runtime = relay_dir / "runtime/bin/java.exe"

    java = source.read_text(encoding="utf-8")
    # Match go2rtc's MISS StartMedia request for channel=2. Handle lens changes on the
    # media-reading thread so the previous stream cannot publish into the new HTTP endpoint.
    java = java.replace("CameraRelay", _CLASS_NAME).replace("CAMERA_RELAY_2", _CHECK)
    start_call = "camera.startVideo(quality, audio);"
    if java.count(start_call) != 3 or java.count(_CHECK) != 1:
        raise ValueError("Expected the CameraRelay.java source from MijiaDeck v1.1.2")
    java = java.replace(start_call, "startLens();")
    replacements = {
        "import java.nio.charset.StandardCharsets;": '''import java.nio.charset.StandardCharsets;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.List;
import micam.pppp.Frame;
import micam.pppp.Msg;''',
        "    private static int videoCodec;": r'''    private static int videoCodec;
    private static volatile CameraPublicSession publicSession;
    private static volatile CameraPairedMedia pairedMedia;
    private static volatile int lens = 1;
    private static volatile String[] nextStream;
    private static String relayStage;
    private static long videoBytes;
    private static long lastByteCount;
    private static volatile byte[] mediaKey;
    private static final int SPEAKER_START = 0x106;
    private static final int SPEAKER_RESPONSE = 0x107;
    private static final int SPEAKER_STOP = 0x108;
    private static final int CODEC_PCMU = 1026;
    private static int receivedAudioCodec;
    private static int speakerCodec;
    private static long speakerStarted;
    private static int speakerSequence;
    private static boolean speakerResponded;

    private static synchronized void startTalk() {
        if (speakerStarted != 0) return;
        camera.sendEncrypted(SPEAKER_START, "");
        speakerStarted = System.nanoTime();
        speakerSequence = 0;
        receivedAudioCodec = 0;
        speakerResponded = false;
    }

    private static synchronized void stopTalk() {
        if (speakerStarted == 0) return;
        speakerStarted = 0;
        speakerCodec = 0;
        camera.sendEncrypted(SPEAKER_STOP, "");
        report("TALK STOPPED");
    }

    private static synchronized void updateTalk(MediaPacket packet) {
        if (speakerStarted == 0) return;
        if (packet != null && (packet.codecId() == MediaPacket.CODEC_OPUS
                || packet.codecId() == MediaPacket.CODEC_PCMA || packet.codecId() == CODEC_PCMU)) {
            receivedAudioCodec = packet.codecId();
        }
        if (!speakerResponded) {
            MissClient.Response response = camera.awaitResponse(Duration.ofMillis(1));
            if (response != null) {
                try {
                    int command = response.command();
                    if (command == MissClient.CMD_ENCODED) {
                        byte[] payload = MissCrypto.decrypt(mediaKey, response.body());
                        if (payload.length >= 4) command = ByteBuffer.wrap(payload).getInt();
                    }
                    if (command == SPEAKER_RESPONSE) {
                        speakerResponded = true;
                        report("TALK PEER RESPONSE");
                    }
                } catch (RuntimeException ignored) { }
            }
        }
        if (speakerCodec != 0) return;
        long elapsed = System.nanoTime() - speakerStarted;
        if (elapsed >= Duration.ofSeconds(1).toNanos() && receivedAudioCodec != 0) {
            speakerCodec = receivedAudioCodec;
            report(switch (speakerCodec) {
                case MediaPacket.CODEC_OPUS -> "TALK OPUS";
                case CODEC_PCMU -> "TALK PCMU";
                default -> "TALK PCMA";
            });
        } else if (elapsed >= Duration.ofSeconds(5).toNanos()) {
            stopTalk();
            report("TALK FAILED");
        }
    }

    private static synchronized void sendTalkAudio(String encoded) {
        if (speakerCodec == 0 || encoded.length() > 1700) return;
        byte[] audioPayload = Base64.getDecoder().decode(encoded);
        boolean valid = speakerCodec == MediaPacket.CODEC_OPUS
                ? audioPayload.length > 0 && audioPayload.length <= 1275 : audioPayload.length == 320;
        if (!valid) return;
        connection.sendRaw(talkFrame(mediaKey, speakerCodec, speakerSequence++, System.currentTimeMillis(), audioPayload));
        if (speakerSequence == 1) report("TALK PACKET SENT");
    }

    private static byte[] talkFrame(byte[] key, int codec, int sequence, long time, byte[] audioPayload) {
        byte[] encrypted = MissCrypto.encrypt(key, audioPayload);
        // The channel 3 envelope is big-endian. The MISS media header is little-endian.
        ByteBuffer frame = ByteBuffer.allocate(8 + MediaPacket.HEADER_LEN + encrypted.length);
        frame.put((byte) 0xd1).put((byte) 3).putShort((short) sequence).putInt(MediaPacket.HEADER_LEN + encrypted.length);
        frame.order(ByteOrder.LITTLE_ENDIAN).putInt(encrypted.length).putInt(codec);
        frame.position(8 + 16);
        frame.putLong(time);
        frame.position(8 + MediaPacket.HEADER_LEN);
        frame.put(encrypted);
        return Frame.encode(Msg.DRW, frame.array());
    }

    private static void checkTalkFrame() {
        byte[] key = new byte[32];
        byte[] sample = {1, 2, 3};
        byte[] datagram = talkFrame(key, MediaPacket.CODEC_OPUS, 17, 100, sample);
        Frame frame = Frame.parse(datagram, datagram.length);
        ByteBuffer header = ByteBuffer.wrap(frame.payload());
        if (frame.type() != Msg.DRW || header.get() != (byte) 0xd1 || header.get() != 3
                || header.getShort() != 17 || header.getInt() != frame.payload().length - 8) {
            throw new IllegalStateException("Invalid audio envelope");
        }
        byte[] media = java.util.Arrays.copyOfRange(frame.payload(), 8, frame.payload().length);
        MediaPacket decoded = MediaPacket.parse(media, key);
        if (decoded.codecId() != MediaPacket.CODEC_OPUS || decoded.timestampMs() != 100
                || !java.util.Arrays.equals(decoded.payload(), sample)) {
            throw new IllegalStateException("Invalid audio payload");
        }
    }

    private static void reportVideoBytes(MediaPacket packet) {
        videoBytes += packet.payload().length;
        long now = System.nanoTime();
        if (now - lastByteCount >= Duration.ofSeconds(1).toNanos()) {
            report("BYTES " + videoBytes);
            lastByteCount = now;
        }
    }

    private static int relayRound = 1;
    private static int emptyRelayRounds;
    private static long relayRoundStart = System.nanoTime();
    private static int candidates, listRequests, listReplies, relayOffers, helloRequests, helloReplies, portReplies, peerReplies;

    private static void reportRelayRound() {
        long millis = Duration.ofNanos(System.nanoTime() - relayRoundStart).toMillis();
        report("RELAY ROUND %d %d %d %d %d %d %d %d %d %d".formatted(
                relayRound, millis, candidates, listRequests, listReplies, relayOffers,
                helloRequests, helloReplies, portReplies, peerReplies));
    }

    private static void relayProgress(String message) {
        String stage;
        // The upstream trace contains addresses and credentials. Only emit counters and fixed stage names.
        if (message.startsWith("  <- ")) {
            switch (message.substring(message.lastIndexOf(' ') + 1)) {
                case "LIST_REQ_ACK" -> listReplies++;
                case "RLY_TO" -> relayOffers++;
                case "RLY_HELLO_ACK" -> helloReplies++;
                case "RLY_PORT_ACK" -> portReplies++;
                case "RLY_RDY" -> peerReplies++;
            }
            return;
        }
        if (message.startsWith("  -> ")) {
            if (message.endsWith(" LIST_REQ1")) listRequests++;
            if (message.endsWith(" RLY_HELLO")) helloRequests++;
            return;
        }
        if (message.startsWith("uid ")) {
            relayRoundStart = System.nanoTime();
            stage = "DISCOVERY";
        } else if (message.startsWith("retrying discovery")) {
            emptyRelayRounds = candidates == 0 && relayOffers == 0 ? emptyRelayRounds + 1 : 0;
            if (emptyRelayRounds >= 3) {
                // Give the other transport a chance and let the caller retry with a fresh grant.
                throw new IllegalStateException("No relay candidates in three discovery rounds");
            }
            reportRelayRound();
            relayRound++;
            candidates = listRequests = listReplies = relayOffers = helloRequests = helloReplies = portReplies = peerReplies = 0;
            relayRoundStart = System.nanoTime();
            stage = "DISCOVERY";
        } else if (message.startsWith("  candidate ")) {
            candidates++;
            stage = "REGISTER";
        } else if (message.startsWith("relay selected ")) stage = "PORT";
        else if (message.startsWith("relay session ")) stage = "CAMERA";
        else return;
        if (!stage.equals(relayStage)) {
            relayStage = stage;
            report("RELAY " + stage);
        }
    }

    private static void startLens() {
        if (pairedMedia != null) pairedMedia.requestVideo();
        else if (lens == 1) camera.startVideo(quality, audio);
        else camera.sendEncrypted(MissClient.CMD_VIDEO_START,
                "{\"videoquality\":-1,\"videoquality2\":%d,\"enableaudio\":%d}"
                .formatted(quality, audio ? 1 : 0));
    }

    private static void receiveAudio(MediaPacket packet) {
        if (packet == null || !audio) return;
        if (packet.codecId() == 1027 || packet.codecId() == 1026) {
            int rate = ((packet.flags() >>> 3) & 15) == 0 ? 8000 : 16000;
            report("AUDIO " + rate + " " + Base64.getEncoder().encodeToString(pcm(packet)));
        } else if (packet.codecId() == 1032) {
            report("AUDIO OPUS " + Base64.getEncoder().encodeToString(packet.payload()));
        }
    }

    private static void playPaired() throws Exception {
        while (!stopped.get()) {
            String[] requested = nextStream;
            if (requested != null) {
                nextStream = null;
                pairedMedia.changeQuality(Integer.parseInt(requested[1]), Integer.parseInt(requested[2]),
                        new CameraPairedMedia.Endpoint(Integer.parseInt(requested[3]), requested[4]));
            }
            MediaPacket packet = camera.nextMedia(Duration.ofMillis(500));
            updateTalk(packet);
            if (packet != null && packet.isVideo()) pairedMedia.publish(packet);
            else receiveAudio(packet);
            String failure = pairedMedia.timeout();
            if (failure != null) {
                report(failure);
                return;
            }
        }
    }

    private static MediaPacket nextKeyframe() {
        long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
        while (!stopped.get() && System.nanoTime() < deadline) {
            MediaPacket packet = camera.nextMedia(Duration.ofMillis(500));
            if (packet != null && packet.isVideo() && packet.isKeyframe()) return packet;
        }
        throw new IllegalStateException();
    }

    private static void pauseVideo() {
        camera.stopVideo();
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        while (camera.nextMedia(Duration.ofMillis(350)) != null) {
            if (stopped.get() || System.nanoTime() >= deadline) throw new IllegalStateException();
        }
    }

    private static void switchStream(String[] fields) throws Exception {
        boolean changeQuality = fields[0].equals("QUALITY");
        if (!changeQuality) pauseVideo();
        if (stopped.get()) return;
        int previousQuality = quality;
        int value = Integer.parseInt(fields[1]);
        if (changeQuality) quality = value;
        else lens = value;
        MediaPacket first;
        try {
            startLens();
            first = nextKeyframe();
            if (changeQuality) report("QUALITY ACCEPTED");
        } catch (RuntimeException error) {
            if (!changeQuality || stopped.get()) throw error;
            quality = previousQuality;
            startLens();
            first = nextKeyframe();
            report("QUALITY REJECTED");
        }
        if (stopped.get()) return;
        server.close();
        server = new HttpStreamServer(Integer.parseInt(fields[2]), fields[3], null);
        server.start();
        server.publish(first);
    }
''',
        '        String phase = "CONFIG";': '''        String phase = "CONFIG";
        if (args.length >= 1 && args[0].equals("2")) lens = 2;
        boolean relayOnly = List.of(args).contains("--relay-only");
        boolean preferDirect = List.of(args).contains("--prefer-p2p");
        boolean paired = List.of(args).contains("--paired");
        boolean channelIndex = List.of(args).contains("--channel-index");''',
        '            quality = Integer.parseInt(field(input));': '''            quality = Integer.parseInt(field(input));
            CameraPairedMedia.Endpoint secondEndpoint = paired
                    ? new CameraPairedMedia.Endpoint(Integer.parseInt(field(input)), field(input)) : null;
            int secondQuality = paired ? Integer.parseInt(field(input)) : 1;
            if (secondQuality < 0 || secondQuality > 3) throw new IllegalArgumentException();''',
        '            if (quality < 0 || quality > 2) throw new IllegalArgumentException();':
            '            if (quality < 0 || quality > 3) throw new IllegalArgumentException();',
        f'            report("{_CHECK}");': f'            checkTalkFrame();\n            report("{_CHECK}");',
        '''            phase = "RELAY";
            report("PHASE RELAY");
            relay = new RelaySession(Uid.parse(uid), InitString.servers(init), null);
            RelaySession.Established session = relay.connect(Duration.ofSeconds(45));
            if (stopped.get()) return;
            phase = "AUTH";
            report("PHASE AUTH");
            connection = new PpppConnection(relay.socket(), session.relay(), null);''': '''            Uid peerId = Uid.parse(uid);
            List<String> discovery = InitString.servers(init);
            phase = "RELAY";
            report("PHASE PUBLIC");
            publicSession = new CameraPublicSession(peerId, discovery);
            CameraPublicSession.Peer peer = publicSession.connect(relayOnly, preferDirect, stopped::get,
                    CameraRelayDualLens::report, CameraRelayDualLens::relayProgress, CameraRelayDualLens::reportRelayRound);
            if (stopped.get()) return;
            report(peer.direct() ? "ROUTE SELECT P2P" : "ROUTE SELECT RELAY");
            connection = new PpppConnection(peer.socket(), peer.address(), null);
            phase = "AUTH";
            report("PHASE AUTH");''',
        '            camera.useSessionKey(MissCrypto.sessionKey(clientPrivate, devicePublic));':
            '            mediaKey = MissCrypto.sessionKey(clientPrivate, devicePublic);\n            camera.useSessionKey(mediaKey);',
        '''            startLens();
            phase = "MEDIA";''': '''            if (paired) {
                phase = "MEDIA";
                report("PHASE MEDIA");
                pairedMedia = new CameraPairedMedia(camera, quality, secondQuality, channelIndex,
                        () -> audio, CameraRelayDualLens::report);
                pairedMedia.start(new CameraPairedMedia.Endpoint(port, path), secondEndpoint);
                phase = "STREAM";
                playPaired();
                return;
            }
            startLens();
            phase = "MEDIA";''',
        '        try { if (camera != null) camera.stopVideo(); } catch (Exception ignored) { }':
            '        try { stopTalk(); } catch (Exception ignored) { }\n'
            '        try { if (camera != null) camera.stopVideo(); } catch (Exception ignored) { }',
        '    private static volatile RelaySession relay;\n': '',
        'import micam.pppp.RelaySession;\n': '',
        '        try { if (relay != null) relay.close(); } catch (Exception ignored) { }':
            '        if (publicSession != null) publicSession.close();',
        '        try { if (server != null) server.close(); } catch (Exception ignored) { }':
            '        try { if (server != null) server.close(); } catch (Exception ignored) { }\n'
            '        if (pairedMedia != null) pairedMedia.close();',
        '                    server.publish(packet);':
            '                    server.publish(packet);\n                    reportVideoBytes(packet);',
        '''                } else if (packet != null && audio && (packet.codecId() == 1027 || packet.codecId() == 1026)) {
                    report("AUDIO 8000 " + Base64.getEncoder().encodeToString(pcm(packet)));
                } else if (packet != null && audio && packet.codecId() == 1032) {
                    report("NOTICE AUDIO_UNSUPPORTED");
                    audio = false;''': '''                } else if (packet != null && audio && (packet.codecId() == 1027 || packet.codecId() == 1026
                        || packet.codecId() == 1032)) {
                    receiveAudio(packet);''',
        '                            if (command.matches("QUALITY [012]")) {': r'''                            if (command.equals("TALK START")) {
                                startTalk();
                            } else if (command.equals("TALK STOP")) {
                                stopTalk();
                            } else if (command.startsWith("TALK DATA ")) {
                                sendTalkAudio(command.substring(10));
                            } else if (paired && command.matches("FEED [12] [0-3] [0-9]{1,5} /camera-[a-f0-9]{32}\\.ts")) {
                                nextStream = command.split(" ");
                            } else if (command.matches("(?:SWITCH [12]|QUALITY [0-3]) [0-9]{1,5} /camera-[a-f0-9]{32}\\.ts")) {
                                nextStream = command.split(" ");
                            } else if (command.matches("QUALITY [0-3]")) {''',
        '''            long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
            MediaPacket first = null;
            while (!stopped.get() && System.nanoTime() < deadline) {
                MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));
                if (packet != null && packet.isVideo() && packet.isKeyframe()) {
                    first = packet;
                    break;
                }
            }
            if (first == null) throw new IllegalStateException();''': '            MediaPacket first = nextKeyframe();',
        '''            while (!stopped.get()) {
                MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));''': '''            while (!stopped.get()) {
                String[] requested = nextStream;
                if (requested != null) {
                    nextStream = null;
                    phase = requested[0];
                    switchStream(requested);
                    if (stopped.get()) return;
                    report("READY");
                    phase = "STREAM";
                    lastFrame = System.nanoTime();
                }
                MediaPacket packet = camera.nextMedia(Duration.ofMillis(500));
                updateTalk(packet);''',
    }
    for original, replacement in replacements.items():
        if java.count(original) != 1:
            raise ValueError("Expected the CameraRelay.java source from MijiaDeck v1.1.2")
        java = java.replace(original, replacement)

    target = relay_dir / _JAR_NAME
    with TemporaryDirectory(prefix="relay-build-", dir=relay_dir) as build_dir:
        temp = Path(build_dir)
        java_file = temp / f"{_CLASS_NAME}.java"
        java_file.write_text(java, encoding="utf-8")
        subprocess.run([
            str(jdk.resolve() / "bin/javac.exe"), "--release", "21", "-encoding", "UTF-8",
            "-cp", str(upstream), "-d", str(temp), str(java_file),
            str(Path(__file__).with_name("CameraDirectSession.java")),
            str(Path(__file__).with_name("CameraPublicSession.java")),
            str(Path(__file__).with_name("CameraPairedMedia.java")),
        ], check=True)

        candidate = temp / _JAR_NAME
        with ZipFile(candidate, "w", ZIP_DEFLATED) as jar:
            for compiled in sorted(temp.glob("*.class")):
                jar.write(compiled, compiled.name)
        classpath = os.pathsep.join(map(str, (candidate, upstream)))
        result = subprocess.run(
            [str(runtime), "-cp", classpath, _CLASS_NAME, "--check"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        if result.stdout.strip() != _CHECK:
            raise RuntimeError("The compiled dual-lens adapter did not pass its startup check")
        candidate.replace(target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jdk", type=Path, required=True, help="Windows JDK 21 directory")
    parser.add_argument("--source", type=Path, required=True, help="MijiaDeck v1.1.2 CameraRelay.java")
    parser.add_argument("--relay-dir", type=Path, required=True, help="Local MijiaDeck relay component directory")
    args = parser.parse_args()
    print(build(args.jdk, args.source, args.relay_dir))
